import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from pyarrow.fs import LocalFileSystem

from src.adapter.datalake import DatalakeAdapter
from src.core.config import ConnectionInfo, ServerConfig, SourceEngine
from src.server import LOCAL_KEY_ID, build_server
from src.service.audit import AuditLogger
from src.service.pool import AdapterPool, SingleAdapter

TABLE = pa.table({"id": [1, 2, 3, 4, None], "tag": ["a", "b", "a", "c", "a"]})


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    for name in ("orders", "users"):
        (tmp_path / name).mkdir()
        pq.write_table(TABLE, tmp_path / name / "part-0.parquet")
    return tmp_path


@pytest.fixture
def config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        server_name="test-mcp",
        engine=SourceEngine.DATALAKE,
        max_sample_limit=2,
        audit_log_path=tmp_path / "audit.jsonl",
    )


@pytest.fixture
def adapter(lake: Path) -> Iterator[DatalakeAdapter]:
    built = DatalakeAdapter(str(lake), LocalFileSystem())
    yield built
    built.close()


def _call(mcp, tool: str, args: dict[str, Any] | None = None) -> Any:
    async def run() -> Any:
        async with Client(mcp) as client:
            return (await client.call_tool(tool, args or {})).data

    return asyncio.run(run())


def _session(mcp, body):
    """
    One client across several calls. `_call` opens and closes a session per call,
    which shuts the lifespan down — and shutdown cancels running scans. A real
    server keeps the session open between tool calls.
    """

    async def run() -> Any:
        async with Client(mcp) as client:

            async def call(tool: str, args: dict[str, Any] | None = None) -> Any:
                return (await client.call_tool(tool, args or {})).data

            return await body(call)

    return asyncio.run(run())


def _tools(mcp) -> list[str]:
    async def run() -> list[str]:
        async with Client(mcp) as client:
            return sorted(tool.name for tool in await client.list_tools())

    return asyncio.run(run())


# ---- surface ----


def test_only_the_read_only_tools_are_exposed(config, adapter):
    assert _tools(build_server(config, adapter)) == [
        "get_sample",
        "get_schema",
        "list_containers",
        "list_databases",
        "profile_column",
    ]


def test_a_bare_adapter_is_wrapped_in_a_provider(config, adapter):
    assert _call(build_server(config, adapter), "list_databases") == ["datalake"]


def test_a_pool_is_used_as_given(config, lake: Path):
    pool = AdapterPool(SourceEngine.DATALAKE, ConnectionInfo(path=str(lake)))
    try:
        assert _call(build_server(config, pool), "list_databases") == ["datalake"]
    finally:
        pool.close()


# ---- tools ----


def test_list_containers_returns_a_page(config, adapter):
    mcp = build_server(config, adapter)

    page = _call(mcp, "list_containers", {"limit": 1})

    assert [c.container_name for c in page.containers] == ["orders"]
    assert page.next_cursor == "orders"


def test_the_cursor_reaches_the_adapter(config, adapter):
    mcp = build_server(config, adapter)

    page = _call(mcp, "list_containers", {"limit": 1, "cursor": "orders"})

    assert [c.container_name for c in page.containers] == ["users"]
    assert page.next_cursor is None


def test_get_schema(config, adapter):
    columns = _call(build_server(config, adapter), "get_schema", {"container": "users"})

    assert [c.name for c in columns] == ["id", "tag"]


def test_get_sample_is_capped_by_the_server_config(config, adapter):
    """config.max_sample_limit is 2 here."""
    rows = _call(
        build_server(config, adapter), "get_sample", {"container": "users", "limit": 99}
    )

    assert len(rows) == 2


def test_profile_column(config, adapter):
    result = _call(
        build_server(config, adapter),
        "profile_column",
        {"container": "users", "column": "id", "mode": "null_ratio"},
    )

    assert result.null_ratio == 0.2


