import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.config import ConnectionInfo, SourceEngine
from src.core.contracts import ColumnInfo, ContainerPage, ProfileMode, ProfileResult
from src.service import pool as pool_module
from src.service.factory import UnknownEngineError
from src.service.pool import (
    AdapterPool,
    AdapterProvider,
    SingleAdapter,
    UnknownDatabaseError,
)

CONN = ConnectionInfo(host="localhost", database="main")


def _fake(adapter: object) -> "FakeAdapter":
    """`get()` is typed as SourceAdaptor; tests need the recording fields."""
    assert isinstance(adapter, FakeAdapter)
    return adapter


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeAdapter:
    def __init__(self, database: str | None = None) -> None:
        self.database = database
        self.close_calls = 0
        self.alive = True
        self.ping_calls = 0
        self.ping_error: Exception | None = None

    def list_databases(self) -> list[str]:
        return [self.database or "default"]

    def list_containers(
        self,
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage:
        return ContainerPage(containers=[])

    def get_schema(self, container: str) -> list[ColumnInfo]:
        return []

    def get_sample(self, container: str, limit: int = 3) -> list[dict[str, Any]]:
        return []

    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult:
        return ProfileResult()

    def close(self) -> None:
        self.close_calls += 1

    def pop_rendered_sql(self) -> str | None:
        return None

    def ping(self) -> bool:
        self.ping_calls += 1
        if self.ping_error is not None:
            raise self.ping_error
        return self.alive


class RecordingFactory:
    def __init__(self, *, supports_multiple: bool = True, delay: float = 0.0) -> None:
        self.created: list[FakeAdapter] = []
        self.databases: list[str | None] = []
        self.max_sample_limits: list[int | None] = []
        self.supports_multiple = supports_multiple
        self.delay = delay

    def create(
        self,
        engine,
        conn_info,
        *,
        database: str | None = None,
        max_sample_limit: int | None = None,
    ) -> FakeAdapter:
        if self.delay:
            time.sleep(self.delay)
        adapter = FakeAdapter(database)
        self.created.append(adapter)
        self.databases.append(database)
        self.max_sample_limits.append(max_sample_limit)
        return adapter

    def load_class(self, engine):
        return SimpleNamespace(SUPPORTS_MULTIPLE_DATABASES=self.supports_multiple)


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> RecordingFactory:
    recorder = RecordingFactory()
    monkeypatch.setattr(pool_module, "create_adapter", recorder.create)
    monkeypatch.setattr(pool_module, "load_adapter_class", recorder.load_class)
    return recorder


def _slow_factory(monkeypatch: pytest.MonkeyPatch, delay: float) -> RecordingFactory:
    recorder = RecordingFactory(delay=delay)
    monkeypatch.setattr(pool_module, "create_adapter", recorder.create)
    monkeypatch.setattr(pool_module, "load_adapter_class", recorder.load_class)
    return recorder


# ---- construction ----


def test_both_implementations_satisfy_the_provider_protocol(factory: RecordingFactory):
    assert isinstance(AdapterPool(SourceEngine.POSTGRES, CONN), AdapterProvider)
    assert isinstance(SingleAdapter(FakeAdapter()), AdapterProvider)


def test_unknown_engine_is_rejected_at_construction():
    with pytest.raises(UnknownEngineError):
        AdapterPool("oracle", CONN)


def test_max_size_must_be_positive():
    with pytest.raises(ValueError, match="max_size must be at least 1"):
        AdapterPool(SourceEngine.POSTGRES, CONN, max_size=0)


def test_default_database_falls_back_to_the_connection(factory: RecordingFactory):
    AdapterPool(SourceEngine.POSTGRES, CONN).get()
    assert factory.databases == ["main"]


def test_explicit_default_database_wins(factory: RecordingFactory):
    AdapterPool(SourceEngine.POSTGRES, CONN, default_database="other").get()
    assert factory.databases == ["other"]


def test_max_sample_limit_is_forwarded(factory: RecordingFactory):
    AdapterPool(SourceEngine.POSTGRES, CONN, max_sample_limit=7).get()
    assert factory.max_sample_limits == [7]


# ---- caching ----


def test_the_same_database_is_only_built_once(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)

    first = _fake(pool.get("db_a"))
    second = _fake(pool.get("db_a"))

    assert first is second
    assert len(factory.created) == 1


def test_default_and_explicit_default_share_one_adapter(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)

    assert pool.get() is pool.get("main")
    assert len(factory.created) == 1


def test_different_databases_get_different_adapters(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)

    assert pool.get("db_a") is not pool.get("db_b")
    assert factory.databases == ["db_a", "db_b"]


def test_list_databases_uses_the_default_adapter(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)

    assert pool.list_databases() == ["main"]
    assert len(factory.created) == 1


# ---- bounded size ----


def test_reaching_max_size_evicts_and_closes_the_least_recently_used(
    factory: RecordingFactory,
):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, max_size=2)

    a = _fake(pool.get("a"))
    pool.get("b")
    pool.get("c")

    assert a.close_calls == 1, "the evicted adapter must be closed, not dropped"
    assert len(factory.created) == 3
    assert pool.get("a") is not a
    assert len(factory.created) == 4


