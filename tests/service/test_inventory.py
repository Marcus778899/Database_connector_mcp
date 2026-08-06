import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.core.contracts import (
    ColumnInfo,
    ContainerInfo,
    ContainerPage,
    ContainerType,
    ProfileMode,
    ProfileResult,
    Sensitivity,
)
from src.service.inventory import (
    InventoryService,
    ScanAlreadyRunningError,
    UnknownJobError,
)
from src.service.staging import StagingStore


class FakeAdapter:
    def __init__(
        self,
        containers: dict[str, list[str]],
        *,
        database: str = "main",
        page_size_seen: list[int] | None = None,
    ) -> None:
        self.catalog = containers
        self.database = database
        self.fail_schema_of: set[str] = set()
        self.fail_profile_of: set[str] = set()
        self.before_schema: dict[str, threading.Event] = {}
        self.schema_calls: list[str] = []
        self.profile_calls: list[tuple[str, str, str]] = []
        self.page_size_seen = page_size_seen if page_size_seen is not None else []
        # what this engine would pick for a column when the caller names nothing
        self.chosen_modes: tuple[ProfileMode, ...] = (ProfileMode.NULL_RATIO,)
        self.distinct_count = 1
        self.column_type = "TEXT"
        self.rows: list[dict] = []
        self.fail_sample = False
        self.sample_calls: list[str] = []

    def list_databases(self) -> list[str]:
        return [self.database]

    def list_containers(
        self, database=None, schema=None, limit=None, cursor=None
    ) -> ContainerPage:
        self.page_size_seen.append(limit or 0)
        names = sorted(self.catalog)
        if cursor is not None:
            names = [n for n in names if n > cursor]
        size = limit or 100
        page, has_more = names[:size], len(names) > size
        return ContainerPage(
            containers=[
                ContainerInfo(
                    database=self.database,
                    container_name=name,
                    container_type=ContainerType.TABLE,
                    estimated_count=10,
                )
                for name in page
            ],
            next_cursor=page[-1] if has_more and page else None,
        )

    def get_schema(self, container: str) -> list[ColumnInfo]:
        self.schema_calls.append(container)
        gate = self.before_schema.get(container)
        if gate is not None:
            gate.wait(5)
        if container in self.fail_schema_of:
            raise PermissionError(f"no access to {container}")
        return [
            ColumnInfo(
                name=name,
                ordinal=index,
                native_type=self.column_type,
                nullable=True,
                is_pk=False,
                is_fk=False,
            )
            for index, name in enumerate(self.catalog[container], start=1)
        ]

    def get_sample(self, container: str, limit: int = 3) -> list[dict]:
        self.sample_calls.append(container)
        if self.fail_sample:
            raise PermissionError("no rows for you")
        return self.rows[:limit]

    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult:
        self.profile_calls.append((container, column, str(mode)))
        if container in self.fail_profile_of:
            raise RuntimeError("profiling blew up")
        if mode == ProfileMode.DISTINCT_COUNT:
            return ProfileResult(distinct_count=self.distinct_count)
        return ProfileResult(null_ratio=0.5)

    def default_profile_modes(self, column: ColumnInfo) -> tuple[ProfileMode, ...]:
        return self.chosen_modes

    def close(self) -> None:
        pass

    def pop_rendered_sql(self) -> str | None:
        return None

    def ping(self) -> bool:
        return True


class FakeProvider:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter

    def get(self, database: str | None = None) -> FakeAdapter:
        return self.adapter

    def list_databases(self) -> list[str]:
        return self.adapter.list_databases()

    def close(self) -> None:
        self.adapter.close()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StagingStore]:
    with StagingStore(tmp_path / "staging.db") as opened:
        yield opened


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter({"orders": ["id", "total"], "users": ["id", "email"]})


def _service(adapter: FakeAdapter, store: StagingStore, **kwargs) -> InventoryService:
    return InventoryService(FakeProvider(adapter), store, **kwargs)