def test_the_mode_argument_is_a_closed_enum(config, adapter):
    """Typed as ProfileMode, so the schema lists the choices and a bad one is a
    validation error rather than a bare ValueError."""
    with pytest.raises(ToolError):
        _call(
            build_server(config, adapter),
            "profile_column",
            {"container": "users", "column": "id", "mode": "median"},
        )


def test_database_selects_the_adapter_without_reaching_the_method(config, adapter):
    """The tools take `database`; `get_schema` on the adapter does not. Passing it
    through was what broke the first client."""
    columns = _call(
        build_server(config, adapter),
        "get_schema",
        {"container": "users", "database": "datalake"},
    )

    assert [c.name for c in columns] == ["id", "tag"]


def test_an_unknown_database_is_a_tool_error(config, lake: Path):
    pool = AdapterPool(SourceEngine.DATALAKE, ConnectionInfo(path=str(lake)))
    try:
        with pytest.raises(ToolError):
            _call(build_server(config, pool), "list_containers", {"database": "nope"})
    finally:
        pool.close()


def test_an_unknown_container_is_a_tool_error(config, adapter):
    with pytest.raises(ToolError):
        _call(build_server(config, adapter), "get_schema", {"container": "ghost"})


# ---- audit ----


def test_every_call_is_recorded(config, adapter):
    trail = AuditLogger(config.audit_log_path)
    mcp = build_server(config, adapter, audit=trail)

    _call(mcp, "get_sample", {"container": "users", "limit": 1})

    (record,) = trail.records()
    assert record["tool"] == "get_sample"
    assert record["key_id"] == LOCAL_KEY_ID
    assert record["outcome"] == "ok"
    assert record["rows_returned"] == 1
    assert record["params"]["container"] == "users"
    assert record["duration_ms"] >= 0


def test_the_statement_the_source_ran_is_recorded(config, adapter):
    """`pop_rendered_sql` was produced and thrown away before this existed."""
    trail = AuditLogger(config.audit_log_path)

    _call(
        build_server(config, adapter, audit=trail), "get_schema", {"container": "users"}
    )

    (record,) = trail.records()
    assert "scan" in record["rendered_sql"]
    assert "users" in record["rendered_sql"]


def test_no_row_contents_reach_the_audit_trail(config, adapter):
    """Otherwise the trail becomes a second copy of the data it accounts for."""
    trail = AuditLogger(config.audit_log_path)

    _call(
        build_server(config, adapter, audit=trail), "get_sample", {"container": "users"}
    )

    written = config.audit_log_path.read_text()
    assert "rows_returned" in written
    for value in ("a", "b", "c"):
        assert f'"{value}"' not in written


def test_a_failed_call_is_recorded_too(config, adapter):
    trail = AuditLogger(config.audit_log_path)
    mcp = build_server(config, adapter, audit=trail)

    with pytest.raises(ToolError):
        _call(mcp, "get_schema", {"container": "ghost"})

    (record,) = trail.records()
    assert record["outcome"] == "ToolError"
    assert "ghost" in record["error"]


# ---- lifecycle ----


def test_the_reaper_runs_while_the_server_is_up(config, lake: Path):
    pool = AdapterPool(SourceEngine.DATALAKE, ConnectionInfo(path=str(lake)))
    mcp = build_server(config, pool)

    async def run() -> tuple[bool, bool]:
        async with Client(mcp):
            during = pool.reaper_running
        return during, pool.reaper_running

    during, after = asyncio.run(run())

    assert during is True
    assert after is False, "shutdown must stop the reaper and close the pool"


# ---- authentication ----


def test_a_server_that_requires_auth_is_built_with_a_verifier(tmp_path: Path, adapter):
    from src.auth.verifier import SignedTokenVerifier

    keys = tmp_path / "keys"
    keys.mkdir()
    config = ServerConfig(
        transport="http", host="0.0.0.0", require_auth=True, authorized_keys_dir=keys
    )

    mcp = build_server(config, adapter)

    assert isinstance(mcp.auth, SignedTokenVerifier)