def test_eviction_order_follows_use_not_insertion(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, max_size=2)

    a = _fake(pool.get("a"))
    b = _fake(pool.get("b"))
    pool.get("a")  # a is now the most recently used
    pool.get("c")

    assert b.close_calls == 1
    assert a.close_calls == 0


def test_a_pool_of_one_still_works(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, max_size=1)

    a = _fake(pool.get("a"))
    pool.get("b")

    assert a.close_calls == 1
    assert len(pool._adapters) == 1


# ---- single-database engines ----


def test_single_database_engine_rejects_a_different_database(
    monkeypatch: pytest.MonkeyPatch,
):
    recorder = RecordingFactory(supports_multiple=False)
    monkeypatch.setattr(pool_module, "create_adapter", recorder.create)
    monkeypatch.setattr(pool_module, "load_adapter_class", recorder.load_class)
    pool = AdapterPool(SourceEngine.DATALAKE, CONN)

    assert pool.get() is pool.get("main")
    with pytest.raises(UnknownDatabaseError, match="single database"):
        pool.get("somewhere_else")
    assert len(recorder.created) == 1


def test_datalake_through_the_real_factory_is_single_database(tmp_path: Path):
    pool = AdapterPool(SourceEngine.DATALAKE, ConnectionInfo(path=str(tmp_path)))

    with pytest.raises(UnknownDatabaseError):
        pool.get("other_lake")


def test_engine_without_a_default_database_keys_on_the_empty_string(
    factory: RecordingFactory,
):
    pool = AdapterPool(SourceEngine.SQLITE, ConnectionInfo(path="/tmp/x.db"))

    pool.get()

    assert list(pool._adapters) == [""]
    assert factory.databases == [None]


# ---- close ----


def test_close_closes_everything_and_empties_the_pool(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)
    a, b = _fake(pool.get("a")), _fake(pool.get("b"))

    pool.close()

    assert (a.close_calls, b.close_calls) == (1, 1)
    assert pool._adapters == {}


def test_close_is_idempotent(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)
    a = _fake(pool.get("a"))

    pool.close()
    pool.close()

    assert a.close_calls == 1


def test_one_failing_close_does_not_strand_the_others(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)
    broken = _fake(pool.get("broken"))
    healthy = _fake(pool.get("healthy"))

    def explode() -> None:
        raise RuntimeError("connection already gone")

    broken.close = explode  # type: ignore[method-assign]

    pool.close()

    assert healthy.close_calls == 1
    assert pool._adapters == {}


def test_get_after_close_rebuilds(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)
    first = _fake(pool.get("a"))
    pool.close()

    assert pool.get("a") is not first


# ---- concurrency ----


