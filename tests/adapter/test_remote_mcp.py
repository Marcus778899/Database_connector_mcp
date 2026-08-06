import threading
from collections.abc import Iterator

import pytest
from fastmcp import Client, FastMCP

from src.adapter.remote_mcp import RemoteCallError, RemoteMcpAdapter
from src.core.config import ConnectionInfo
from src.core.contracts import (
    ColumnInfo,
    ContainerInfo,
    ContainerPage,
    ContainerType,
    ProfileMode,
    ProfileResult,
    SourceAdaptor,
)

CATALOG = ["alpha", "beta", "gamma"]


def _column(name: str, ordinal: int = 1) -> ColumnInfo:
    return ColumnInfo(
        name=name,
        ordinal=ordinal,
        native_type="TEXT",
        nullable=True,
        is_pk=name == "id",
        is_fk=False,
    )


def _remote(*, fail: set[str] | None = None, slow: threading.Event | None = None):
    """A stand-in MCP server exposing this project's own tool contract."""
    failing = fail or set()
    mcp = FastMCP("stand-in")

    @mcp.tool
    def list_databases() -> list[str]:
        return ["remote_db", "other_db"]

    @mcp.tool
    def list_containers(
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage:
        names = [n for n in CATALOG if cursor is None or n > cursor]
        page = names[: (limit or 100)]
        return ContainerPage(
            containers=[
                ContainerInfo(
                    database=database or "remote_db",
                    container_name=name,
                    container_type=ContainerType.TABLE,
                    estimated_count=7,
                )
                for name in page
            ],
            next_cursor=page[-1] if page and len(names) > len(page) else None,
        )

    @mcp.tool
    def get_schema(container: str) -> list[ColumnInfo]:
        if container in failing:
            raise ValueError(f"unknown container: {container}")
        return [_column("id"), _column("label", 2)]

    @mcp.tool
    def get_sample(container: str, limit: int = 3) -> list[dict]:
        return [{"id": index} for index in range(limit)]

    @mcp.tool
    def profile_column(container: str, column: str, mode: str) -> ProfileResult:
        if slow is not None:
            slow.wait(10)
        return ProfileResult(null_ratio=0.25, approximate=True)

    return mcp


@pytest.fixture
def adapter() -> Iterator[RemoteMcpAdapter]:
    with RemoteMcpAdapter(Client(_remote()), database="remote_db") as opened:
        yield opened


# ---- contract ----


def test_it_is_a_source_adaptor(adapter: RemoteMcpAdapter):
    checked: SourceAdaptor = adapter
    assert isinstance(checked, SourceAdaptor)


def test_the_factory_can_build_it():
    from src.core.config import SourceEngine
    from src.service.factory import AdapterFactory, load_adapter_class

    cls = load_adapter_class(SourceEngine.MCP)

    assert cls is RemoteMcpAdapter
    assert isinstance(cls, AdapterFactory)


# ---- tools ----


def test_list_databases(adapter: RemoteMcpAdapter):
    assert adapter.list_databases() == ["remote_db", "other_db"]


def test_list_containers_returns_a_page(adapter: RemoteMcpAdapter):
    page = adapter.list_containers()

    assert [c.container_name for c in page.containers] == CATALOG
    assert page.next_cursor is None
    assert page.containers[0].estimated_count == 7


def test_the_cursor_is_forwarded_to_the_remote(adapter: RemoteMcpAdapter):
    """Without this the remote catalog cannot be paged, which is the whole
    reason `list_containers` takes a cursor."""
    first = adapter.list_containers(limit=2)

    assert [c.container_name for c in first.containers] == ["alpha", "beta"]
    assert first.next_cursor == "beta"

    second = adapter.list_containers(limit=2, cursor=first.next_cursor)

    assert [c.container_name for c in second.containers] == ["gamma"]
    assert second.next_cursor is None


def test_page_size_is_capped_locally(adapter: RemoteMcpAdapter):
    page = adapter.list_containers(limit=10_000)

    assert len(page.containers) == len(CATALOG)


def test_get_schema(adapter: RemoteMcpAdapter):
    columns = adapter.get_schema("alpha")

    assert [c.name for c in columns] == ["id", "label"]
    assert columns[0].is_pk is True
    assert columns[1].ordinal == 2


def test_get_sample(adapter: RemoteMcpAdapter):
    assert adapter.get_sample("alpha", limit=2) == [{"id": 0}, {"id": 1}]


def test_sample_limit_is_capped_locally():
    """The remote's own cap is not ours to trust."""
    with RemoteMcpAdapter(Client(_remote()), max_sample_limit=2) as adapter:
        assert len(adapter.get_sample("alpha", limit=99)) == 2


def test_profile_column_keeps_every_field(adapter: RemoteMcpAdapter):
    result = adapter.profile_column("alpha", "id", ProfileMode.NULL_RATIO)

    assert result.null_ratio == 0.25
    assert result.approximate is True


def test_no_database_argument_is_sent_to_the_per_container_tools(
    adapter: RemoteMcpAdapter,
):
    """The project's own contract has no `database` on these, so sending one is
    an unexpected-keyword error on the remote."""
    assert adapter.get_schema("alpha")
    assert adapter.get_sample("alpha")
    assert adapter.profile_column("alpha", "id", ProfileMode.MIN_MAX)


# ---- errors ----


def test_a_remote_failure_becomes_our_own_error_type(adapter: RemoteMcpAdapter):
    """fastmcp's ToolError must not leak into the adapter contract."""
    with RemoteMcpAdapter(Client(_remote(fail={"broken"}))) as failing:
        with pytest.raises(RemoteCallError, match="remote get_schema failed"):
            failing.get_schema("broken")


def test_an_unknown_tool_is_a_remote_call_error(adapter: RemoteMcpAdapter):
    with pytest.raises(RemoteCallError):
        adapter._call("no_such_tool", {})


def test_a_slow_remote_times_out_instead_of_hanging():
    gate = threading.Event()
    adapter = RemoteMcpAdapter(Client(_remote(slow=gate)), timeout=0.2)
    try:
        with pytest.raises(RemoteCallError, match="timed out"):
            adapter.profile_column("alpha", "id", ProfileMode.NULL_RATIO)
    finally:
        gate.set()
        adapter.close()


# ---- lifecycle ----


def test_connecting_is_lazy_and_happens_once():
    adapter = RemoteMcpAdapter(Client(_remote()))
    try:
        assert adapter._connected is False
        adapter.list_databases()
        assert adapter._connected is True
        adapter.list_databases()
    finally:
        adapter.close()


def test_ping_is_a_real_round_trip(adapter: RemoteMcpAdapter):
    assert adapter.ping() is True


def test_ping_is_false_once_closed():
    adapter = RemoteMcpAdapter(Client(_remote()))
    adapter.list_databases()
    adapter.close()

    assert adapter.ping() is False


def test_using_a_closed_adapter_says_so():
    """It used to surface as `RuntimeError: Event loop is closed`."""
    adapter = RemoteMcpAdapter(Client(_remote()))
    adapter.list_databases()
    adapter.close()

    with pytest.raises(RemoteCallError, match="closed"):
        adapter.list_databases()


def test_close_is_idempotent():
    adapter = RemoteMcpAdapter(Client(_remote()))
    adapter.list_databases()

    adapter.close()
    adapter.close()


def test_close_without_ever_connecting_is_harmless():
    RemoteMcpAdapter(Client(_remote())).close()


# ---- concurrency ----


def test_concurrent_calls_from_many_threads_all_succeed(adapter: RemoteMcpAdapter):
    """
    The pool hands one adapter to concurrent callers, and FastMCP runs sync
    tools in a threadpool. A shared `run_until_complete` raised "this event loop
    is already running" here.
    """
    barrier = threading.Barrier(8)
    results: list[object] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            value = adapter.list_databases()
        except Exception as exc:  # noqa: BLE001 - reported, not raised in a thread
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")
            return
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert errors == []
    assert len(results) == 8


def test_concurrent_first_use_connects_once():
    adapter = RemoteMcpAdapter(Client(_remote()))
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        adapter.list_databases()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        assert adapter._connected is True
    finally:
        adapter.close()


def test_the_loop_thread_does_not_outlive_the_adapter():
    before = threading.active_count()
    adapter = RemoteMcpAdapter(Client(_remote()))
    adapter.list_databases()
    adapter.close()

    for _ in range(200):
        if threading.active_count() == before:
            break
        threading.Event().wait(0.01)

    assert threading.active_count() == before


# ---- audit ----


def test_tool_calls_are_recorded_for_the_audit_layer(adapter: RemoteMcpAdapter):
    adapter.pop_rendered_sql()

    adapter.list_databases()
    adapter.get_schema("alpha")

    rendered = adapter.pop_rendered_sql() or ""
    assert "call list_databases()" in rendered
    assert "call get_schema(container)" in rendered
    assert adapter.pop_rendered_sql() is None


# ---- from_connection ----


def test_from_connection_requires_a_target():
    with pytest.raises(ValueError, match="requires a <REF>_URI"):
        RemoteMcpAdapter.from_connection(ConnectionInfo(host="localhost"))


def test_from_connection_takes_the_database_name():
    adapter = RemoteMcpAdapter.from_connection(
        ConnectionInfo(uri="http://example.invalid/mcp", database="upstream")
    )
    try:
        assert adapter.list_databases  # not called: nothing is listening
        assert adapter._database == "upstream"
    finally:
        adapter.close()


def test_from_connection_attaches_a_token():
    adapter = RemoteMcpAdapter.from_connection(
        ConnectionInfo(uri="http://example.invalid/mcp", token="secret")
    )
    try:
        assert adapter._client.transport.auth is not None
    finally:
        adapter.close()


def test_from_connection_without_a_token_stays_anonymous():
    adapter = RemoteMcpAdapter.from_connection(
        ConnectionInfo(uri="http://example.invalid/mcp")
    )
    try:
        assert adapter._client.transport.auth is None
    finally:
        adapter.close()
