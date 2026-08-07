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

Two things are read rather than assumed:

  * the tool list comes from the AST of `src/server.py`, so it cannot drift
    from what the server actually registers;
  * the grants come from the token that was just signed, so the skill and the
    credential can never disagree.

Run by entrypoint.sh after `token issue`. Opens no database and contacts no
server.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# The transports that mean "a client connects to a URL" rather than "a client
# spawns the process". They need different artifacts, not different values in
# the same artifact.
NETWORK_TRANSPORTS = frozenset({"http", "streamable-http", "sse"})

# Registered inside `_register_inventory_tools`, and only reached when an export
# directory was configured — src/server.py returns before it otherwise.
NEEDS_EXPORT_DIR = "inventory_export"

# How a catalog too large to read should be approached, in the order a task
# should try them. Kept here rather than in the template because the rows have
# to be dropped when the tool behind them is not served.
CATALOG_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("how big is this", "inventory_summary", "counts only, a few hundred bytes"),
    ("where is the thing I mean", "inventory_search", "narrow hits, no statistics"),
    (
        "what is in this table",
        "inventory_columns",
        "one page; pass `include_profile=False` on a wide one",
    ),
    (
        "all of it",
        "inventory_export",
        "**a file** — only its path comes back, never the contents",
    ),
)


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


@dataclass(frozen=True)
class Tool:
    name: str
    summary: str
    inventory: bool


# ------------------------------------------------------------ what is served ---


def _server_source() -> Path:
    """
    Locate `src/server.py` without importing it.

    `find_spec` imports the parent package only, which is a namespace package
    here and therefore free. Importing the module itself would drag in every
    adapter, and the slim image deliberately lacks some of their drivers.
    """
    try:
        spec = importlib.util.find_spec("src.server")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin:
        return Path(spec.origin)

    # Running from a checkout rather than the image.
    local = Path(__file__).resolve().parent.parent / "src" / "server.py"
    if local.is_file():
        return local
    raise ProvisionError("cannot find src/server.py to read the tool list from")


def _is_tool_decorator(node: ast.expr) -> bool:
    """Matches `@mcp.tool`, which is how every tool in server.py is registered."""
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr == "tool"


def _summarise(doc: str | None) -> str:
    """
    The first sentence of the docstring, on one line.

    The whole docstring is what the MCP client already shows the model; this is
    for a table of contents, where the job is to be scannable.
    """
    if not doc:
        return ""
    first = doc.strip().split("\n\n")[0]
    collapsed = " ".join(first.split())
    match = re.search(r"^(.+?[.;])(?:\s|$)", collapsed)
    if match is None:
        return collapsed
    # A docstring that runs on past a semicolon is being cut mid-thought, so the
    # clause that is kept ends as a sentence rather than as a dangling ';'.
    return match.group(1).rstrip(";") + "." if match.group(1).endswith(";") else match.group(1)


def discover_tools(source: Path) -> list[Tool]:
    """Every `@mcp.tool` in server.py, in the order it is registered."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    tools: list[Tool] = []
    for registrar in tree.body:
        if not isinstance(registrar, ast.FunctionDef):
            continue
        if not registrar.name.startswith("_register"):
            continue
        inventory = "inventory" in registrar.name
        for node in registrar.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_tool_decorator(dec) for dec in node.decorator_list):
                continue
            tools.append(
                Tool(
                    name=node.name,
                    summary=_summarise(ast.get_docstring(node)),
                    inventory=inventory,
                )
            )
    if not tools:
        raise ProvisionError(f"no @mcp.tool functions found in {source}")
    return tools


def served(tools: list[Tool], *, staging: bool, export: bool) -> list[Tool]:
    """Filter to what this server actually registers, matching main.py's gates."""
    return [
        tool
        for tool in tools
        if (staging or not tool.inventory)
        and (export or tool.name != NEEDS_EXPORT_DIR)
    ]


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
) -> dict[str, Any]:
    """
    The server entry a client adds.

    The token is referenced as `${VAR}` rather than written in: this file ends
    up in a project, and a bearer token pasted into a project is a credential in
    everything that project is ever copied into.
    """
    if transport in NETWORK_TRANSPORTS:
        return {
            "mcpServers": {
                server_name: {
                    "type": "http",
                    "url": url,
                    "headers": {"Authorization": f"Bearer ${{{token_env}}}"},
                }
            }
        }

    # stdio: the client spawns the container, so the connection variables have
    # to come from the client's own environment. `-e NAME` (no value) is docker's
    # pass-through form, which keeps the secret out of this file too.
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", connection_ref).strip("_").upper()
    args = ["run", "-i", "--rm", "-e", f"MCP_ENGINE={engine}"]
    args += ["-e", f"MCP_CONNECTION_REF={connection_ref}"]
    for suffix in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE", "URI", "PATH", "TOKEN"):
        args += ["-e", f"{prefix}_{suffix}"]
    args += [image, "serve"]
    return {"mcpServers": {server_name: {"type": "stdio", "command": "docker", "args": args}}}


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
        raise ProvisionError(f"template asks for unknown values: {sorted(set(missing))}")
    # A placeholder that resolved to nothing leaves the blank lines that framed
    # it behind, and three blank lines is a heading gap in rendered markdown.
    return re.sub(r"\n{3,}", "\n\n", output)