def test_a_server_that_does_not_require_auth_has_none(config, adapter):
    """stdio: whoever spawned us already has the credential we would protect."""
    assert build_server(config, adapter).auth is None


def test_require_auth_without_a_key_directory_is_refused(tmp_path: Path, adapter):
    """Better than starting and turning every caller away — the mistake is in
    the configuration and the message says which flag fixes it."""
    from src.auth import AuthConfigurationError

    config = ServerConfig(transport="http", host="0.0.0.0", require_auth=True)

    with pytest.raises(AuthConfigurationError, match="--authorized-keys-dir"):
        build_server(config, adapter)


def test_an_unauthenticated_call_is_refused_when_auth_is_required(monkeypatch):
    """Defence in depth: the transport should have turned this away already, so
    if one ever arrives here it is the bug that matters."""
    from src import server as server_module

    monkeypatch.setattr(server_module, "get_access_token", lambda: None)

    assert server_module._identity("get_schema") == LOCAL_KEY_ID
    with pytest.raises(ToolError, match="requires an authenticated caller"):
        server_module._identity("get_schema", require_auth=True)


def test_a_tool_outside_the_scopes_is_refused(monkeypatch):
    from src import server as server_module

    token = SimpleNamespace(
        scopes=["get_schema"], subject="pm-alice", client_id="pm-alice"
    )
    monkeypatch.setattr(server_module, "get_access_token", lambda: token)

    assert server_module._identity("get_schema", require_auth=True) == "pm-alice"
    with pytest.raises(ToolError, match="may not call get_sample"):
        server_module._identity("get_sample", require_auth=True)


# ---- federation loop ----


def test_our_own_client_adapter_can_drive_our_own_server(config, adapter):
    """
    The tool signatures and the SourceAdaptor contract have to agree exactly.
    Pointing RemoteMcpAdapter at build_server() is the only test that proves it.
    """
    from fastmcp import Client as FastClient

    from src.adapter.remote_mcp import RemoteMcpAdapter
    from src.core.contracts import ProfileMode, SourceAdaptor

    mcp = build_server(config, adapter)

    with RemoteMcpAdapter(FastClient(mcp), database="datalake") as remote:
        assert isinstance(remote, SourceAdaptor)
        assert remote.list_databases() == ["datalake"]

        page = remote.list_containers(limit=1)
        assert [c.container_name for c in page.containers] == ["orders"]
        assert page.next_cursor == "orders"

        rest = remote.list_containers(cursor=page.next_cursor)
        assert [c.container_name for c in rest.containers] == ["users"]

        assert [c.name for c in remote.get_schema("users")] == ["id", "tag"]
        assert len(remote.get_sample("users", limit=99)) == 2  # capped by the server
        assert (
            remote.profile_column("users", "id", ProfileMode.NULL_RATIO).null_ratio
            == 0.2
        )


# ---- inventory tools ----


@pytest.fixture
def inventory(config, adapter, tmp_path: Path) -> Iterator[Any]:
    from src.service.inventory import InventoryService
    from src.service.staging import StagingStore

    with StagingStore(tmp_path / "staging.db") as store:
        service = InventoryService(SingleAdapter(adapter), store)
        yield service
        service.close()


INVENTORY_TOOLS = [
    "inventory_annotate",
    "inventory_cancel",
    "inventory_columns",
    "inventory_containers",
    "inventory_start",
    "inventory_status",
    "inventory_summary",
]


def test_the_inventory_tools_are_opt_in(config, adapter):
    assert not set(INVENTORY_TOOLS) & set(_tools(build_server(config, adapter)))


def test_the_inventory_tools_appear_when_a_service_is_given(config, adapter, inventory):
    exposed = _tools(build_server(config, adapter, inventory=inventory))

    assert set(INVENTORY_TOOLS) <= set(exposed)
    assert len(exposed) == 12