def test_concurrent_gets_build_exactly_one_adapter(monkeypatch: pytest.MonkeyPatch):
    """Without a lock, the losers of the race leak a connection each."""
    recorder = _slow_factory(monkeypatch, delay=0.02)
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)

    threads_count = 16
    barrier = threading.Barrier(threads_count)
    results: list[FakeAdapter] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        adapter = _fake(pool.get("hot"))
        with lock:
            results.append(adapter)

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(recorder.created) == 1
    assert len(set(map(id, results))) == 1


def test_concurrent_gets_on_different_databases_all_succeed(
    monkeypatch: pytest.MonkeyPatch,
):
    recorder = _slow_factory(monkeypatch, delay=0.005)
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, max_size=8)

    names = [f"db_{i}" for i in range(8)]
    barrier = threading.Barrier(len(names))

    def worker(name: str) -> None:
        barrier.wait()
        pool.get(name)

    threads = [threading.Thread(target=worker, args=(n,)) for n in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(recorder.created) == len(names)
    assert set(pool._adapters) == set(names)


# ---- SingleAdapter ----


def test_single_adapter_hands_back_what_it_wraps():
    adapter = FakeAdapter("main")
    provider = SingleAdapter(adapter, database="main")

    assert provider.get() is adapter
    assert provider.get("main") is adapter


def test_single_adapter_rejects_another_database():
    provider = SingleAdapter(FakeAdapter("main"), database="main")

    with pytest.raises(UnknownDatabaseError, match="only serves 'main'"):
        provider.get("other")


def test_single_adapter_delegates():
    adapter = FakeAdapter("main")
    provider = SingleAdapter(adapter, database="main")

    assert provider.list_databases() == ["main"]
    provider.close()
    assert adapter.close_calls == 1


def test_single_adapter_accepts_the_name_its_adapter_reports():
    """A caller must be able to pass back a name it just read from
    list_databases; the server advertises those names."""
    adapter = FakeAdapter("main")
    provider = SingleAdapter(adapter)

    assert provider.get() is adapter
    assert provider.get("main") is adapter
    with pytest.raises(UnknownDatabaseError, match="anything"):
        provider.get("anything")


# ---- validation on borrow ----


def test_a_cached_adapter_is_pinged_before_being_handed_out(
    factory: RecordingFactory,
):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)

    first = _fake(pool.get("a"))
    assert first.ping_calls == 0, "no point pinging an adapter we just built"

    assert pool.get("a") is first
    assert first.ping_calls == 1


def test_a_dead_adapter_is_replaced_not_handed_out(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)
    dead = _fake(pool.get("a"))
    dead.alive = False

    replacement = _fake(pool.get("a"))

    assert replacement is not dead
    assert dead.close_calls == 1
    assert len(factory.created) == 2


def test_a_ping_that_raises_counts_as_dead(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN)
    dead = _fake(pool.get("a"))
    dead.ping_error = RuntimeError("server closed the connection")

    replacement = _fake(pool.get("a"))

    assert replacement is not dead
    assert dead.close_calls == 1


# ---- idle expiry ----


def test_an_idle_adapter_is_closed_on_the_next_get(factory: RecordingFactory):
    clock = FakeClock()
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, idle_timeout=600, clock=clock)
    idle = _fake(pool.get("a"))

    clock.advance(601)
    pool.get("b")

    assert idle.close_calls == 1
    assert list(pool._adapters) == ["b"]


def test_an_adapter_still_inside_the_timeout_survives(factory: RecordingFactory):
    clock = FakeClock()
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, idle_timeout=600, clock=clock)
    fresh = _fake(pool.get("a"))

    clock.advance(599)

    assert pool.get("a") is fresh
    assert fresh.close_calls == 0


def test_using_an_adapter_resets_its_idle_clock(factory: RecordingFactory):
    clock = FakeClock()
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, idle_timeout=600, clock=clock)
    kept = _fake(pool.get("a"))

    for _ in range(5):
        clock.advance(500)
        assert pool.get("a") is kept

    assert kept.close_calls == 0
    assert len(factory.created) == 1


