"""
Every tool this server can register, as data.

Not the implementations — those stay in `src/server.py`, where they have the
adapter, the audit trail and the permission check to hand. This is the part
three other places need and none of them should be guessing at:

  * `src.core.roles`, to refuse a role naming a tool that does not exist. A
    typo in a scope is otherwise invisible until an agent calls the tool and
    is told it may not;
  * `docker/provision.py`, to write a SKILL.md listing what is actually served
    — it used to parse the AST of server.py for this, which worked but meant
    the document and the roles were reading the catalog from different places;
  * `main.build`, to say which tools a given configuration leaves off.

Deliberately importable with nothing installed but the standard library, and
importing no adapter. `provision` runs in an image built for one engine and
must not drag in the drivers of the others.

`tests/core/test_tools.py` asserts this list matches the `@mcp.tool` functions
in `src/server.py`, so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ToolGroup(StrEnum):
    """
    Which half of the server a tool belongs to.

    `CATALOG` asks the source directly and is always served. `INVENTORY` reads
    and writes the staging database, and is served only when there is one.
    """

    CATALOG = "catalog"
    INVENTORY = "inventory"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    group: ToolGroup
    # One line, for a table of contents. The full docstring is what the MCP
    # client already shows the model; this is for a human skimming a role.
    summary: str
    # True where the tool changes something. Nothing here writes to the source
    # database — `inventory_annotate` writes to the inventory, and that is the
    # only write there is.
    writes: bool = False
    # Served only when an export directory is configured.
    needs_export_dir: bool = False
    # Live statistics, or a scan, that make the source do real work.
    costly: bool = False

    @property
    def needs_staging(self) -> bool:
        return self.group is ToolGroup.INVENTORY


TOOLS: tuple[ToolSpec, ...] = (
    # ---- catalog: asked of the source, every call ----
    ToolSpec(
        name="list_databases",
        group=ToolGroup.CATALOG,
        summary="Databases this connection can inventory.",
    ),
    ToolSpec(
        name="list_containers",
        group=ToolGroup.CATALOG,
        summary="One page of tables / views / collections.",
    ),
    ToolSpec(
        name="get_schema",
        group=ToolGroup.CATALOG,
        summary="Columns of one container, with key and nullability flags.",
    ),
    ToolSpec(
        name="get_sample",
        group=ToolGroup.CATALOG,
        summary="A few rows, masked unless the caller was granted otherwise.",
    ),
    ToolSpec(
        name="profile_column",
        group=ToolGroup.CATALOG,
        summary="One statistic about one column, computed now.",
        costly=True,
    ),
    # ---- inventory: read and written through the staging database ----
    ToolSpec(
        name="inventory_start",
        group=ToolGroup.INVENTORY,
        summary="Start a background catalog scan and return its job id.",
        writes=True,
        costly=True,
    ),
    ToolSpec(
        name="inventory_status",
        group=ToolGroup.INVENTORY,
        summary="How far a scan has got, and why it stopped.",
    ),
    ToolSpec(
        name="inventory_cancel",
        group=ToolGroup.INVENTORY,
        summary="Stop a scan after the container in flight.",
        writes=True,
    ),
    ToolSpec(
        name="inventory_summary",
        group=ToolGroup.INVENTORY,
        summary="Counts over what has been inventoried.",
    ),
    ToolSpec(
        name="inventory_containers",
        group=ToolGroup.INVENTORY,
        summary="One page of inventoried containers, with their scan state.",
    ),
    ToolSpec(
        name="inventory_columns",
        group=ToolGroup.INVENTORY,
        summary="One page of a container's recorded columns, in ordinal order.",
    ),
    ToolSpec(
        name="inventory_relationships",
        group=ToolGroup.INVENTORY,
        summary="Every foreign key in the inventory, as edges.",
    ),
    ToolSpec(
        name="inventory_changes",
        group=ToolGroup.INVENTORY,
        summary="What the upstream schema did between scans, newest first.",
    ),
    ToolSpec(
        name="inventory_search",
        group=ToolGroup.INVENTORY,
        summary="Containers and columns whose name or description mentions a keyword.",
    ),
    ToolSpec(
        name="inventory_annotate",
        group=ToolGroup.INVENTORY,
        summary="Describe what an inventoried table and its columns actually hold.",
        writes=True,
    ),
    ToolSpec(
        name="inventory_export",
        group=ToolGroup.INVENTORY,
        summary="Write the whole inventory to a file and return only its path.",
        needs_export_dir=True,
    ),
)

BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}
TOOL_NAMES: frozenset[str] = frozenset(BY_NAME)


def served(*, staging: bool, export_dir: bool) -> tuple[ToolSpec, ...]:
    """
    What a server with this configuration actually registers.

    Mirrors the two gates in `src/server.py`: no staging database means no
    inventory tools at all, and no export directory means `inventory_export`
    is not registered rather than registered and failing on every call.
    """
    return tuple(
        spec
        for spec in TOOLS
        if (staging or not spec.needs_staging)
        and (export_dir or not spec.needs_export_dir)
    )


def unknown_names(names: object) -> tuple[str, ...]:
    """The given names that are not tools, in the order given."""
    if not isinstance(names, (list, tuple)):
        return ()
    return tuple(
        str(name) for name in names if isinstance(name, str) and name not in TOOL_NAMES
    )