def test_a_scan_can_be_started_and_polled(config, adapter, inventory):
    mcp = build_server(config, adapter, inventory=inventory)

    async def body(call):
        job_id = await call("inventory_start", {"database": "datalake"})
        inventory.wait(job_id, timeout=20)
        return await call("inventory_status", {"job_id": job_id})

    status = _session(mcp, body)

    assert status.state == "done"
    assert status.containers_done == 2


def test_the_summary_comes_back_typed(config, adapter, inventory):
    async def body(call):
        inventory.wait(await call("inventory_start", {}), timeout=20)
        return await call("inventory_summary", {})

    summary = _session(build_server(config, adapter, inventory=inventory), body)

    assert summary.containers == 2
    assert summary.columns == 4
    assert summary.containers_failed == 0


def test_the_recorded_containers_are_paged(config, adapter, inventory):
    async def body(call):
        inventory.wait(await call("inventory_start", {}), timeout=20)
        return (
            await call("inventory_containers", {"limit": 1}),
            await call("inventory_containers", {"limit": 1, "cursor": "orders"}),
        )

    first, rest = _session(build_server(config, adapter, inventory=inventory), body)

    assert [c.container_name for c in first.containers] == ["orders"]
    assert first.next_cursor == "orders"
    assert [c.container_name for c in rest.containers] == ["users"]
    assert rest.next_cursor is None


def test_recorded_columns_include_the_profile(config, adapter, inventory):
    async def body(call):
        job_id = await call(
            "inventory_start",
            {"database": "datalake", "profile_modes": ["null_ratio"]},
        )
        inventory.wait(job_id, timeout=20)
        return await call(
            "inventory_columns", {"container": "users", "database": "datalake"}
        )

    columns = _session(build_server(config, adapter, inventory=inventory), body)

    assert [c.column_name for c in columns] == ["id", "tag"]
    assert columns[0].profile is not None
    assert columns[0].profile["null_ratio"]["null_ratio"] == 0.2


def test_an_unknown_job_is_a_tool_error(config, adapter, inventory):
    with pytest.raises(ToolError):
        _call(
            build_server(config, adapter, inventory=inventory),
            "inventory_status",
            {"job_id": "nope"},
        )


def test_cancelling_an_unknown_job_is_false_not_an_error(config, adapter, inventory):
    result = _call(
        build_server(config, adapter, inventory=inventory),
        "inventory_cancel",
        {"job_id": "nope"},
    )

    assert result is False


def test_a_second_scan_of_one_database_is_refused(config, adapter, inventory, tmp_path):
    """ScanAlreadyRunningError has to reach the caller as a readable tool error."""
    from src.service.inventory import ScanAlreadyRunningError

    mcp = build_server(config, adapter, inventory=inventory)
    started: list[str] = []

    def fake_start(*args, **kwargs):
        if started:
            raise ScanAlreadyRunningError("a scan of 'datalake' is already running")
        started.append("x")
        return "job-1"

    inventory.start = fake_start
    _call(mcp, "inventory_start", {})

    with pytest.raises(ToolError, match="already running"):
        _call(mcp, "inventory_start", {})


def test_starting_a_scan_is_audited_with_its_job_id(config, adapter, inventory):
    trail = AuditLogger(config.audit_log_path)
    mcp = build_server(config, adapter, audit=trail, inventory=inventory)

    async def body(call):
        job_id = await call("inventory_start", {})
        inventory.wait(job_id, timeout=20)
        return job_id

    job_id = _session(mcp, body)

    (record,) = trail.records()
    assert record["tool"] == "inventory_start"
    assert record["job_id"] == job_id


# ---- annotation ----


