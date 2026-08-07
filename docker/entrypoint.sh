#!/usr/bin/env bash
#
# Two roles out of one image:
#
#   provision   one-shot. Makes a key pair per identity and signs each one a
#               token, then exits. The private halves are written to /private,
#               which the serving container does not mount: a server that
#               cannot sign is a server that, once taken, still cannot issue
#               itself a token.
#   serve       long-running. Reads /keys (public halves only) and serves.
#
# An identity is `<kid>=<role>`: the name the token speaks for, and the role in
# docker/roles.toml that says what it may do. Several are the normal case — a
# `de` that runs the scans and a `pm` that reads the result are not the same
# credential and should not be.
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
    local -a cli_args=()
    local cli_kid=""
    local cli_role=""
    local has_scope=0

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --kid)
                [ "$#" -ge 2 ] || die "--kid needs a value"
                cli_kid="$2"
                shift 2
                ;;
            --kid=*)
                cli_kid="${1#--kid=}"
                shift
                ;;
            --role)
                [ "$#" -ge 2 ] || die "--role needs a value"
                cli_role="$2"
                shift 2
                ;;
            --role=*)
                cli_role="${1#--role=}"
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

    mkdir -p "$KEYS_DIR" "$PRIVATE_DIR" "$OUT_DIR"

    # `--kid` on the command line provisions that one identity and nothing else:
    # `docker compose run --rm provision --kid alice --role pm` is how a person
    # gets added without re-running the whole set.
    if [ -n "$cli_kid" ]; then
        _provision_one "$cli_kid" "$cli_role" "$has_scope" "${cli_args[@]}"
        return
    fi

    local -a identities=()
    _collect_identities identities
    [ "${#identities[@]}" -gt 0 ] || die \
        "nothing to provision. Set PROVISION_IDENTITIES to <kid>=<role> pairs," \
        "e.g. PROVISION_IDENTITIES=de=de,pm=pm — \`role list\` shows the roles."

    local pair
    for pair in "${identities[@]}"; do
        _provision_one "${pair%%=*}" "${pair#*=}" 0
    done
}

# The identities to provision, as `<kid>=<role>` words in the named array.
#
# PROVISION_KID and PROVISION_SCOPES came first and are still honoured: a
# deployment that set them keeps working, with the scopes standing in for a
# role. New ones should use PROVISION_IDENTITIES, which is the only spelling
# that can name more than one agent.
_collect_identities() {
    local -n _out="$1"
    _out=()

    if [ -n "${PROVISION_IDENTITIES:-}" ]; then
        local -a raw=()
        IFS=',' read -ra raw <<<"$PROVISION_IDENTITIES"
        local item kid role
        for item in "${raw[@]}"; do
            item="${item//[[:space:]]/}"
            [ -n "$item" ] || continue
            kid="${item%%=*}"
            role="${item#*=}"
            [ "$kid" != "$item" ] || die \
                "PROVISION_IDENTITIES wants <kid>=<role> pairs; got $item. The kid" \
                "is the agent's name, the role is a section in the roles file."
            [ -n "$kid" ] && [ -n "$role" ] || die \
                "PROVISION_IDENTITIES entry $item has an empty half"
            _out+=("$kid=$role")
        done
        return
    fi

    if [ -n "${PROVISION_KID:-}" ]; then
        # No role: the scopes below carry the grants, as they used to.
        _out+=("${PROVISION_KID}=")
    fi
}

# Sign for one identity and write its bundle.
#
#   $1 kid, $2 role (may be empty), $3 whether --scope was given on the command
#   line, $4… extra arguments passed straight to `token issue`
_provision_one() {
    local kid="$1"
    local role="$2"
    local has_scope="$3"
    shift 3
    local -a extra_args=("$@")
    local -a env_args=()

    [ -n "$kid" ] || die "an identity needs a name"

    if [ -n "$role" ]; then
        env_args+=(--role "$role")
    fi

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

    [ -n "$role" ] || [ "$has_scope" -eq 1 ] || die \
        "$kid was given neither a role nor a scope, so its token could call" \
        "nothing. Name a role — \`role list\` shows them — or pass --scope."

    # Everything for one agent under one directory, the token included: an
    # INSTALL.md telling you to \`cat\` a file that turned out to be a level up
    # is a bad first five minutes.
    local bundle="$OUT_DIR/$kid"
    local private_key="$PRIVATE_DIR/$kid.pem"
    local token_file="$bundle/$kid.jwt"
    mkdir -p "$bundle"

    say ""
    say "=== $kid${role:+  ($role)}"

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
            "${extra_args[@]}" \
            --out "$token_file"
        chmod 600 "$token_file"
        say ""
        say "token         $token_file   hand this to the agent; it is a credential"
        say "signing key   $private_key   keep this off the serving container"
        say "public key    $KEYS_DIR/$kid.pub   the server reads this"
        say "revoke with   rm $KEYS_DIR/$kid.pub"
    fi

    # Regenerated even when the token was kept, so that a change to the server's
    # configuration — an export directory added, a staging database removed, a
    # role edited — reaches the skill without anyone having to remember to
    # re-issue.
    PROVISION_KID="$kid" \
        PROVISION_ROLE="$role" \
        PROVISION_TOKEN_FILE="$token_file" \
        MCP_OUT_DIR="$OUT_DIR" \
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
    # The CLI's own subcommands, passed straight through. Two are worth knowing
    # about: `role list` answers "what can I put in PROVISION_IDENTITIES", and
    # `test-connection` answers "why will the server not start" — which cannot
    # be asked of the server itself, because it is not running.
    token | role | test-connection)
        exec mcp-connector "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
