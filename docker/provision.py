"""
The client half of a provision: what an agent needs in order to reach a server
whose key it has just been given.

Generated rather than kept as a checked-in example, because the useful version
is specific to one deployment. Which tools exist depends on whether a staging
database and an export directory were configured; which of those the holder may
call depends on the scopes in its token. A hand-written SKILL.md would have to
describe every arrangement, and would therefore describe none of them — worse,
it would name tools that are not served, which an agent then spends turns
discovering.

Three things are read rather than assumed:

  * the tool list comes from `src.core.tools`, the same table the roles file is
    validated against, so a skill cannot name a tool the server has not got;
  * the grants come from the token that was just signed, so the skill and the
    credential can never disagree;
  * the role's own description comes from the roles file, so an agent is told
    what its role is *for* and not only what it may call.

Run by entrypoint.sh after `token issue`. Opens no database and contacts no
server.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Installed as a wheel in the image; a checkout needs the repository root on the
# path, since this file lives in docker/ rather than beside src/.
try:
    from src.core.engines import DISPLAY_NAMES, parse_engine
    from src.core.tools import ToolGroup, ToolSpec, served
except ImportError:  # pragma: no cover - exercised by running from a checkout
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.core.engines import DISPLAY_NAMES, parse_engine
    from src.core.tools import ToolGroup, ToolSpec, served

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# The language the generated documents are written in. Tool names, parameter
# names and claim names are identifiers and are never translated — a SKILL.md
# that localised `get_sample` would describe a tool that does not exist.
DEFAULT_LANG = "zh-TW"

# The transports that mean "a client connects to a URL" rather than "a client
# spawns the process". They need different artifacts, not different values in
# the same artifact.
NETWORK_TRANSPORTS = frozenset({"http", "streamable-http", "sse"})

# Registered inside `_register_inventory_tools`, and only reached when an export
# directory was configured — src/server.py returns before it otherwise.
NEEDS_EXPORT_DIR = "inventory_export"

# Prose that is only true when the tool it is about is reachable. Telling an
# agent how to sample responsibly when it cannot sample at all wastes context
# and, worse, reads as an invitation to try.
CONDITIONAL_SECTIONS: tuple[tuple[str, str], ...] = (
    ("get_sample", "fragment-sampling.md"),
    ("inventory_start", "fragment-scanning.md"),
    ("inventory_annotate", "fragment-descriptions.md"),
)


class ProvisionError(Exception):
    """The artifacts cannot be generated from what was given."""


def _truthy(raw: str | None) -> bool:
    """The spellings entrypoint.sh and main.py both accept."""
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_false(raw: str | None) -> bool:
    """
    Explicitly turned off, as opposed to not mentioned.

    Needed where the default is on: an unset variable and `PROVISION_INLINE_TOKEN=0`
    have to mean different things.
    """
    return (raw or "").strip().lower() in {"0", "false", "no", "off"}


# ------------------------------------------------------------ what is served ---


def load_phrases(lang: str) -> tuple[dict[str, Any], Path]:
    """
    The strings for one language, and the directory they came from.

    An unknown language is refused rather than quietly falling back to English:
    a deployment that set PROVISION_LANG=zh and got English would look like the
    setting did nothing.
    """
    directory = TEMPLATE_DIR / lang
    phrases = directory / "phrases.toml"
    if not phrases.is_file():
        available = ", ".join(
            sorted(entry.name for entry in TEMPLATE_DIR.iterdir() if entry.is_dir())
        )
        raise ProvisionError(
            f"no templates for PROVISION_LANG={lang!r}. Available: {available}"
        )
    try:
        return tomllib.loads(phrases.read_text(encoding="utf-8")), directory
    except tomllib.TOMLDecodeError as exc:
        raise ProvisionError(f"cannot read {phrases}: {exc}") from exc


# ------------------------------------------------------------------- the token ---


def token_claims(token: str) -> dict[str, Any]:
    """
    Read the payload of the token just signed.

    Deliberately unverified: this decides how to word a document, not whether to
    let anyone in, and the signature was made by this same process seconds ago.
    Nothing here may be copied into code that grants access.
    """
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ProvisionError("not a JWT; expected three dot-separated parts")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError) as exc:
        raise ProvisionError(f"cannot read the token payload: {exc}") from exc


# --------------------------------------------------------------- the artifacts ---


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "mcp-server"


def env_var_for(server_name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", server_name.upper()).strip("_") + "_TOKEN"


def mcp_json(
    *,
    server_name: str,
    transport: str,
    url: str,
    token_env: str,
    image: str,
    connection_ref: str,
    engine: str,
    inline_token: str | None = None,
) -> dict[str, Any]:
    """
    The server entry a client adds.

    The token is written in by default. `${VAR}` reads better and is what this
    used to do, but it asks two things at once: that the client expands
    variables, and that the shell which exported it is the one that started the
    client. A client launched from a desktop app satisfies neither, and what it
    sends is the literal placeholder — which the server rejects for its shape
    rather than for being the wrong token, so the error names neither the
    variable nor the cause.

    Nothing is lost by writing it in. The bundle already holds the same token as
    a file next to this one, so the directory is a credential either way, and
    both get 0600. `PROVISION_INLINE_TOKEN=0` goes back to the placeholder for
    a client that does expand it.
    """
    if transport in NETWORK_TRANSPORTS:
        bearer = inline_token if inline_token else f"${{{token_env}}}"
        return {
            "mcpServers": {
                server_name: {
                    "type": "http",
                    "url": url,
                    "headers": {"Authorization": f"Bearer {bearer}"},
                }
            }
        }

    # stdio: the client spawns the container, so the connection variables have
    # to come from the client's own environment. `-e NAME` (no value) is docker's
    # pass-through form, which keeps the secret out of this file too.
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", connection_ref).strip("_").upper()
    args = ["run", "-i", "--rm", "-e", f"MCP_ENGINE={engine}"]
    args += ["-e", f"MCP_CONNECTION_REF={connection_ref}"]
    for suffix in (
        "HOST",
        "PORT",
        "USER",
        "PASSWORD",
        "DATABASE",
        "URI",
        "PATH",
        "TOKEN",
    ):
        args += ["-e", f"{prefix}_{suffix}"]
    args += [image, "serve"]
    return {
        "mcpServers": {
            server_name: {"type": "stdio", "command": "docker", "args": args}
        }
    }


def plugin_json(*, slug: str, server_name: str, engine: str) -> dict[str, Any]:
    return {
        "name": slug,
        "description": (
            f"Read-only catalog access to the {engine} source behind {server_name}, "
            "with the guidance needed to explore it without flooding the context."
        ),
        "version": "0.1.0",
        "keywords": ["database", "catalog", "metadata", engine],
    }


def codex_toml(
    *, server_name: str, transport: str, url: str, token_env: str, entry: dict[str, Any]
) -> str:
    """
    The same server in Codex's spelling.

    Separate from mcp.json rather than converted from it: the two formats agree
    on the stdio shape and diverge on everything else, and a translation layer
    would hide that.
    """
    key = re.sub(r"[^a-z0-9_]+", "_", server_name.lower()).strip("_")
    lines = [
        "# Append to ~/.codex/config.toml",
        "",
        f"[mcp_servers.{key}]",
    ]
    if transport in NETWORK_TRANSPORTS:
        lines += [
            f'url = "{url}"',
            f'bearer_token_env_var = "{token_env}"',
            "",
            "# The HTTP form needs a Codex build with the rmcp client; if yours",
            "# rejects `url`, front the server with a stdio bridge instead.",
        ]
    else:
        server = entry["mcpServers"][server_name]
        args = ", ".join(f'"{arg}"' for arg in server["args"])
        lines += [f'command = "{server["command"]}"', f"args = [{args}]"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------- rendering ---


def yaml_scalar(text: str) -> str:
    """
    One line of free text, safe to drop into the SKILL.md frontmatter.

    A YAML plain scalar may not contain `: ` — colon then space — and a skill
    description is a sentence, so sooner or later it does. Unquoted, the parser
    reads the text after the colon as a nested mapping and the whole frontmatter
    fails; the client then sees a skill with no description, or no skill at all.

    Quoting happens here rather than in the template because the text comes from
    a phrases file that anyone may edit, and quoting it there would leave the
    escaping to whoever writes the sentence. `json.dumps` is exactly the right
    tool despite the name: a JSON string *is* a YAML double-quoted scalar, with
    the same escapes.
    """
    return json.dumps(" ".join(text.split()), ensure_ascii=False)


def render(template: str, values: dict[str, str]) -> str:
    """`{{name}}` and nothing else, so prose containing $ or ${} is left alone."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return values[key]

    output = re.sub(r"\{\{(\w+)\}\}", replace, template)
    if missing:
        raise ProvisionError(
            f"template asks for unknown values: {sorted(set(missing))}"
        )
    # A placeholder that resolved to nothing leaves the blank lines that framed
    # it behind, and three blank lines is a heading gap in rendered markdown.
    return re.sub(r"\n{3,}", "\n\n", output)