def test_a_description_written_through_the_tool_survives_a_forced_rescan(
    config, adapter, inventory
):
    """The regression the write path exists for: rescanning used to wipe every
    description it had just been given."""
    mcp = build_server(config, adapter, inventory=inventory)

    async def body(call):
        inventory.wait(await call("inventory_start", {}), timeout=20)
        written = await call(
            "inventory_annotate",
            {
                "database": "datalake",
                "container": "users",
                "container_description": "everyone who signed up",
                "columns": [
                    {"column": "id", "description": "surrogate key"},
                    {"column": "tag", "description": "cohort", "sensitivity": "pii"},
                ],
            },
        )
        inventory.wait(await call("inventory_start", {"force": True}), timeout=20)
        return written, await call(
            "inventory_columns", {"container": "users", "database": "datalake"}
        )

    written, columns = _session(mcp, body)

    assert (written.containers_updated, written.columns_updated) == (1, 2)
    assert written.unknown_columns == []
    assert [c.description for c in columns] == ["surrogate key", "cohort"]
    assert columns[0].description_source == "ai"
    assert columns[1].sensitivity == "pii"


def test_an_unknown_column_comes_back_rather_than_failing_the_write(
    config, adapter, inventory
):
    mcp = build_server(config, adapter, inventory=inventory)

    async def body(call):
        inventory.wait(await call("inventory_start", {}), timeout=20)
        return await call(
            "inventory_annotate",
            {
                "database": "datalake",
                "container": "users",
                "columns": [
                    {"column": "id", "description": "surrogate key"},
                    {"column": "nope", "description": "not a column"},
                ],
            },
        )

    result = _session(mcp, body)

    assert result.unknown_columns == ["nope"]
    assert result.columns_updated == 1


def test_annotating_something_never_inventoried_is_a_tool_error(
    config, adapter, inventory
):
    with pytest.raises(ToolError, match="inventory_start"):
        _call(
            build_server(config, adapter, inventory=inventory),
            "inventory_annotate",
            {
                "database": "datalake",
                "container": "ghost",
                "container_description": "nothing here",
            },
        )


def test_a_write_is_audited_by_how_much_it_wrote(config, adapter, inventory):
    """rows_returned accounts for reads; a write needs its own counterpart."""
    trail = AuditLogger(config.audit_log_path)
    mcp = build_server(config, adapter, audit=trail, inventory=inventory)

    async def body(call):
        inventory.wait(await call("inventory_start", {}), timeout=20)
        await call(
            "inventory_annotate",
            {
                "database": "datalake",
                "container": "users",
                "columns": [{"column": "id", "description": "surrogate key"}],
            },
        )

    _session(mcp, body)

    record = trail.records()[-1]
    assert record["tool"] == "inventory_annotate"
    assert record["columns_written"] == 1
    assert record["params"]["source"] == "ai"


def test_no_token_means_the_description_is_not_a_persons(config, adapter, inventory):
    """An agent must not be able to label its own guesses as a person's work."""
    from src.server import _annotation_source

    assert _annotation_source() == "ai"


def test_only_a_key_granted_the_scope_writes_as_a_person(monkeypatch):
    """The half of `annotate:human` that had nothing to read it until tokens
    existed: the scope is granted when the key is issued, and claiming it in
    the call is not an option the caller has."""
    from src import server as server_module

    def carrying(*scopes: str):
        monkeypatch.setattr(
            server_module,
            "get_access_token",
            lambda: SimpleNamespace(
                scopes=list(scopes), subject="pm-alice", client_id="pm-alice"
            ),
        )

    carrying("inventory_annotate")
    assert server_module._annotation_source() == "ai"

    carrying("inventory_annotate", server_module.HUMAN_ANNOTATION_SCOPE)
    assert server_module._annotation_source() == "human"


def test_shutdown_stops_the_inventory_workers(config, adapter, inventory):
    mcp = build_server(config, adapter, inventory=inventory)

    async def run() -> None:
        async with Client(mcp) as client:
            await client.call_tool("inventory_start", {})

    asyncio.run(run())

    assert all(not worker.is_alive() for worker in inventory._workers.values())