def _wait_until(predicate, timeout: float = 5.0) -> None:
    """Poll rather than sleep, so the test is not racy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition never became true")


# ---- a plain run ----


def test_a_scan_returns_immediately_and_finishes_in_the_background(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)

    job_id = service.start("main")
    status = service.wait(job_id, timeout=10)

    assert status.state == "done"
    assert status.containers_done == 2
    assert status.finished_at is not None


def test_the_scan_writes_the_inventory_to_staging(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    summary = store.summary("main")
    assert summary.containers == 2
    assert summary.columns == 4
    assert [c.column_name for c in store.columns("main", "users").columns] == [
        "id",
        "email",
    ]


def test_status_tracks_the_last_container_processed(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)
    job_id = service.start("main")
    service.wait(job_id, timeout=10)

    status = service.status(job_id)
    assert status.cursor == "users"  # last in sorted order
    assert status.database == "main"
    assert status.started_at is not None


def test_an_unknown_job_is_an_error(adapter: FakeAdapter, store: StagingStore):
    with pytest.raises(UnknownJobError):
        _service(adapter, store).status("nope")


def test_pages_are_followed_to_the_end(store: StagingStore):
    adapter = FakeAdapter({f"t{i:02d}": ["id"] for i in range(10)})
    service = _service(adapter, store, page_size=3)

    status = service.wait(service.start("main"), timeout=10)

    assert status.state == "done"
    assert status.containers_done == 10
    assert store.summary("main").containers == 10


def test_the_configured_page_size_reaches_the_adapter(store: StagingStore):
    adapter = FakeAdapter({"a": ["id"]})
    service = _service(adapter, store, page_size=7)

    service.wait(service.start("main"), timeout=10)

    assert adapter.page_size_seen == [7]


# ---- incremental ----


def test_a_second_scan_skips_containers_that_did_not_change(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)
    adapter.schema_calls.clear()

    status = service.wait(service.start("main", resume=False), timeout=10)

    assert status.containers_skipped == 2
    assert status.containers_done == 0
    assert sorted(adapter.schema_calls) == ["orders", "users"]


def test_a_changed_schema_is_rescanned(adapter: FakeAdapter, store: StagingStore):
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    adapter.catalog["users"] = ["id", "email", "created_at"]
    status = service.wait(service.start("main", resume=False), timeout=10)

    assert status.containers_done == 1
    assert status.containers_skipped == 1
    assert len(store.columns("main", "users").columns) == 3


def test_force_rescans_even_when_nothing_changed(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    status = service.wait(service.start("main", resume=False, force=True), timeout=10)

    assert status.containers_done == 2
    assert status.containers_skipped == 0


# ---- resume ----


def test_a_scan_resumes_after_the_last_finished_container(
    adapter: FakeAdapter, store: StagingStore
):
    """A run that died at container 7000 should not start over."""
    store.create_scan("crashed", "main")
    store.advance_scan("crashed", cursor="orders", done=1, failed=0, skipped=0)
    store.finish_scan("crashed", "failed", error="connection lost")

    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    assert adapter.schema_calls == ["users"], "orders was already done"


def test_resume_can_be_turned_off(adapter: FakeAdapter, store: StagingStore):
    store.create_scan("crashed", "main")
    store.advance_scan("crashed", cursor="orders", done=1, failed=0, skipped=0)
    store.finish_scan("crashed", "failed")

    service = _service(adapter, store)
    service.wait(service.start("main", resume=False), timeout=10)

    assert sorted(adapter.schema_calls) == ["orders", "users"]


def test_a_finished_scan_is_not_resumed_from(adapter: FakeAdapter, store: StagingStore):
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)
    adapter.schema_calls.clear()

    service.wait(service.start("main"), timeout=10)

    assert sorted(adapter.schema_calls) == ["orders", "users"]


# ---- failure isolation ----


def test_one_unreadable_container_does_not_abandon_the_rest(
    adapter: FakeAdapter, store: StagingStore
):
    adapter.fail_schema_of = {"orders"}
    service = _service(adapter, store)

    status = service.wait(service.start("main"), timeout=10)

    assert status.state == "done"
    assert (status.containers_done, status.containers_failed) == (1, 1)
    rows = {r.container_name: r for r in store.containers("main").containers}
    assert "PermissionError" in (rows["orders"].error or "")
    assert rows["users"].error is None


def test_a_failed_container_is_retried_on_the_next_scan(
    adapter: FakeAdapter, store: StagingStore
):
    adapter.fail_schema_of = {"orders"}
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    adapter.fail_schema_of.clear()
    status = service.wait(service.start("main", resume=False), timeout=10)

    assert status.containers_done == 1  # orders, now readable
    assert store.containers("main").containers[0].error is None


def test_a_failure_in_the_catalog_itself_marks_the_scan_failed(
    adapter: FakeAdapter, store: StagingStore
):
    def explode(**kwargs):
        raise ConnectionError("server went away")

    adapter.list_containers = explode  # type: ignore[method-assign]
    service = _service(adapter, store)

    status = service.wait(service.start("main"), timeout=10)

    assert status.state == "failed"
    assert "ConnectionError" in (status.error or "")


# ---- profiling ----


def test_the_adapter_picks_the_modes_when_no_one_else_does(
    adapter: FakeAdapter, store: StagingStore
):
    """Nothing configured is not "gather nothing": it hands the choice to the
    engine, which is the only layer that knows what its type names mean."""
    adapter.chosen_modes = (ProfileMode.MIN_MAX,)
    service = _service(adapter, store)

    service.wait(service.start("main"), timeout=10)

    assert {call[2] for call in adapter.profile_calls} == {"min_max"}
    assert store.summary("main").columns_profiled == 4


def test_the_modes_are_chosen_per_column_not_per_scan(
    adapter: FakeAdapter, store: StagingStore
):
    seen: list[str] = []

    def modes_for(column: ColumnInfo) -> tuple[ProfileMode, ...]:
        seen.append(column.name)
        if column.name == "email":
            return (ProfileMode.TOP_VALUES,)
        return (ProfileMode.MIN_MAX,)

    adapter.default_profile_modes = modes_for  # type: ignore[method-assign]
    service = _service(adapter, store)

    service.wait(service.start("main"), timeout=10)

    assert sorted(seen) == ["email", "id", "id", "total"]
    assert ("users", "email", "top_values") in adapter.profile_calls
    assert ("users", "id", "min_max") in adapter.profile_calls


def test_top_values_is_skipped_when_the_column_has_too_many(
    adapter: FakeAdapter, store: StagingStore
):
    """The twenty commonest values of a column with a million of them describe
    nothing, and the query is the expensive one."""
    adapter.chosen_modes = (ProfileMode.DISTINCT_COUNT, ProfileMode.TOP_VALUES)
    adapter.distinct_count = InventoryService.TOP_VALUES_MAX_DISTINCT + 1
    service = _service(adapter, store)

    service.wait(service.start("main"), timeout=10)

    assert {call[2] for call in adapter.profile_calls} == {"distinct_count"}


def test_top_values_is_gathered_when_the_column_is_narrow(
    adapter: FakeAdapter, store: StagingStore
):
    adapter.chosen_modes = (ProfileMode.DISTINCT_COUNT, ProfileMode.TOP_VALUES)
    adapter.distinct_count = InventoryService.TOP_VALUES_MAX_DISTINCT
    service = _service(adapter, store)

    service.wait(service.start("main"), timeout=10)

    assert {call[2] for call in adapter.profile_calls} == {
        "distinct_count",
        "top_values",
    }


def test_a_caller_who_names_top_values_gets_it_whatever_the_cardinality(
    adapter: FakeAdapter, store: StagingStore
):
    adapter.distinct_count = InventoryService.TOP_VALUES_MAX_DISTINCT * 100
    service = _service(adapter, store)

    service.wait(
        service.start(
            "main",
            profile_modes=[ProfileMode.DISTINCT_COUNT, ProfileMode.TOP_VALUES],
        ),
        timeout=10,
    )

    assert {call[2] for call in adapter.profile_calls} == {
        "distinct_count",
        "top_values",
    }


def test_requested_modes_are_profiled_and_stored(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)

    service.wait(
        service.start("main", profile_modes=[ProfileMode.NULL_RATIO]), timeout=10
    )

    assert len(adapter.profile_calls) == 4  # 2 containers x 2 columns
    assert store.summary("main").columns_profiled == 4
    profile = store.columns("main", "users").columns[0].profile
    assert profile is not None
    assert profile["null_ratio"]["null_ratio"] == 0.5


def test_the_default_modes_apply_when_the_caller_names_none(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store, default_profile_modes=[ProfileMode.NULL_RATIO])

    service.wait(service.start("main"), timeout=10)

    assert len(adapter.profile_calls) == 4  # 2 containers x 2 columns


def test_the_caller_overrides_the_default_modes(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store, default_profile_modes=[ProfileMode.NULL_RATIO])

    service.wait(service.start("main", profile_modes=[ProfileMode.MIN_MAX]), timeout=10)

    assert {call[2] for call in adapter.profile_calls} == {"min_max"}


def test_an_empty_list_means_gather_nothing(adapter: FakeAdapter, store: StagingStore):
    """Distinct from None, which falls back to the default."""
    service = _service(adapter, store, default_profile_modes=[ProfileMode.NULL_RATIO])

    service.wait(service.start("main", profile_modes=[]), timeout=10)

    assert adapter.profile_calls == []


def test_an_empty_default_means_gather_nothing_either(
    adapter: FakeAdapter, store: StagingStore
):
    """The way a server turns profiling off for good, distinct from leaving it
    unset and getting the engine's per-column choice."""
    service = _service(adapter, store, default_profile_modes=[])

    service.wait(service.start("main"), timeout=10)

    assert adapter.profile_calls == []


