#!/usr/bin/env bash
#
# Two roles out of one image:
#
#   provision   one-shot. Makes a key pair and signs a token, then exits.
#               The private half is written to /private, which the serving
#               container does not mount: a server that cannot sign is a
#               server that, once taken, still cannot issue itself a token.
#   serve       long-running. Reads /keys (public halves only) and serves.
#
# Everything printed here goes to stderr on purpose. Under the stdio transport
# stdout carries JSON-RPC, and one stray line of chatter breaks the handshake
# before the client ever sees the server.

set -euo pipefail

DATA_DIR="${MCP_DATA_DIR:-/data}"
KEYS_DIR="${MCP_AUTHORIZED_KEYS_DIR:-/keys}"
PRIVATE_DIR="${MCP_PRIVATE_KEY_DIR:-/private}"
OUT_DIR="${MCP_OUT_DIR:-/out}"
# provision.py and its templates. Defaulted so the script also runs from a
# checkout, where they sit next to it.
PROVISION_HOME="${PROVISION_HOME:-$(cd -- "$(dirname -- "$0")" && pwd)}"

say() { printf '%s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# The same spellings main.py accepts, so a value that works as MCP_REQUIRE_AUTH
# means the same thing to both.
is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1 | true | yes | on) return 0 ;;
        *) return 1 ;;
    esac
}

# sqlite opens -wal and -shm next to the database, so what has to exist and be
# writable is the directory, not the file.
ensure_parent() {
    [ -n "${1:-}" ] || return 0
    mkdir -p "$(dirname -- "$1")"
}

ensure_state_dirs() {
    mkdir -p "$DATA_DIR"
    ensure_parent "${MCP_STAGING_DB:-}"
    ensure_parent "${MCP_AUDIT_LOG:-}"
    if [ -n "${MCP_EXPORT_DIR:-}" ]; then mkdir -p "$MCP_EXPORT_DIR"; fi
    if [ -n "${LOG_DIR:-}" ]; then mkdir -p "$LOG_DIR"; fi
}

# ------------------------------------------------------------------ serve ---

cmd_serve() {
    ensure_state_dirs

    if is_true "${MCP_REQUIRE_AUTH:-}"; then
        [ -d "$KEYS_DIR" ] || die \
            "MCP_REQUIRE_AUTH is on but $KEYS_DIR is not a directory. Mount the" \
            "public keys there, or run the provision role first."
        # An empty allowlist starts cleanly and then refuses every caller, which
        # reads as a broken server rather than an unprovisioned one.
        if ! compgen -G "$KEYS_DIR/*.pub" >/dev/null; then
            die "no *.pub in $KEYS_DIR, so no agent could authenticate. Run" \
                "\`provision --kid <name> --scope <tool>\` first."
        fi
    fi

    # A signing key reachable from the serving container gives away the one
    # property the split above buys. Worth saying out loud even when it is a
    # deliberate single-volume shortcut in development.
    if compgen -G "$PRIVATE_DIR/*.pem" >/dev/null 2>&1; then
        say "warning: a signing key is visible at $PRIVATE_DIR. The serving" \
            "container should not mount it — anyone who reaches this process" \
            "can then mint their own tokens."
    fi

    exec mcp-connector "$@"
}

# -------------------------------------------------------------- provision ---

# Why the server would refuse the token on file, or nothing at all when it
# would accept it.
#
# Existence is not enough to go on. `docker compose down -v` takes the key
# volumes with it and leaves the bind-mounted ./out behind, so the next start
# regenerates the key pair while yesterday's token is still sitting there —
# and the server then rejects it with InvalidSignatureError, which reads as a
# broken deployment rather than a stale file. An expired token and an audience
# changed in .env fail the same way, so ask the only question that settles all
# three: would the server accept this?
_token_rejected_because() {
    python - "$1" "$2" "${MCP_AUDIENCE:-}" <<'PY'
import sys

import jwt

token_path, public_path, audience = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    token = open(token_path, encoding="utf-8").read().strip()
    public = open(public_path, encoding="utf-8").read()
except OSError as exc:
    print(f"{exc.filename} cannot be read")
    raise SystemExit(0)

# Mirrors src/auth/verifier.py, so a token that passes here passes there.
try:
    jwt.decode(
        token,
        public,
        algorithms=["EdDSA", "ES256", "RS256"],
        audience=audience or None,
        options={"verify_aud": bool(audience)},
    )
except jwt.ExpiredSignatureError:
    print("it has expired")
except jwt.InvalidAudienceError:
    print(f"it was signed for a different audience than {audience!r}")
except jwt.InvalidSignatureError:
    print("it was signed by a key that is no longer on file")
except jwt.PyJWTError as exc:
    print(f"it is unusable ({type(exc).__name__})")
PY
}