def catalog_guidance(phrases: dict, callable_names: set[str]) -> str:
    """
    The routes through a catalog too large to read, and the advice that closes
    them. Both halves are generated: without the inventory tools the table has
    nothing in it, and the advice that would follow it — reach for an export —
    names a tool that is not there.
    """
    section = phrases["catalog"]
    rows = [route for route in section["routes"] if route["tool"] in callable_names]
    if not rows:
        return section["empty"].strip()

    head = section["head"]
    lines = [
        "| " + " | ".join(head) + " |",
        "|" + "---|" * len(head),
    ]
    lines += [
        f"| {route['want']} | `{route['tool']}` | {route['gets']} |" for route in rows
    ]
    closing = section["closing"]
    if NEEDS_EXPORT_DIR in callable_names:
        # The separator is the language's, not a space: a space between two
        # sentences is an English convention, and one after `。` reads as a
        # typo in Chinese.
        closing += section.get("sentence_join", " ") + section["closing_export"]
    return "\n".join(lines) + "\n\n" + closing


def conditional_sections(directory: Path, callable_names: set[str]) -> str:
    """The fragments whose subject this token can actually reach."""
    chosen = [
        (directory / filename).read_text(encoding="utf-8").strip()
        for tool, filename in CONDITIONAL_SECTIONS
        if tool in callable_names
    ]
    return "\n\n".join(chosen)