def catalog_guidance(callable_names: set[str]) -> str:
    """
    The routes through a catalog too large to read, and the advice that closes
    them. Both halves are generated: without the inventory tools the table has
    nothing in it, and the advice that would follow it — reach for an export —
    names a tool that is not there.
    """
    rows = [
        (want, tool, note) for want, tool, note in CATALOG_ROUTES if tool in callable_names
    ]
    if not rows:
        return (
            "This connection has no stored catalog to page through, so there is\n"
            "nothing to summarise or export. Work from `list_containers` and then\n"
            "`get_schema`, one container at a time, and keep what you learn in your\n"
            "own notes rather than asking again."
        )
    lines = ["| you want | ask for | what comes back |", "|---|---|---|"]
    lines += [f"| {want} | `{tool}` | {note} |" for want, tool, note in rows]
    closing = "\nWork outside in. Ask for counts, then narrow, then read one page."
    if NEEDS_EXPORT_DIR in callable_names:
        closing += (
            " Reach\nfor an export when the answer is \"all of it\" — the file is the "
            "deliverable\nand only its path comes back."
        )
    return "\n".join(lines) + "\n" + closing


def conditional_sections(callable_names: set[str]) -> str:
    """The fragments whose subject this token can actually reach."""
    chosen = [
        (TEMPLATE_DIR / filename).read_text(encoding="utf-8").strip()
        for tool, filename in CONDITIONAL_SECTIONS
        if tool in callable_names
    ]
    return "\n\n".join(chosen)


def tool_reference(tools: list[Tool], callable_names: set[str]) -> str:
    lines = ["## Tools you can call", ""]
    for group, label in ((False, "Catalog"), (True, "Inventory")):
        rows = [t for t in tools if t.inventory is group and t.name in callable_names]
        if not rows:
            continue
        lines += [f"### {label}", ""]
        lines += [f"- `{tool.name}` — {tool.summary}" for tool in rows]
        lines += [""]

    withheld = [t.name for t in tools if t.name not in callable_names]
    if withheld:
        lines += [
            "### Served, but not to you",
            "",
            "Calling one of these is refused; it is not an outage and not worth a",
            "retry: " + ", ".join(f"`{name}`" for name in withheld) + ".",
            "",
        ]
    return "\n".join(lines).rstrip()


def limits_section(claims: dict[str, Any]) -> str:
    lines = ["## What this connection may see", ""]
    databases = claims.get("databases") or []
    if databases:
        lines.append(
            "- Databases: " + ", ".join(f"`{name}`" for name in databases) + " only."
        )
    else:
        lines.append("- Databases: every database this connection reaches.")

    containers = claims.get("containers") or {}
    allow = containers.get("allow") or []
    deny = containers.get("deny") or []
    if allow:
        lines.append("- Containers: only " + ", ".join(f"`{p}`" for p in allow) + ".")
    if deny:
        lines.append(
            "- Never readable: "
            + ", ".join(f"`{p}`" for p in deny)
            + ". These are filtered out of listings too, so a name you cannot find "
            "may simply be one you are not shown."
        )
    if not allow and not deny:
        lines.append("- Containers: no name-based restriction.")

    if claims.get("allow_raw_sample") is True:
        lines.append(
            "- `get_sample(mask=False)` is granted. It is still the wrong default; "
            "use it when the task turns on the literal value."
        )
    else:
        lines.append(
            "- `get_sample(mask=False)` is **not** granted. Masked rows are all you "
            "get, and asking again will not change that."
        )
    if claims.get("annotate_as_human") is True:
        lines.append(
            "- Descriptions you write are recorded as a person's rather than an "
            "agent's. Be correspondingly careful."
        )
    return "\n".join(lines)


def expiry_line(claims: dict[str, Any]) -> str:
    exp = claims.get("exp")
    if not isinstance(exp, int):
        return ""
    when = datetime.fromtimestamp(exp, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"The credential expires on {when}; after that every call is refused until "
        "someone issues a new one."
    )


