"""
The tool registry has to match the tools.

`src/core/tools.py` is data about code that lives somewhere else, which is the
kind of thing that rots silently: a tool added to `server.py` and not to the
registry is a tool no role can grant, and one removed from `server.py` but left
in the registry is a name a role can grant that nothing answers to.

Reading `server.py` here rather than importing it, because importing drags in
every adapter and an image built for one engine has only one engine's drivers.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.core.tools import BY_NAME, TOOLS, ToolGroup, served, unknown_names

SERVER = Path(__file__).resolve().parents[2] / "src" / "server.py"


def _is_tool_decorator(node: ast.expr) -> bool:
    """Matches `@mcp.tool`, which is how every tool in server.py is registered."""
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr == "tool"


def _registered() -> list[tuple[str, ToolGroup]]:
    """Every `@mcp.tool` in server.py, and which registrar it sits in."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    found: list[tuple[str, ToolGroup]] = []
    for registrar in tree.body:
        if not isinstance(registrar, ast.FunctionDef):
            continue
        if not registrar.name.startswith("_register"):
            continue
        group = (
            ToolGroup.INVENTORY if "inventory" in registrar.name else ToolGroup.CATALOG
        )
        for node in ast.walk(registrar):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(_is_tool_decorator(dec) for dec in node.decorator_list):
                found.append((node.name, group))
    return found


def test_the_registry_names_exactly_the_registered_tools():
    registered = {name for name, _ in _registered()}
    listed = set(BY_NAME)

    assert registered, "found no @mcp.tool in server.py; the parser is wrong"
    assert listed - registered == set(), "registry names tools server.py does not serve"
    assert registered - listed == set(), "server.py serves tools the registry omits"


def test_every_tool_is_in_the_group_it_is_registered_under():
    for name, group in _registered():
        assert BY_NAME[name].group is group, f"{name} is in the wrong group"


def test_the_registry_keeps_the_order_of_registration():
    """The SKILL.md tool list is this order, and it is a deliberate one: the
    cheap questions come before the expensive ones."""
    assert [spec.name for spec in TOOLS] == [name for name, _ in _registered()]


def test_every_tool_has_a_summary():
    """It goes into a document a person reads to review a role."""
    for spec in TOOLS:
        assert spec.summary.strip(), f"{spec.name} has no summary"
        assert spec.summary.endswith("."), f"{spec.name}'s summary is not a sentence"


# ---- the gates ----


def test_without_a_staging_database_only_the_catalog_is_served():
    names = {spec.name for spec in served(staging=False, export_dir=False)}

    assert names == {spec.name for spec in TOOLS if spec.group is ToolGroup.CATALOG}


def test_the_export_needs_somewhere_to_write():
    with_export = {spec.name for spec in served(staging=True, export_dir=True)}
    without = {spec.name for spec in served(staging=True, export_dir=False)}

    assert "inventory_export" in with_export
    assert with_export - without == {"inventory_export"}


def test_an_export_directory_without_staging_still_serves_no_inventory():
    """`inventory_export` reads the staging database, so an export directory on
    its own buys nothing."""
    names = {spec.name for spec in served(staging=False, export_dir=True)}

    assert "inventory_export" not in names


# ---- checking names ----


@pytest.mark.parametrize(
    "names, expected",
    [
        (["get_schema", "nope"], ("nope",)),
        (["get_schema"], ()),
        ([], ()),
        # A scopes claim that is not a list grants nothing, and asking which of
        # its entries are unknown should not raise on the way to finding out.
        ("get_schema", ()),
        (None, ()),
    ],
)
def test_unknown_names_reports_what_is_not_a_tool(names: object, expected: tuple):
    assert unknown_names(names) == expected