def tool_reference(
    phrases: dict, tools: tuple[ToolSpec, ...], callable_names: set[str]
) -> str:
    section = phrases["tools"]
    lines = [section["heading"], ""]
    # Said before the list, not after it. The rest of this document is about
    # what to be careful of — cost, masking, the audit trail — and a model
    # reading only that tends to round "be deliberate" down to "I had better
    # not", then reports it as a permission it does not have. The list is the
    # grant; nothing here needs asking for twice.
    preamble = section.get("preamble")
    if preamble:
        lines += [preamble, ""]
    for group, label in (
        (ToolGroup.CATALOG, section["catalog"]),
        (ToolGroup.INVENTORY, section["inventory"]),
    ):
        rows = [
            spec
            for spec in tools
            if spec.group is group and spec.name in callable_names
        ]
        if not rows:
            continue
        # The registry's summaries are English, because they sit next to the
        # code. A language pack may override them; anything it does not name
        # falls back, so adding a tool never leaves a blank line in a document.
        summaries = section.get("summaries", {})
        lines += [f"### {label}", ""]
        lines += [
            f"- `{spec.name}` — {summaries.get(spec.name, spec.summary)}"
            for spec in rows
        ]
        lines += [""]

    withheld = [spec.name for spec in tools if spec.name not in callable_names]
    if withheld:
        names = ", ".join(f"`{name}`" for name in withheld)
        lines += [
            section["withheld_heading"],
            "",
            section["withheld_body"].format(names=names),
            "",
        ]
    return "\n".join(lines).rstrip()


def limits_section(phrases: dict, claims: dict[str, Any]) -> str:
    section = phrases["limits"]
    lines = [section["heading"], ""]

    databases = claims.get("databases") or []
    if databases:
        names = ", ".join(f"`{name}`" for name in databases)
        lines.append(section["databases_only"].format(names=names))
    else:
        lines.append(section["databases_all"])

    containers = claims.get("containers") or {}
    allow = containers.get("allow") or []
    deny = containers.get("deny") or []
    if allow:
        lines.append(
            section["containers_allow"].format(
                names=", ".join(f"`{pattern}`" for pattern in allow)
            )
        )
    if deny:
        lines.append(
            section["containers_deny"].format(
                names=", ".join(f"`{pattern}`" for pattern in deny)
            )
        )
    if not allow and not deny:
        lines.append(section["containers_none"])

    lines.append(
        section["raw_granted"]
        if claims.get("allow_raw_sample") is True
        else section["raw_denied"]
    )
    if claims.get("annotate_as_human") is True:
        lines.append(section["human"])
    return "\n".join(lines)