def test_a_column_that_cannot_be_profiled_does_not_fail_the_scan(
    adapter: FakeAdapter, store: StagingStore
):
    adapter.fail_profile_of = {"orders"}
    service = _service(adapter, store)

    status = service.wait(
        service.start("main", profile_modes=[ProfileMode.NULL_RATIO]), timeout=10
    )

    assert status.state == "done"
    assert status.containers_done == 2
    assert store.columns("main", "orders").columns[0].profile is None
    assert store.columns("main", "users").columns[0].profile is not None


# ---- concurrency ----


def test_two_scans_of_one_database_are_refused(
    adapter: FakeAdapter, store: StagingStore
):
    gate = threading.Event()
    adapter.before_schema["orders"] = gate
    service = _service(adapter, store)
    first = service.start("main")

    try:
        with pytest.raises(ScanAlreadyRunningError, match="already running"):
            service.start("main")
    finally:
        gate.set()

    assert service.wait(first, timeout=10).state == "done"


def test_a_database_can_be_scanned_again_once_the_first_run_ends(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    assert service.wait(service.start("main"), timeout=10).state == "done"


# ---- cancellation ----


def test_cancelling_stops_the_scan_but_keeps_the_progress(
    adapter: FakeAdapter, store: StagingStore
):
    """Cancel mid-container: it finishes, the scan stops before the next."""
    gate = threading.Event()
    adapter.before_schema["orders"] = gate
    service = _service(adapter, store)
    job_id = service.start("main")

    _wait_until(lambda: adapter.schema_calls == ["orders"])
    assert service.cancel(job_id) is True
    gate.set()

    status = service.wait(job_id, timeout=10)

    assert status.state == "cancelled"
    assert status.containers_done == 1
    assert status.cursor == "orders"
    assert adapter.schema_calls == ["orders"], "users was never started"
    assert store.summary("main").containers == 1


def test_a_cancelled_scan_resumes_where_it_stopped(
    adapter: FakeAdapter, store: StagingStore
):
    store.create_scan("earlier", "main")
    store.advance_scan("earlier", cursor="orders", done=1, failed=0, skipped=0)

    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    assert adapter.schema_calls == ["users"]


def test_cancelling_an_unknown_or_finished_job_returns_false(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)
    job_id = service.start("main")
    service.wait(job_id, timeout=10)

    assert service.cancel(job_id) is False
    assert service.cancel("never-existed") is False


def test_close_stops_everything(adapter: FakeAdapter, store: StagingStore):
    gate = threading.Event()
    adapter.before_schema["orders"] = gate
    service = _service(adapter, store)
    job_id = service.start("main")

    gate.set()
    service.close(timeout=10)

    assert service.status(job_id).state in {"done", "cancelled"}


# ---- change history ----


def test_a_first_scan_records_every_container_as_added(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    changes = store.changes()
    assert {c.container_name for c in changes} == {"orders", "users"}
    assert {c.change_type for c in changes} == {"container_added"}


def test_a_changed_container_records_what_changed(
    adapter: FakeAdapter, store: StagingStore
):
    """`schema_hash` only ever kept the latest state; this is the question a
    data engineer actually asks."""
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)
    adapter.catalog["users"] = ["id", "created_at"]

    service.wait(service.start("main", resume=False), timeout=10)

    change = store.changes()[0]
    assert change.change_type == "schema_changed"
    assert change.container_name == "users"
    assert change.detail == {"added": ["created_at"], "removed": ["email"]}
    assert change.old_hash != change.new_hash


def test_a_retyped_column_is_recorded_as_one(store: StagingStore):
    adapter = FakeAdapter({"users": ["id"]})
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    adapter.column_type = "INTEGER"
    service.wait(service.start("main", resume=False), timeout=10)

    change = store.changes()[0]
    assert change.detail == {"retyped": ["id: TEXT → INTEGER"]}


def test_an_unchanged_container_records_nothing(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    service.wait(service.start("main", resume=False), timeout=10)

    assert len(store.changes()) == 2  # the two additions, and nothing since


def test_a_forced_rescan_of_something_unchanged_invents_no_history(
    adapter: FakeAdapter, store: StagingStore
):
    """`force` is about re-reading the source, not about writing history."""
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    service.wait(service.start("main", resume=False, force=True), timeout=10)

    assert len(store.changes()) == 2


def test_a_container_that_disappeared_is_recorded_and_dropped(
    adapter: FakeAdapter, store: StagingStore
):
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)

    del adapter.catalog["orders"]
    service.wait(service.start("main", resume=False), timeout=10)

    removed = [c for c in store.changes() if c.change_type == "container_removed"]
    assert [c.container_name for c in removed] == ["orders"]
    assert [c.container_name for c in store.containers("main").containers] == ["users"]


def test_a_resumed_scan_does_not_call_what_it_never_looked_at_removed(
    adapter: FakeAdapter, store: StagingStore
):
    """The one kind of error an append-only history cannot take back: a run
    that started from a cursor never saw what came before it."""
    service = _service(adapter, store)
    service.wait(service.start("main"), timeout=10)
    store.create_scan("crashed", "main")
    store.advance_scan("crashed", cursor="orders", done=1, failed=0, skipped=0)
    store.finish_scan("crashed", "failed", error="connection lost")

    service.wait(service.start("main"), timeout=10)

    assert [c.change_type for c in store.changes()] == ["container_added"] * 2
    assert len(store.containers("main").containers) == 2


# ---- sensitivity ----


def test_a_scan_marks_the_columns_whose_names_give_them_away(store: StagingStore):
    adapter = FakeAdapter({"users": ["id", "email", "note"]})
    service = _service(adapter, store)

    service.wait(service.start("main"), timeout=10)

    assert store.sensitivity_of("main", "users") == {"email": Sensitivity.PII}


def test_a_scan_judges_by_the_values_when_the_name_says_nothing(store: StagingStore):
    adapter = FakeAdapter({"users": ["id", "contact"]})
    adapter.rows = [{"id": n, "contact": f"user{n}@x.com"} for n in range(5)]
    service = _service(adapter, store)

    service.wait(service.start("main"), timeout=10)

    assert store.sensitivity_of("main", "users") == {"contact": Sensitivity.PII}


def test_a_source_that_will_not_be_sampled_does_not_fail_the_scan(
    store: StagingStore,
):
    adapter = FakeAdapter({"users": ["id", "contact"]})
    adapter.fail_sample = True
    service = _service(adapter, store)

    status = service.wait(service.start("main"), timeout=10)

    assert status.state == "done"
    assert store.sensitivity_of("main", "users") == {}


def test_nothing_is_sampled_when_the_names_already_settle_it(store: StagingStore):
    """The read costs something and buys nothing once every column is decided."""
    adapter = FakeAdapter({"users": ["email", "phone"]})
    service = _service(adapter, store)

    service.wait(service.start("main"), timeout=10)

    assert adapter.sample_calls == []