def install_notes(
    *,
    plugin_dir: Path,
    server_name: str,
    transport: str,
    url: str,
    token_env: str,
    token_file: Path,
) -> str:
    lines = [
        f"# Installing `{server_name}`",
        "",
        "## Claude Code",
        "",
        "```bash",
        f"cp -r {plugin_dir.name} ~/.claude/plugins/{plugin_dir.name}",
        "```",
        "",
        "Or point a marketplace at this directory. `.mcp.json` registers the",
        "server and `skills/` carries the usage guidance, so both arrive together.",
        "",
        "## Codex",
        "",
        "Append `codex.toml` to `~/.codex/config.toml`. Codex has no skills, so",
        f"pass `skills/*/SKILL.md` in as context, or paste it into `AGENTS.md`.",
        "",
        "## The credential",
        "",
    ]
    if transport in NETWORK_TRANSPORTS:
        lines += [
            f"The token is in `{token_file.name}` and is referenced as",
            f"`${{{token_env}}}` rather than written into the config. Export it",
            "from wherever you keep secrets:",
            "",
            "```bash",
            f"export {token_env}=$(cat {token_file.name})",
            "```",
            "",
            f"The server is expected at {url}. Change it in `.mcp.json` if you",
            "publish it elsewhere.",
        ]
    else:
        lines += [
            "Over stdio the client spawns the container and there is no token:",
            "whoever can start the process already holds the database credential.",
            "",
            "Check the `-e` flags in `.mcp.json` — they pass the connection",
            "variables through from your own environment, and a file-backed engine",
            "needs a `-v` mount adding for its source.",
        ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------ main ---


def main() -> int:
    kid = os.environ.get("PROVISION_KID", "").strip()
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
    image = os.environ.get("PROVISION_IMAGE") or "database-mcp-connector:slim"
    url = os.environ.get("PROVISION_PUBLIC_URL") or "http://localhost:8000/mcp"

    claims = token_claims(token_path.read_text(encoding="utf-8"))
    scopes = {s for s in claims.get("scopes", []) if isinstance(s, str)}

    tools = served(
        discover_tools(_server_source()),
        staging=bool(os.environ.get("MCP_STAGING_DB")),
        export=bool(os.environ.get("MCP_EXPORT_DIR")),
    )
    callable_names = {tool.name for tool in tools if tool.name in scopes}
    if not callable_names:
        print(
            f"warning: none of this token's scopes ({', '.join(sorted(scopes)) or 'none'}) "
            f"name a tool this server serves. Check --scope against the tool list.",
            file=sys.stderr,
        )

    slug = slugify(server_name)
    token_env = env_var_for(server_name)
    plugin_dir = out_dir / kid
    skill_dir = plugin_dir / "skills" / slug

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
    )

    skill = render(
        (TEMPLATE_DIR / "SKILL.md.tmpl").read_text(encoding="utf-8"),
        {
            "skill_slug": slug,
            "skill_description": (
                f"Explore the {engine} catalog behind {server_name}: what tables and "
                f"columns exist, what they mean, and how they join. Use when asked "
                f"about the data itself rather than about code."
            ),
            "server_name": server_name,
            "engine": engine,
            "agent": kid,
            "expiry_line": expiry_line(claims),
            "catalog_guidance": catalog_guidance(callable_names),
            "conditional_sections": conditional_sections(callable_names),
            "tool_reference": tool_reference(tools, callable_names),
            "limits": limits_section(claims),
        },
    )

    written = [
        (plugin_dir / ".claude-plugin" / "plugin.json", json.dumps(
            plugin_json(slug=slug, server_name=server_name, engine=engine), indent=2
        ) + "\n"),
        (plugin_dir / ".mcp.json", json.dumps(entry, indent=2) + "\n"),
        (skill_dir / "SKILL.md", skill),
        (plugin_dir / "codex.toml", codex_toml(
            server_name=server_name,
            transport=transport,
            url=url,
            token_env=token_env,
            entry=entry,
        )),
        (plugin_dir / "INSTALL.md", install_notes(
            plugin_dir=plugin_dir,
            server_name=server_name,
            transport=transport,
            url=url,
            token_env=token_env,
            token_file=token_path,
        )),
    ]
    for path, content in written:
        path.write_text(content, encoding="utf-8")

    # stderr throughout: this runs from the entrypoint, whose stdout may be a
    # JSON-RPC channel.
    print(f"\nplugin        {plugin_dir}", file=sys.stderr)
    for path, _ in written:
        print(f"  {path.relative_to(plugin_dir)}", file=sys.stderr)
    print(
        f"\n{len(callable_names)} of {len(tools)} served tools are in scope: "
        f"{', '.join(sorted(callable_names)) or 'none'}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProvisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