def expiry_line(phrases: dict, claims: dict[str, Any]) -> str:
    exp = claims.get("exp")
    if not isinstance(exp, int):
        return ""
    when = datetime.fromtimestamp(exp, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return phrases["skill"]["expiry"].format(when=when)


def role_line(phrases: dict, role: str, description: str) -> str:
    """
    What follows the agent's name: which role it is, and what that role is for.

    An agent that knows only its list of tools has to infer the job from the
    list. Naming the role is a sentence, and it is the sentence that stops a
    `pm` token from trying to run a scan because it looked useful.
    """
    if not role:
        return ""
    key = "role_line" if description else "role_line_plain"
    return phrases["skill"][key].format(role=role, role_description=description)


def role_description(role: str) -> str:
    """
    The role's own description, if the roles file can be read.

    Best effort: a missing or unreadable roles file is not a reason to fail a
    provision that has already signed a working token. The document is simply
    a little less specific.
    """
    if not role:
        return ""
    try:
        from src.core.roles import load_roles

        found = load_roles().get(role)
    except Exception as exc:  # noqa: BLE001 - a document, not a decision
        print(f"warning: cannot read the roles file ({exc})", file=sys.stderr)
        return ""
    return found.description if found else ""


def install_notes(
    *,
    phrases: dict,
    bundle: Path,
    agent: str,
    role: str,
    role_summary: str,
    server_name: str,
    transport: str,
    url: str,
    token_env: str,
    token_file: Path,
    inline_token: str | None = None,
) -> str:
    section = phrases["install"]
    parts = [
        section["title"].format(server_name=server_name, agent=agent),
        section["claude_code"].format(bundle=bundle.name).strip(),
        section["codex"].strip(),
    ]
    if transport in NETWORK_TRANSPORTS and inline_token:
        parts.append(
            section["credential_inline"].format(token_env=token_env, url=url).strip()
        )
    elif transport in NETWORK_TRANSPORTS:
        parts.append(
            section["credential_env"]
            .format(token_env=token_env, token_name=token_file.name, url=url)
            .strip()
        )
    else:
        parts.append(section["credential_stdio"].strip())

    if role:
        parts.append(
            section["role_note"]
            .format(
                agent=agent,
                role=role,
                role_description=role_summary or role,
            )
            .strip()
        )
    return "\n\n".join(parts) + "\n"


# ------------------------------------------------------------------------ main ---


def main() -> int:
    kid = os.environ.get("PROVISION_KID", "").strip()
    role = os.environ.get("PROVISION_ROLE", "").strip()
    token_path = Path(os.environ.get("PROVISION_TOKEN_FILE", ""))
    out_dir = Path(os.environ.get("MCP_OUT_DIR", "/out"))
    if not kid:
        raise ProvisionError("PROVISION_KID is not set")
    if not token_path.is_file():
        raise ProvisionError(f"no token at {token_path}")

    server_name = os.environ.get("MCP_SERVER_NAME") or "etl-agent-mcp"
    engine = os.environ.get("MCP_ENGINE") or "sqlite"
    transport = os.environ.get("MCP_TRANSPORT") or "stdio"
    connection_ref = os.environ.get("MCP_CONNECTION_REF") or "source"
    # The image tag carries the engine, because that is what it was built for.
    image = os.environ.get("PROVISION_IMAGE") or f"database-mcp-connector:{engine}"
    url = os.environ.get("PROVISION_PUBLIC_URL") or "http://localhost:8000/mcp"
    lang = os.environ.get("PROVISION_LANG", "").strip() or DEFAULT_LANG

    try:
        engine_display = DISPLAY_NAMES[parse_engine(engine)]
    except ValueError:
        engine_display = engine

    phrases, template_dir = load_phrases(lang)

    raw_token = token_path.read_text(encoding="utf-8").strip()
    claims = token_claims(raw_token)
    scopes = {s for s in claims.get("scopes", []) if isinstance(s, str)}
    # Written in by default. The `${VAR}` form needs the client to expand
    # variables *and* to have been started from the shell that exported them,
    # and a client launched from a desktop app satisfies neither — it sends the
    # placeholder as the bearer token, which the server rejects for its shape.
    # The bundle already holds the token as a file, so it is a credential
    # either way; the only thing the placeholder buys is a failure mode.
    inline = None if _is_false(os.environ.get("PROVISION_INLINE_TOKEN")) else raw_token

    tools = served(
        staging=bool(os.environ.get("MCP_STAGING_DB")),
        export_dir=bool(os.environ.get("MCP_EXPORT_DIR")),
    )
    callable_names = {spec.name for spec in tools if spec.name in scopes}
    if not callable_names:
        print(
            f"warning: none of {kid}'s scopes ({', '.join(sorted(scopes)) or 'none'}) "
            f"name a tool this server serves. Check the role against `role list`.",
            file=sys.stderr,
        )
    # A role that grants tools this server does not register is not an error —
    # a `de` token is still fine on a server with no staging database — but it
    # is worth saying, because the difference shows up as a refusal later.
    unserved = sorted(scopes - {spec.name for spec in tools})
    if unserved:
        print(
            f"note: {kid} is granted {', '.join(unserved)}, which this server does "
            f"not register (no staging database or export directory configured). "
            f"They are left out of its SKILL.md.",
            file=sys.stderr,
        )

    slug = slugify(server_name)
    token_env = env_var_for(server_name)
    plugin_dir = out_dir / kid
    skill_dir = plugin_dir / "skills" / slug
    summary = role_description(role)

    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    skill_dir.mkdir(parents=True, exist_ok=True)

    entry = mcp_json(
        server_name=server_name,
        transport=transport,
        url=url,
        token_env=token_env,
        image=image,
        connection_ref=connection_ref,
        engine=engine,
        inline_token=inline,
    )

    skill = render(
        (template_dir / "SKILL.md.tmpl").read_text(encoding="utf-8"),
        {
            "skill_slug": slug,
            # Already quoted — see `yaml_scalar`. The template must not add
            # quotes of its own.
            "skill_description": yaml_scalar(
                phrases["skill"]["description"].format(
                    server_name=server_name, engine_display=engine_display
                )
            ),
            "server_name": server_name,
            "engine": engine,
            "engine_display": engine_display,
            "agent": kid,
            "role_line": role_line(phrases, role, summary),
            "expiry_line": expiry_line(phrases, claims),
            "catalog_guidance": catalog_guidance(phrases, callable_names),
            "conditional_sections": conditional_sections(template_dir, callable_names),
            "tool_reference": tool_reference(phrases, tools, callable_names),
            "limits": limits_section(phrases, claims),
        },
    )

    written = [
        (
            plugin_dir / ".claude-plugin" / "plugin.json",
            json.dumps(
                plugin_json(slug=slug, server_name=server_name, engine=engine_display),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        ),
        (plugin_dir / ".mcp.json", json.dumps(entry, indent=2) + "\n"),
        (skill_dir / "SKILL.md", skill),
        (
            plugin_dir / "codex.toml",
            codex_toml(
                server_name=server_name,
                transport=transport,
                url=url,
                token_env=token_env,
                entry=entry,
            ),
        ),
        (
            plugin_dir / "INSTALL.md",
            install_notes(
                phrases=phrases,
                bundle=plugin_dir,
                agent=kid,
                role=role,
                role_summary=summary,
                server_name=server_name,
                transport=transport,
                url=url,
                token_env=token_env,
                token_file=token_path,
                inline_token=inline,
            ),
        ),
    ]
    for path, content in written:
        path.write_text(content, encoding="utf-8")
    if inline is not None:
        # It now holds a bearer token, so it gets a credential's permissions.
        (plugin_dir / ".mcp.json").chmod(0o600)

    # stderr throughout: this runs from the entrypoint, whose stdout may be a
    # JSON-RPC channel.
    print(f"\nbundle        {plugin_dir}", file=sys.stderr)
    for path, _ in written:
        print(f"  {path.relative_to(plugin_dir)}", file=sys.stderr)
    print(
        f"\n{len(callable_names)} of {len(tools)} served tools are in scope: "
        f"{', '.join(sorted(callable_names)) or 'none'}",
        file=sys.stderr,
    )
    if transport in NETWORK_TRANSPORTS and inline is None:
        # The step that is easy to miss, and whose failure surfaces as a
        # complaint about the token's shape rather than about the variable
        # never having been set.
        print(
            f"\n.mcp.json refers to the token as ${{{token_env}}}. Export it in the "
            f"shell you start the client from:\n"
            f"    export {token_env}=$(cat {token_path})\n"
            f"A client that does not expand ${{...}} — or one started from a desktop "
            f"app, which never saw that shell — needs the token written in instead. "
            f"Unset PROVISION_INLINE_TOKEN to go back to that. See INSTALL.md.",
            file=sys.stderr,
        )
    elif transport in NETWORK_TRANSPORTS:
        print(
            "\n.mcp.json carries the token itself and is a credential (0600). "
            "Gitignore it.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
