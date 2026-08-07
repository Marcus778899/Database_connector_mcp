"""
Roles: the named sets of grants a token can be cut from.

A token already carries everything needed to describe a job — tools, databases,
container globs, whether raw rows are allowed — but the only way to say any of
it was a flag on `token issue`, so in practice every deployment issued the same
one-size token and lived with it. A role is that list of flags, written down
once, under a name, in a file that can be reviewed and diffed.

The file is data, not policy: nothing here decides whether a caller may do
something. It decides what goes into a token, and `src.auth.permissions` is
still the only thing that reads a token and answers questions. A roles file
that granted a tool the server does not serve would produce a token that can
call nothing extra — which is why the tool names are checked against
`src.core.tools` at load time rather than trusted.

TOML rather than YAML because `tomllib` is in the standard library, and this
module is imported by `docker/provision.py`, which runs before anything is
guaranteed to be installed.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.tools import TOOL_NAMES

# Where a roles file is looked for when nobody says. The first is the image's
# own copy; the second is a checkout, so the CLI works without a container.
DEFAULT_ROLES_PATHS: tuple[Path, ...] = (
    Path("/etc/mcp/roles.toml"),
    Path(__file__).resolve().parent.parent.parent / "docker" / "roles.toml",
)

ENV_ROLES_FILE = "MCP_ROLES_FILE"

# Fields a role may set. Listed so a typo is an error rather than a setting
# that silently does nothing — the failure mode of a permissions file that
# ignores what it does not recognise is the worst one available.
_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "description",
        "extends",
        "tools",
        "add_tools",
        "drop_tools",
        "databases",
        "containers",
        "allow_raw_sample",
        "annotate_as_human",
        "lifetime",
    }
)
_CONTAINER_KEYS: frozenset[str] = frozenset({"allow", "deny"})


class RoleError(Exception):
    """The roles file cannot be read, or does not describe usable roles."""


@dataclass(frozen=True)
class Role:
    """One role, with inheritance already resolved."""

    name: str
    description: str = ""
    tools: tuple[str, ...] = ()
    databases: tuple[str, ...] = ()
    allow_containers: tuple[str, ...] = ()
    deny_containers: tuple[str, ...] = ()
    allow_raw_sample: bool = False
    annotate_as_human: bool = False
    # None means "whatever the issuer's default is", so a role does not have to
    # have an opinion about how long its tokens live.
    lifetime: str | None = None

    def issue_kwargs(self) -> dict[str, Any]:
        """The arguments `src.auth.issue.issue_token` takes for this role."""
        return {
            "scopes": list(self.tools),
            "databases": list(self.databases),
            "allow_containers": list(self.allow_containers),
            "deny_containers": list(self.deny_containers),
            "allow_raw_sample": self.allow_raw_sample,
            "annotate_as_human": self.annotate_as_human,
        }


@dataclass
class _Draft:
    """A role as written, before `extends` is applied."""

    name: str
    raw: dict[str, Any]
    extends: str | None = None
    resolved: Role | None = field(default=None)


def find_roles_file(path: str | Path | None = None) -> Path:
    """
    The roles file to read: what was asked for, then the environment, then the
    places one is shipped to.
    """
    if path is not None:
        found = Path(path)
        if not found.is_file():
            raise RoleError(f"no roles file at {found}")
        return found

    from_env = os.environ.get(ENV_ROLES_FILE)
    if from_env:
        found = Path(from_env)
        if not found.is_file():
            raise RoleError(f"{ENV_ROLES_FILE} points at {found}, which is not a file")
        return found

    for candidate in DEFAULT_ROLES_PATHS:
        if candidate.is_file():
            return candidate
    looked = ", ".join(str(candidate) for candidate in DEFAULT_ROLES_PATHS)
    raise RoleError(
        f"no roles file found (looked in {looked}). Point {ENV_ROLES_FILE} at one, "
        "or mount it into the container."
    )


def load_roles(path: str | Path | None = None) -> dict[str, Role]:
    """
    Every role in the file, with `extends` resolved and tool names checked.

    Order is the file's own, because `role list` reads better when the roles
    come out in the order somebody chose to write them.
    """
    source = find_roles_file(path)
    try:
        document = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RoleError(f"cannot read {source}: {exc}") from exc

    table = document.get("roles")
    if not isinstance(table, dict) or not table:
        raise RoleError(f"{source} defines no [roles.<name>] sections")

    drafts: dict[str, _Draft] = {}
    for name, body in table.items():
        if not isinstance(body, dict):
            raise RoleError(
                f"[roles.{name}] should be a table, not {type(body).__name__}"
            )
        unknown = sorted(set(body) - _KNOWN_KEYS)
        if unknown:
            allowed = ", ".join(sorted(_KNOWN_KEYS))
            raise RoleError(
                f"[roles.{name}] sets {', '.join(unknown)}, which mean nothing here. "
                f"Known settings: {allowed}"
            )
        extends = body.get("extends")
        if extends is not None and not isinstance(extends, str):
            raise RoleError(f"[roles.{name}] extends should be a role name")
        drafts[str(name)] = _Draft(name=str(name), raw=body, extends=extends)

    for draft in drafts.values():
        _resolve(draft, drafts, source, trail=())

    return {name: draft.resolved for name, draft in drafts.items() if draft.resolved}


def _resolve(
    draft: _Draft, drafts: dict[str, _Draft], source: Path, *, trail: tuple[str, ...]
) -> Role:
    if draft.resolved is not None:
        return draft.resolved
    if draft.name in trail:
        cycle = " -> ".join((*trail, draft.name))
        raise RoleError(f"roles inherit in a circle: {cycle}")

    parent: Role | None = None
    if draft.extends:
        if draft.extends not in drafts:
            known = ", ".join(sorted(drafts))
            raise RoleError(
                f"[roles.{draft.name}] extends {draft.extends!r}, which is not a role "
                f"in {source}. Known roles: {known}"
            )
        parent = _resolve(
            drafts[draft.extends], drafts, source, trail=(*trail, draft.name)
        )

    draft.resolved = _build(draft.name, draft.raw, parent)
    return draft.resolved


def _build(name: str, raw: dict[str, Any], parent: Role | None) -> Role:
    base = parent or Role(name=name)

    if "tools" in raw and ("add_tools" in raw or "drop_tools" in raw):
        raise RoleError(
            f"[roles.{name}] sets both tools and add_tools/drop_tools. `tools` "
            "replaces the inherited list; use one or the other so the result is "
            "readable without holding the parent in your head."
        )

    if "tools" in raw:
        tools = list(_strings(name, "tools", raw["tools"]))
    else:
        tools = list(base.tools)
        for extra in _strings(name, "add_tools", raw.get("add_tools", [])):
            if extra not in tools:
                tools.append(extra)
        dropped = set(_strings(name, "drop_tools", raw.get("drop_tools", [])))
        unheard = sorted(dropped - set(tools))
        if unheard:
            # A drop that removes nothing is a rename or a typo upstream, and
            # silently doing nothing is how a role ends up broader than it reads.
            raise RoleError(
                f"[roles.{name}] drops {', '.join(unheard)}, which it does not "
                f"inherit from {base.name!r}"
            )
        tools = [tool for tool in tools if tool not in dropped]

    unknown = sorted(set(tools) - TOOL_NAMES)
    if unknown:
        raise RoleError(
            f"[roles.{name}] names {', '.join(unknown)}, which this server has no "
            "tool for. A scope that matches no tool grants nothing, so this is "
            "almost always a typo."
        )
    if not tools:
        raise RoleError(
            f"[roles.{name}] grants no tools, so its token could call nothing."
        )

    containers = raw.get("containers", {})
    if not isinstance(containers, dict):
        raise RoleError(f"[roles.{name}] containers should be a table")
    unknown_keys = sorted(set(containers) - _CONTAINER_KEYS)
    if unknown_keys:
        raise RoleError(
            f"[roles.{name}] containers sets {', '.join(unknown_keys)}; only "
            "allow and deny mean anything"
        )

    return Role(
        name=name,
        description=str(raw.get("description", base.description)),
        tools=tuple(tools),
        databases=tuple(
            _strings(name, "databases", raw["databases"])
            if "databases" in raw
            else base.databases
        ),
        allow_containers=tuple(
            _strings(name, "containers.allow", containers["allow"])
            if "allow" in containers
            else base.allow_containers
        ),
        deny_containers=tuple(
            _strings(name, "containers.deny", containers["deny"])
            if "deny" in containers
            else base.deny_containers
        ),
        allow_raw_sample=_flag(name, "allow_raw_sample", raw, base.allow_raw_sample),
        annotate_as_human=_flag(name, "annotate_as_human", raw, base.annotate_as_human),
        lifetime=str(raw["lifetime"]) if "lifetime" in raw else base.lifetime,
    )


def _strings(role: str, key: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RoleError(f"[roles.{role}] {key} should be a list of strings")
    return tuple(value)


def _flag(role: str, key: str, raw: dict[str, Any], inherited: bool) -> bool:
    if key not in raw:
        return inherited
    value = raw[key]
    if not isinstance(value, bool):
        raise RoleError(f"[roles.{role}] {key} should be true or false")
    return value