cmd_provision() {
    local kid="${PROVISION_KID:-}"
    local has_scope=0
    local -a cli_args=()
    local -a env_args=()

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --kid)
                [ "$#" -ge 2 ] || die "--kid needs a value"
                kid="$2"
                shift 2
                ;;
            --kid=*)
                kid="${1#--kid=}"
                shift
                ;;
            --scope | --scope=*)
                has_scope=1
                cli_args+=("$1")
                shift
                ;;
            *)
                cli_args+=("$1")
                shift
                ;;
        esac
    done

    # Compose is easier to read with scalars than with a long command, so the
    # common knobs get an environment spelling too. Flags come last, so an
    # explicit --lifetime on the command line still wins.
    if [ -n "${PROVISION_SCOPES:-}" ]; then
        local -a scopes=()
        IFS=',' read -ra scopes <<<"$PROVISION_SCOPES"
        local scope
        for scope in "${scopes[@]}"; do
            scope="${scope//[[:space:]]/}"
            [ -n "$scope" ] || continue
            env_args+=(--scope "$scope")
            has_scope=1
        done
    fi
    if [ -n "${PROVISION_LIFETIME:-}" ]; then
        env_args+=(--lifetime "$PROVISION_LIFETIME")
    fi
    # The audience is checked on every request, so a token signed against the
    # default while the server was given one of its own verifies as forged.
    if [ -n "${MCP_AUDIENCE:-}" ]; then
        env_args+=(--audience "$MCP_AUDIENCE")
    fi

    [ -n "$kid" ] || die \
        "provision needs --kid (or PROVISION_KID): the agent's name, and the" \
        "filename its public key takes."
    [ "$has_scope" -eq 1 ] || die \
        "no --scope given (or PROVISION_SCOPES), so this token could call" \
        "nothing. Name the tools the agent may use, e.g. --scope" \
        "list_containers --scope get_columns."

    mkdir -p "$KEYS_DIR" "$PRIVATE_DIR" "$OUT_DIR"

    local private_key="$PRIVATE_DIR/$kid.pem"
    local token_file="$OUT_DIR/$kid.jwt"

    # --if-missing because `docker compose up` runs this again on every start,
    # and a fresh pair would silently invalidate every token already handed out.
    mcp-connector token keygen \
        --kid "$kid" \
        --keys-dir "$KEYS_DIR" \
        --out "$private_key" \
        --if-missing >&2

    local reason=""
    if is_true "${PROVISION_FORCE:-}"; then
        reason="PROVISION_FORCE is set"
    elif [ ! -e "$token_file" ]; then
        reason="there is no token yet"
    else
        reason="$(_token_rejected_because "$token_file" "$KEYS_DIR/$kid.pub")"
    fi

    if [ -z "$reason" ]; then
        say "token         $token_file   still verifies against $KEYS_DIR/$kid.pub," \
            "keeping it (PROVISION_FORCE=1 to sign a new one)"
    else
        say "signing a token: $reason"
        # --out keeps the token off stdout: it belongs in a file with 0600 on
        # it, not in a compose log that anything can scroll back through.
        mcp-connector token issue \
            --key "$private_key" \
            --kid "$kid" \
            "${env_args[@]}" \
            "${cli_args[@]}" \
            --out "$token_file"
        chmod 600 "$token_file"
        say ""
        say "token         $token_file   hand this to the agent; it is a credential"
        say "signing key   $private_key   keep this off the serving container"
        say "public key    $KEYS_DIR/$kid.pub   the server reads this"
        say "revoke with   rm $KEYS_DIR/$kid.pub"
    fi

    # Regenerated even when the token was kept, so that a change to the server's
    # configuration — an export directory added, a staging database removed —
    # reaches the skill without anyone having to remember to re-issue.
    PROVISION_KID="$kid" PROVISION_TOKEN_FILE="$token_file" MCP_OUT_DIR="$OUT_DIR" \
        python "$PROVISION_HOME/provision.py"
}

# --------------------------------------------------------------- dispatch ---

if [ "$#" -eq 0 ]; then set -- serve; fi

case "$1" in
    serve)
        shift
        cmd_serve "$@"
        ;;
    provision)
        shift
        cmd_provision "$@"
        ;;
    # `docker run <image> --engine sqlite …` reads as serving, not as a command.
    -*)
        cmd_serve "$@"
        ;;
    token)
        exec mcp-connector "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