def test_only_the_expired_entries_are_swept(factory: RecordingFactory):
    clock = FakeClock()
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, idle_timeout=600, clock=clock)

    old = _fake(pool.get("old"))
    clock.advance(400)
    recent = _fake(pool.get("recent"))
    clock.advance(300)  # old is 700s idle, recent is 300s
    pool.get("trigger")

    assert old.close_calls == 1
    assert recent.close_calls == 0
    assert set(pool._adapters) == {"recent", "trigger"}


def test_idle_timeout_can_be_disabled(factory: RecordingFactory):
    clock = FakeClock()
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, idle_timeout=None, clock=clock)
    kept = _fake(pool.get("a"))

    clock.advance(10_000)
    pool.get("b")

    assert kept.close_calls == 0
    assert set(pool._adapters) == {"a", "b"}


def test_idle_timeout_must_be_positive():
    with pytest.raises(ValueError, match="idle_timeout must be positive"):
        AdapterPool(SourceEngine.POSTGRES, CONN, idle_timeout=0)


def test_reap_closes_idle_adapters_without_any_traffic(factory: RecordingFactory):
    clock = FakeClock()
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, idle_timeout=600, clock=clock)
    idle = _fake(pool.get("a"))

    assert pool.reap() == 0

    clock.advance(601)

    assert pool.reap() == 1
    assert idle.close_calls == 1
    assert pool._adapters == {}
    assert len(factory.created) == 1, "reaping must not build anything"


def test_default_idle_timeout_is_ten_minutes():
    assert AdapterPool.DEFAULT_IDLE_TIMEOUT == 600.0


# ---- reaper thread ----


def test_the_reaper_closes_idle_adapters_with_no_traffic(factory: RecordingFactory):
    clock = FakeClock()
    pool = AdapterPool(
        SourceEngine.POSTGRES,
        CONN,
        idle_timeout=600,
        reaper_interval=0.01,
        clock=clock,
    )
    idle = _fake(pool.get("a"))
    clock.advance(601)

    pool.start_reaper()
    try:
        deadline = time.monotonic() + 2.0
        while idle.close_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        pool.stop_reaper()

    assert idle.close_calls == 1, "the reaper must not need a request to fire"
    assert pool._adapters == {}


def test_start_reaper_is_idempotent(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, reaper_interval=0.01)
    try:
        pool.start_reaper()
        thread = pool._reaper
        pool.start_reaper()

        assert pool._reaper is thread
    finally:
        pool.stop_reaper()


def test_stop_reaper_actually_joins_the_thread(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, reaper_interval=30)
    pool.start_reaper()
    thread = pool._reaper
    assert pool.reaper_running

    pool.stop_reaper(timeout=2.0)

    assert thread is not None and not thread.is_alive()
    assert not pool.reaper_running


def test_stop_reaper_without_start_is_harmless(factory: RecordingFactory):
    AdapterPool(SourceEngine.POSTGRES, CONN).stop_reaper()


def test_no_reaper_when_idle_expiry_is_disabled(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, idle_timeout=None)

    pool.start_reaper()

    assert not pool.reaper_running


def test_close_stops_the_reaper(factory: RecordingFactory):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, reaper_interval=30)
    pool.start_reaper()
    thread = pool._reaper

    pool.close()

    assert thread is not None and not thread.is_alive()


def test_the_pool_works_as_a_context_manager(factory: RecordingFactory):
    with AdapterPool(SourceEngine.POSTGRES, CONN, reaper_interval=30) as pool:
        adapter = _fake(pool.get("a"))
        thread = pool._reaper
        assert pool.reaper_running

    assert adapter.close_calls == 1
    assert thread is not None and not thread.is_alive()
    assert pool._adapters == {}


def test_the_reaper_is_a_daemon_so_a_forgotten_stop_cannot_hang_exit(
    factory: RecordingFactory,
):
    pool = AdapterPool(SourceEngine.POSTGRES, CONN, reaper_interval=30)
    try:
        pool.start_reaper()
        assert pool._reaper is not None and pool._reaper.daemon
    finally:
        pool.stop_reaper()
