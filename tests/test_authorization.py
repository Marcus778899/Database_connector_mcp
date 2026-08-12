"""
What a key may see, driven through the tools.

`tests/auth/test_permissions.py` covers the rules themselves. This is the half
that matters more: that every tool actually asks. A rule nothing consults is
not a restriction, and the way this goes wrong is one tool quietly not checking
— which is why the sweep at the bottom exists.
"""

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src import server as server_module
from src.adapter.sqlite import SqliteAdapter
from src.core.config import ServerConfig, SourceEngine
from src.server import build_server
from src.service.audit import AuditLogger
from src.service.inventory import InventoryService
from src.service.pool import SingleAdapter
from src.service.staging import StagingStore


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, note TEXT);
        CREATE TABLE dim_product (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE users_pii (id INTEGER PRIMARY KEY, ssn TEXT);
        """
    )
    conn.execute("INSERT INTO users (email, note) VALUES ('alice@example.com','hi')")
    conn.execute("INSERT INTO dim_product (name) VALUES ('widget')")
    conn.execute("INSERT INTO users_pii (ssn) VALUES ('123-45-6789')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def served(source: Path, tmp_path: Path) -> Iterator[Any]:
    config = ServerConfig(
        server_name="authz", engine=SourceEngine.SQLITE, export_dir=tmp_path / "exports"
    )
    (tmp_path / "exports").mkdir()
    adapter = SqliteAdapter(source)
    with StagingStore(tmp_path / "staging.db", source_path=source) as store:
        service = InventoryService(SingleAdapter(adapter), store)
        mcp = build_server(config, adapter, inventory=service)
        service.wait(service.start("main"), timeout=20)
        yield mcp
        service.close()
    adapter.close()


def _as(monkeypatch: pytest.MonkeyPatch, scopes: list[str], **claims) -> None:
    """Call as a key carrying these scopes and claims."""
    monkeypatch.setattr(
        server_module,
        "get_access_token",
        lambda: SimpleNamespace(
            scopes=scopes, claims=claims, subject="pm-alice", client_id="pm-alice"
        ),
    )


def _call(mcp, tool: str, args: dict[str, Any] | None = None) -> Any:
    async def run() -> Any:
        async with Client(mcp) as client:
            return (await client.call_tool(tool, args or {})).data

    return asyncio.run(run())


ALL_TOOLS = [
    "list_databases",
    "list_containers",
    "get_schema",
    "get_sample",
    "profile_column",
    "inventory_start",
    "inventory_summary",
    "inventory_containers",
    "inventory_columns",
    "inventory_search",
    "inventory_relationships",
    "inventory_changes",
    "inventory_annotate",
    "inventory_export",
]


# ---- masking ----


def test_a_sample_is_masked_by_default(served):
    """These rows go into an agent's context and from there into everything
    that context touches."""
    (row,) = _call(served, "get_sample", {"container": "users", "limit": 1})

    assert row["email"] == "a***@***.com"
    assert row["note"] == "hi", "an ordinary column is left alone"
    assert row["id"] == 1


def test_masking_uses_what_the_scan_recorded(served):
    """`ssn` is not a name the value patterns would catch from one row; the
    inventory knew it from the column name."""
    (row,) = _call(served, "get_sample", {"container": "users_pii", "limit": 1})

    assert row["ssn"] != "123-45-6789"
    assert "***" in str(row["ssn"])


def test_stdio_can_ask_for_raw_rows(served):
    """No token means the caller already holds the database credential; there
    is nothing left for a refusal to protect."""
    (row,) = _call(
        served, "get_sample", {"container": "users", "limit": 1, "mask": False}
    )

    assert row["email"] == "alice@example.com"


def test_a_key_without_the_grant_may_not_ask_for_raw_rows(
    served, monkeypatch: pytest.MonkeyPatch
):
    _as(monkeypatch, ["get_sample"])

    with pytest.raises(ToolError, match="may not read unmasked rows"):
        _call(served, "get_sample", {"container": "users", "mask": False})


def test_a_key_with_the_grant_may(served, monkeypatch: pytest.MonkeyPatch):
    _as(monkeypatch, ["get_sample"], allow_raw_sample=True)

    (row,) = _call(
        served, "get_sample", {"container": "users", "limit": 1, "mask": False}
    )

    assert row["email"] == "alice@example.com"


def test_the_trail_says_which_columns_were_hidden(source: Path, tmp_path: Path):
    trail = AuditLogger(tmp_path / "masked.jsonl")
    mcp = build_server(
        ServerConfig(server_name="authz", engine=SourceEngine.SQLITE),
        SqliteAdapter(source),
        audit=trail,
    )

    _call(mcp, "get_sample", {"container": "users", "limit": 1})

    assert trail.records()[-1]["masked_columns"] == ["email"]


def test_the_trail_says_when_raw_rows_were_handed_over(source: Path, tmp_path: Path):
    """ "Who has seen unmasked rows, and of what" should be answerable without
    reading the whole trail for it."""
    trail = AuditLogger(tmp_path / "raw.jsonl")
    mcp = build_server(
        ServerConfig(server_name="authz", engine=SourceEngine.SQLITE),
        SqliteAdapter(source),
        audit=trail,
    )

    _call(mcp, "get_sample", {"container": "users", "limit": 1, "mask": False})

    record = trail.records()[-1]
    assert record["unmasked"] is True
    assert record["params"]["mask"] is False


# ---- which containers ----


def test_a_denied_container_is_refused_by_name(served, monkeypatch):
    """Not silently emptied: an agent told a table is not there goes looking
    for it, and one told it may not look asks for access."""
    _as(monkeypatch, ["get_schema"], containers={"deny": ["*_pii"]})

    with pytest.raises(ToolError, match="may not read 'users_pii'"):
        _call(served, "get_schema", {"container": "users_pii"})


def test_an_allowed_container_still_works(served, monkeypatch):
    _as(monkeypatch, ["get_schema"], containers={"allow": ["dim_*"]})

    columns = _call(served, "get_schema", {"container": "dim_product"})

    assert [c.name for c in columns] == ["id", "name"]


def test_a_listing_leaves_out_what_the_key_may_not_read(served, monkeypatch):
    """Showing the names would leak the shape of the catalog to someone who
    cannot read any of it."""
    _as(monkeypatch, ["list_containers"], containers={"allow": ["dim_*"]})

    page = _call(served, "list_containers")

    assert [c.container_name for c in page.containers] == ["dim_product"]


def test_the_inventory_listing_is_filtered_the_same_way(served, monkeypatch):
    _as(monkeypatch, ["inventory_containers"], containers={"deny": ["users*"]})

    page = _call(served, "inventory_containers")

    assert [c.container_name for c in page.containers] == ["dim_product"]


def test_search_does_not_return_what_the_key_may_not_read(served, monkeypatch):
    """The hole this closes: `get_schema` refuses `users_pii`, so search must
    not hand over its column names instead."""
    _as(monkeypatch, ["inventory_search"], containers={"deny": ["*_pii"]})

    hits = _call(served, "inventory_search", {"keyword": "ssn"})

    assert hits == []


def test_the_columns_of_a_denied_container_are_refused(served, monkeypatch):
    _as(monkeypatch, ["inventory_columns"], containers={"deny": ["*_pii"]})

    with pytest.raises(ToolError, match="may not read"):
        _call(
            served,
            "inventory_columns",
            {"container": "users_pii", "database": "main"},
        )


def test_a_database_wide_page_of_columns_is_filtered_too(served, monkeypatch):
    """
    The hole a container-less page opens: `_require_access` can only check the
    database, so a key denied `users_pii` would otherwise read its column names
    by asking for the whole database instead of that one table.
    """
    _as(monkeypatch, ["inventory_columns"], containers={"deny": ["*_pii"]})

    page = _call(served, "inventory_columns", {"database": "main", "limit": 99})

    assert {c.container_name for c in page.columns} == {"dim_product", "users"}
    assert "ssn" not in {c.column_name for c in page.columns}


def test_filtering_a_page_does_not_stall_the_paging(served, monkeypatch):
    """The cursor comes from the unfiltered page, so a run of denied containers
    is skipped over rather than answered with an empty page and no way on."""
    _as(monkeypatch, ["inventory_columns"], containers={"allow": ["users"]})

    seen: list[str] = []
    cursor = None
    for _ in range(20):
        page = _call(
            served,
            "inventory_columns",
            {"database": "main", "limit": 1, "cursor": cursor},
        )
        seen.extend(c.column_name for c in page.columns)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert seen == ["id", "email", "note"]


def test_an_export_covers_only_what_the_key_may_read(served, monkeypatch):
    """More use than a refusal, and the same rule the listings apply."""
    _as(monkeypatch, ["inventory_export"], containers={"allow": ["dim_*"]})

    result = _call(served, "inventory_export", {"format": "csv"})

    written = Path(result.path).read_text(encoding="utf-8")
    assert "dim_product" in written
    assert "users_pii" not in written
    assert result.containers == 1


# ---- which database ----


def test_a_key_named_on_another_database_is_refused(served, monkeypatch):
    _as(monkeypatch, ["get_schema"], databases=["analytics"])

    with pytest.raises(ToolError, match="may not read database"):
        _call(served, "get_schema", {"container": "users", "database": "main"})


def test_a_restricted_key_may_not_ride_the_servers_default(served, monkeypatch):
    """`database=None` is "whatever this server points at", which is not a
    database this key was named on."""
    _as(monkeypatch, ["get_schema"], databases=["analytics"])

    with pytest.raises(ToolError, match="may not read database"):
        _call(served, "get_schema", {"container": "users"})


def test_list_databases_shows_only_the_permitted_ones(served, monkeypatch):
    _as(monkeypatch, ["list_databases"], databases=["analytics"])

    assert _call(served, "list_databases") == []


# ---- the sweep ----


def test_every_tool_refuses_a_key_that_was_not_granted_it(served, monkeypatch):
    """
    A rule nothing consults is not a restriction. This is the test that fails
    when a tool added later forgets to ask.
    """
    _as(monkeypatch, [])  # a valid token granting nothing

    for tool in ALL_TOOLS:
        with pytest.raises(ToolError, match=f"may not call {tool}"):
            _call(served, tool, _arguments_for(tool))


def _arguments_for(tool: str) -> dict[str, Any]:
    if tool in ("get_schema", "get_sample"):
        return {"container": "users"}
    if tool == "profile_column":
        return {"container": "users", "column": "id", "mode": "null_ratio"}
    if tool == "inventory_columns":
        return {"container": "users", "database": "main"}
    if tool == "inventory_annotate":
        return {"database": "main", "container": "users", "container_description": "x"}
    if tool == "inventory_search":
        return {"keyword": "id"}
    return {}
