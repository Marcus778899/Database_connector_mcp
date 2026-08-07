"""
The entry point, up to but not including `mcp.run()` — starting a transport
would block. `build` is exercised through a client session, which runs the
lifespan and so also covers the teardown it owns.
"""

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

import main as entry
from src.core.config import ConnectionInfo, ServerConfig, SourceEngine
from src.core.contracts import ProfileMode
from src.service import factory

LIVE_TOOLS = {
    "list_databases",
    "list_containers",
    "get_schema",
    "get_sample",
    "profile_column",
}
INVENTORY_TOOLS = {
    "inventory_start",
    "inventory_status",
    "inventory_cancel",
    "inventory_summary",
    "inventory_containers",
    "inventory_columns",
    "inventory_search",
    "inventory_annotate",
}


@pytest.fixture(autouse=True)
def no_ambient_env(monkeypatch: pytest.MonkeyPatch):
    """A developer's own .env must not decide what these tests see."""
    monkeypatch.setattr("src.core.env.load_repo_dotenv", lambda **_: None)
    for name in (
        "MCP_SERVER_NAME",
        "MCP_ENGINE",
        "MCP_CONNECTION_REF",
        "MCP_DATABASE",
        "MCP_TRANSPORT",
        "MCP_HOST",
        "MCP_PORT",
        "MCP_MAX_SAMPLE_LIMIT",
        "MCP_STAGING_DB",
        "MCP_EXPORT_DIR",
        "MCP_AUDIT_LOG",
        "MCP_PROFILE_MODES",
        "MCP_REQUIRE_AUTH",
        "MCP_ALLOW_INSECURE_HTTP",
        "MCP_CONNECTION_CHECK",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "source.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    conn.execute("INSERT INTO users (email) VALUES ('a@x.com')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def source_env(db: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """`resolve_connection` reads the real environment, by prefix."""
    monkeypatch.setenv("LOCAL_PATH", str(db))
    return "local"


def _config(argv: list[str], env: dict[str, str] | None = None) -> ServerConfig:
    args = entry.build_parser().parse_args(argv)
    return entry.config_from_args(args, env or {})


def _tool_names(mcp) -> set[str]:
    async def run() -> set[str]:
        async with Client(mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    return asyncio.run(run())


def _call(mcp, tool: str, args: dict[str, Any] | None = None) -> Any:
    async def run() -> Any:
        async with Client(mcp) as client:
            return (await client.call_tool(tool, args or {})).data

    return asyncio.run(run())


# ---- config resolution ----


def test_an_empty_command_line_keeps_the_defaults():
    config = _config([])

    assert config.engine == SourceEngine.SQLITE
    assert config.transport == "stdio"
    assert config.connection_ref is None
    assert config.staging_db_path is None
    assert config.export_dir is None
    assert config.profile_modes is None


def test_the_environment_supplies_what_the_flags_omit():
    config = _config([], {"MCP_ENGINE": "datalake", "MCP_CONNECTION_REF": "lake"})

    assert config.engine == SourceEngine.DATALAKE
    assert config.connection_ref == "lake"


def test_a_flag_beats_the_environment():
    config = _config(["--connection-ref", "flag"], {"MCP_CONNECTION_REF": "env"})

    assert config.connection_ref == "flag"


def test_an_empty_environment_variable_is_not_a_value():
    assert _config([], {"MCP_CONNECTION_REF": ""}).connection_ref is None


def test_numbers_and_paths_are_coerced(tmp_path: Path):
    config = _config(
        ["--port", "9001", "--max-sample-limit", "7"],
        {"MCP_STAGING_DB": str(tmp_path / "staging.db")},
    )

    assert config.port == 9001
    assert config.max_sample_limit == 7
    assert config.staging_db_path == tmp_path / "staging.db"


def test_profile_modes_from_repeated_flags():
    config = _config(["--profile-mode", "null_ratio", "--profile-mode", "min_max"])

    assert config.profile_modes == [ProfileMode.NULL_RATIO, ProfileMode.MIN_MAX]


def test_profile_modes_from_the_environment():
    config = _config([], {"MCP_PROFILE_MODES": "null_ratio, top_values"})

    assert config.profile_modes == [ProfileMode.NULL_RATIO, ProfileMode.TOP_VALUES]


def test_an_unknown_profile_mode_is_refused():
    with pytest.raises(entry.ConfigurationError, match="unknown profile mode"):
        _config([], {"MCP_PROFILE_MODES": "median"})


def test_the_audit_rotation_is_configurable():
    config = _config(["--audit-max-mb", "50"], {"MCP_AUDIT_BACKUPS": "2"})

    assert config.audit_max_mb == 50
    assert config.audit_backups == 2


def test_unset_leaves_the_choice_to_the_engine():
    """None and [] are different downstream: per-column choice, or nothing."""
    assert _config([]).profile_modes is None


def test_no_profile_turns_profiling_off_entirely():
    assert _config(["--no-profile"]).profile_modes == []
    assert _config([], {"MCP_PROFILE": "false"}).profile_modes == []


def test_asking_for_profiling_without_naming_modes_leaves_the_choice_open():
    assert _config(["--profile"]).profile_modes is None


def test_no_profile_beats_the_modes_it_contradicts():
    config = _config(["--no-profile", "--profile-mode", "null_ratio"])

    assert config.profile_modes == []


def test_booleans_come_from_the_environment():
    config = _config(
        ["--transport", "http", "--host", "0.0.0.0"], {"MCP_REQUIRE_AUTH": "true"}
    )

    assert config.require_auth is True


def test_a_bare_flag_is_true():
    assert _config(["--allow-insecure-http"], {}).allow_insecure_http is True


@pytest.mark.parametrize("raw", ["1", "yes", "ON"])
def test_truthy_spellings(raw: str):
    assert _config([], {"MCP_ALLOW_INSECURE_HTTP": raw}).allow_insecure_http is True


@pytest.mark.parametrize("raw", ["0", "false", "no"])
def test_falsy_spellings(raw: str):
    assert _config([], {"MCP_ALLOW_INSECURE_HTTP": raw}).allow_insecure_http is False


def test_no_flag_beats_a_truthy_environment_variable():
    config = _config(["--no-allow-insecure-http"], {"MCP_ALLOW_INSECURE_HTTP": "1"})

    assert config.allow_insecure_http is False


def test_a_value_that_is_not_a_boolean_is_refused():
    with pytest.raises(entry.ConfigurationError, match="expected a boolean"):
        _config([], {"MCP_REQUIRE_AUTH": "maybe"})


def test_a_config_the_model_rejects_becomes_a_configuration_error():
    """The transport/auth rules stay in ServerConfig; this only reports them."""
    with pytest.raises(entry.ConfigurationError, match="without authentication"):
        _config(["--transport", "http", "--host", "0.0.0.0"])


# ---- wiring ----


def test_build_needs_a_connection_ref():
    with pytest.raises(entry.ConfigurationError, match="no connection"):
        entry.build(ServerConfig())


def test_a_ref_with_no_environment_behind_it_is_a_configuration_error():
    """The commonest setup error. Its message must not arrive in a traceback."""
    with pytest.raises(entry.ConfigurationError, match="ABSENT_REF_URI"):
        entry.build(ServerConfig(connection_ref="absent_ref"))


def test_main_reports_a_mistyped_ref(capsys: pytest.CaptureFixture[str]):
    assert entry.main(["--connection-ref", "absent_ref"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ABSENT_REF_PATH" in captured.err


def test_build_serves_the_live_tools(source_env: str, db: Path):
    mcp = entry.build(ServerConfig(connection_ref=source_env))

    assert LIVE_TOOLS <= _tool_names(mcp)


def test_without_a_staging_database_there_are_no_inventory_tools(source_env: str):
    mcp = entry.build(ServerConfig(connection_ref=source_env))

    assert _tool_names(mcp) & INVENTORY_TOOLS == set()


def test_a_staging_database_adds_the_inventory_tools(source_env: str, tmp_path: Path):
    mcp = entry.build(
        ServerConfig(connection_ref=source_env, staging_db_path=tmp_path / "staging.db")
    )

    assert INVENTORY_TOOLS <= _tool_names(mcp)


def test_the_wired_server_reaches_the_source(source_env: str):
    mcp = entry.build(ServerConfig(connection_ref=source_env))

    page = _call(mcp, "list_containers")

    assert [c.container_name for c in page.containers] == ["users"]


def test_staging_into_the_source_is_refused(source_env: str, db: Path):
    """The staging store would otherwise write into the database being read."""
    with pytest.raises(entry.ConfigurationError, match="MCP_STAGING_DB"):
        entry.build(ServerConfig(connection_ref=source_env, staging_db_path=db))


# ---- the connection is checked before the port is bound ----


def test_a_reachable_source_is_confirmed_in_the_log(source_env: str, caplog):
    """
    The point of the whole check: something in the log that says the login
    worked, rather than silence that means nobody has tried yet.
    """
    entry.build(ServerConfig(connection_ref=source_env), check_connection=True)

    assert "login is accepted" in caplog.text


def test_an_unreachable_source_stops_the_server_starting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    A wrong host used to be discovered by an agent, mid-task, through a tool
    error — on a server whose own logs said it started fine.
    """
    monkeypatch.setenv("LOCAL_PATH", str(tmp_path / "nope" / "missing.db"))

    with pytest.raises(entry.ConfigurationError, match="cannot reach"):
        entry.build(ServerConfig(connection_ref="local"), check_connection=True)


def test_the_failure_says_how_to_start_anyway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCAL_PATH", str(tmp_path / "nope" / "missing.db"))

    with pytest.raises(entry.ConfigurationError) as caught:
        entry.build(ServerConfig(connection_ref="local"), check_connection=True)

    assert "MCP_CONNECTION_CHECK=warn" in str(caught.value)


def test_warn_starts_anyway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog):
    """For a source that is legitimately slower to come up than this is."""
    monkeypatch.setenv("LOCAL_PATH", str(tmp_path / "nope" / "missing.db"))

    entry.build(
        ServerConfig(connection_ref="local", connection_check="warn"),
        check_connection=True,
    )

    assert "starting anyway" in caplog.text


def test_off_does_not_open_a_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setenv("LOCAL_PATH", str(tmp_path / "nope" / "missing.db"))

    entry.build(
        ServerConfig(connection_ref="local", connection_check="off"),
        check_connection=True,
    )


def test_building_without_serving_does_not_touch_the_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """`build` is what the tests do; only `main` is what serves."""
    monkeypatch.setenv("LOCAL_PATH", str(tmp_path / "nope" / "missing.db"))

    entry.build(ServerConfig(connection_ref="local"))


@pytest.mark.parametrize(
    "conn, expected",
    [
        (
            ConnectionInfo(
                host="db.internal", port=1433, user="reader", database="shop"
            ),
            "mssql as reader@db.internal:1433/shop",
        ),
        (ConnectionInfo(host="db.internal"), "mssql as db.internal"),
        (ConnectionInfo(path="/source/shop.db"), "mssql at /source/shop.db"),
    ],
)
def test_the_source_is_described_without_its_password(
    conn: ConnectionInfo, expected: str
):
    config = ServerConfig(engine=SourceEngine.MSSQL, connection_ref="shop")

    assert entry.describe_source(config, conn) == expected


def test_a_uri_is_never_quoted_back(caplog):
    """It carries the password inside it, and this string goes to a log."""
    config = ServerConfig(engine=SourceEngine.POSTGRES, connection_ref="shop")
    conn = ConnectionInfo(uri="postgresql://reader:s3cret@db.internal/shop")

    described = entry.describe_source(config, conn)

    assert "s3cret" not in described
    assert "SHOP_URI" in described


def test_the_check_is_required_by_default():
    assert ServerConfig().connection_check == "require"


def test_the_check_can_be_set_from_the_environment():
    config = _config([], {"MCP_CONNECTION_CHECK": "warn"})

    assert config.connection_check == "warn"


def test_a_check_mode_nobody_recognises_is_refused():
    with pytest.raises(entry.ConfigurationError):
        _config([], {"MCP_CONNECTION_CHECK": "maybe"})


def test_an_unimplemented_engine_is_reported(monkeypatch: pytest.MonkeyPatch, db: Path):
    """
    Refused at build time, not at the first call.

    The pool loads adapters lazily, which used to mean an image built without
    the driver started cleanly and then failed on every tool call — a symptom
    a long way from its cause. One image now carries one engine's driver, so
    the mismatch is worth catching before the port is bound.
    """
    monkeypatch.setitem(
        factory._ADAPTER_REGISTRY,
        SourceEngine.POSTGRES,
        ("src.adapter.oracle", "OracleAdapter"),
    )
    monkeypatch.setenv("PG_URI", "postgresql://localhost/x")

    with pytest.raises(entry.ConfigurationError, match="not implemented yet"):
        entry.build(ServerConfig(engine=SourceEngine.POSTGRES, connection_ref="pg"))


def test_a_missing_driver_in_a_container_says_to_rebuild(
    monkeypatch: pytest.MonkeyPatch,
):
    """
    The advice has to match where it is read.

    `uv sync --extra mssql` is right in a checkout and wrong in a container:
    it installs no OS-level driver, and nothing it does install survives the
    next start. Inside an image the engine is a build argument.
    """
    monkeypatch.setenv("MCP_IN_CONTAINER", "1")
    monkeypatch.setattr(
        factory.importlib,
        "import_module",
        _raise(ImportError("No module named 'pyodbc'", name="pyodbc")),
    )

    with pytest.raises(factory.AdapterNotAvailableError) as caught:
        factory.load_adapter_class(SourceEngine.MSSQL)

    message = str(caught.value)
    assert "MCP_ENGINE=mssql" in message
    assert "uv sync" not in message


def _raise(exc: Exception):
    def raiser(*_args: object, **_kwargs: object):
        raise exc

    return raiser


# ---- transport ----


def test_stdio_takes_no_host_or_port():
    assert entry._run_kwargs(ServerConfig(transport="stdio")) == {}


def test_an_http_transport_takes_host_and_port():
    config = ServerConfig(transport="http", host="127.0.0.1", port=9002)

    assert entry._run_kwargs(config) == {"host": "127.0.0.1", "port": 9002}


def test_main_reports_a_configuration_error(capsys: pytest.CaptureFixture[str]):
    """Never on stdout: on stdio that stream carries JSON-RPC."""
    assert entry.main([]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no connection" in captured.err


def test_the_export_tool_follows_the_export_directory(source_env: str, tmp_path: Path):
    """Configured end to end: the flag decides whether the tool exists at all."""
    without = entry.build(
        ServerConfig(connection_ref=source_env, staging_db_path=tmp_path / "staging.db")
    )
    assert "inventory_export" not in _tool_names(without)

    with_export = entry.build(
        ServerConfig(
            connection_ref=source_env,
            staging_db_path=tmp_path / "staging2.db",
            export_dir=tmp_path / "exports",
        )
    )
    assert "inventory_export" in _tool_names(with_export)
