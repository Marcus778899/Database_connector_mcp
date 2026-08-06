import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.core.contracts import (
    ColumnInfo,
    ContainerInfo,
    ContainerType,
    ProfileMode,
    ProfileResult,
    TopValue,
)
from src.service.staging import (
    MARKER,
    NO_SCHEMA,
    SCHEMA_VERSION,
    NotAStagingStoreError,
    StagingPathConflictError,
    StagingStore,
    schema_hash,
)


def _column(
    name: str, ordinal: int = 1, native_type: str = "TEXT", **kwargs
) -> ColumnInfo:
    return ColumnInfo(
        name=name,
        ordinal=ordinal,
        native_type=native_type,
        nullable=kwargs.get("nullable", True),
        is_pk=kwargs.get("is_pk", False),
        is_fk=kwargs.get("is_fk", False),
    )


def _container(name: str = "users", database: str = "main", **kwargs) -> ContainerInfo:
    return ContainerInfo(
        database=database,
        schema_name=kwargs.get("schema_name"),
        container_name=name,
        container_type=kwargs.get("container_type", ContainerType.TABLE),
        estimated_count=kwargs.get("estimated_count"),
    )


def _scan(store: StagingStore, job_id: str) -> dict:
    row = store.get_scan(job_id)
    assert row is not None, f"no scan recorded for {job_id}"
    return row


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StagingStore]:
    with StagingStore(tmp_path / "staging.db") as opened:
        yield opened


# ---- schema_hash ----


def test_hash_is_stable_for_the_same_schema():
    columns = [_column("id"), _column("name", 2)]

    assert schema_hash(columns) == schema_hash(list(columns))


@pytest.mark.parametrize(
    "changed",
    [
        [_column("id_renamed"), _column("name", 2)],
        [_column("id", native_type="INTEGER"), _column("name", 2)],
        [_column("id", 2), _column("name", 1)],
        [_column("id", nullable=False), _column("name", 2)],
        [_column("id", is_pk=True), _column("name", 2)],
        [_column("id")],
    ],
)
def test_hash_changes_when_anything_about_the_schema_changes(changed):
    baseline = schema_hash([_column("id"), _column("name", 2)])

    assert schema_hash(changed) != baseline


def test_hash_of_no_columns_is_defined():
    assert schema_hash([]) == schema_hash([])


# ---- setup ----


def test_the_database_file_and_tables_are_created(tmp_path: Path):
    path = tmp_path / "nested" / "staging.db"

    with StagingStore(path) as store:
        assert path.exists()
        names = {
            row["name"]
            for row in store._rows("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert {"scans", "containers", "columns"} <= names


def test_wal_is_enabled_so_a_reader_is_not_blocked_by_the_scan(store: StagingStore):
    row = store._row("PRAGMA journal_mode")
    assert row is not None and row[0] == "wal"


def test_reopening_keeps_what_was_written(tmp_path: Path):
    path = tmp_path / "staging.db"
    with StagingStore(path) as first:
        first.upsert_container(_container(), hash_="abc")

    with StagingStore(path) as second:
        assert second.stored_schema_hash("main", None, "users") == "abc"


# ---- scans ----


def test_scan_lifecycle(store: StagingStore):
    store.create_scan("job1", "main")
    assert _scan(store, "job1")["state"] == "running"

    store.advance_scan("job1", cursor="users", done=3, failed=1, skipped=2)
    scan = _scan(store, "job1")
    assert (scan["cursor"], scan["containers_done"]) == ("users", 3)
    assert (scan["containers_failed"], scan["containers_skipped"]) == (1, 2)

    store.finish_scan("job1", "done")
    scan = _scan(store, "job1")
    assert scan["state"] == "done"
    assert scan["finished_at"] is not None


def test_unknown_scan_is_none(store: StagingStore):
    assert store.get_scan("nope") is None


def test_finish_records_the_error(store: StagingStore):
    store.create_scan("job1", "main")
    store.finish_scan("job1", "failed", error="OperationalError: gone")

    assert "gone" in _scan(store, "job1")["error"]


def test_only_unfinished_scans_are_resumable(store: StagingStore):
    store.create_scan("finished", "main")
    store.advance_scan("finished", cursor="z", done=1, failed=0, skipped=0)
    store.finish_scan("finished", "done")

    assert store.resumable_scan("main") is None

    store.create_scan("crashed", "main")
    store.advance_scan("crashed", cursor="m", done=5, failed=0, skipped=0)
    store.finish_scan("crashed", "failed", error="boom")

    resumable = store.resumable_scan("main")
    assert resumable is not None
    assert resumable["cursor"] == "m"


def test_a_still_running_scan_is_resumable(store: StagingStore):
    store.create_scan("running", "main")
    store.advance_scan("running", cursor="k", done=2, failed=0, skipped=0)

    resumable = store.resumable_scan("main")
    assert resumable is not None and resumable["cursor"] == "k"


def test_resumable_scans_are_per_database(store: StagingStore):
    store.create_scan("job1", "other")
    store.advance_scan("job1", cursor="x", done=1, failed=0, skipped=0)

    assert store.resumable_scan("main") is None
    assert store.resumable_scan("other") is not None


def test_a_database_of_none_is_matched_not_ignored(store: StagingStore):
    store.create_scan("job1", None)
    store.advance_scan("job1", cursor="x", done=1, failed=0, skipped=0)

    assert store.resumable_scan(None) is not None


# ---- containers ----


def test_upsert_is_idempotent(store: StagingStore):
    store.upsert_container(_container(estimated_count=10), hash_="h1")
    store.upsert_container(_container(estimated_count=20), hash_="h2")

    rows = store.containers("main")
    assert len(rows) == 1
    assert rows[0]["estimated_count"] == 20
    assert rows[0]["schema_hash"] == "h2"


def test_a_missing_schema_name_is_stored_as_the_empty_string(store: StagingStore):
    store.upsert_container(_container(schema_name=None), hash_="h")
    store.upsert_container(_container(schema_name=None), hash_="h")

    rows = store.containers("main")
    assert len(rows) == 1
    assert rows[0]["schema_name"] == NO_SCHEMA


def test_the_same_name_in_two_databases_stays_separate(store: StagingStore):
    store.upsert_container(_container(database="a"), hash_="h")
    store.upsert_container(_container(database="b"), hash_="h")

    assert len(store.containers()) == 2
    assert len(store.containers("a")) == 1


def test_the_same_name_in_two_schemas_stays_separate(store: StagingStore):
    store.upsert_container(_container(schema_name="public"), hash_="h")
    store.upsert_container(_container(schema_name="staging"), hash_="h")

    assert len(store.containers("main")) == 2


def test_stored_hash_is_none_for_an_unknown_container(store: StagingStore):
    assert store.stored_schema_hash("main", None, "ghost") is None


def test_a_failed_container_reports_no_hash_so_it_is_retried(store: StagingStore):
    store.upsert_container(_container(), hash_="", error="PermissionDenied")

    assert store.stored_schema_hash("main", None, "users") is None
    assert store.containers("main")[0]["error"] == "PermissionDenied"


def test_a_container_can_recover_from_a_failure(store: StagingStore):
    store.upsert_container(_container(), hash_="", error="PermissionDenied")
    store.upsert_container(_container(), hash_="h")

    assert store.stored_schema_hash("main", None, "users") == "h"
    assert store.containers("main")[0]["error"] is None


# ---- columns ----


def test_columns_round_trip(store: StagingStore):
    store.upsert_container(_container(), hash_="h")
    store.replace_columns(
        "main",
        None,
        "users",
        [_column("id", 1, "INTEGER", nullable=False, is_pk=True), _column("name", 2)],
    )

    columns = store.columns("main", "users")

    assert [c["column_name"] for c in columns] == ["id", "name"]
    assert columns[0]["nullable"] is False
    assert columns[0]["is_pk"] is True
    assert columns[1]["is_pk"] is False
    assert columns[0]["profile"] is None


def test_columns_come_back_in_ordinal_order(store: StagingStore):
    store.replace_columns(
        "main", None, "users", [_column("z", 1), _column("a", 2), _column("m", 3)]
    )

    assert [c["column_name"] for c in store.columns("main", "users")] == ["z", "a", "m"]


def test_replacing_columns_drops_the_ones_that_disappeared(store: StagingStore):
    store.replace_columns("main", None, "users", [_column("id"), _column("legacy", 2)])

    store.replace_columns("main", None, "users", [_column("id")])

    assert [c["column_name"] for c in store.columns("main", "users")] == ["id"]


def test_columns_of_one_container_do_not_affect_another(store: StagingStore):
    store.replace_columns("main", None, "users", [_column("id")])
    store.replace_columns("main", None, "orders", [_column("order_id")])

    store.replace_columns("main", None, "users", [_column("id"), _column("email", 2)])

    assert len(store.columns("main", "orders")) == 1
    assert len(store.columns("main", "users")) == 2


# ---- profiles ----


def test_profiles_from_several_modes_are_merged(store: StagingStore):
    store.replace_columns("main", None, "users", [_column("id")])

    store.record_profile(
        "main",
        None,
        "users",
        "id",
        ProfileMode.NULL_RATIO,
        ProfileResult(null_ratio=0.25),
    )
    store.record_profile(
        "main",
        None,
        "users",
        "id",
        ProfileMode.TOP_VALUES,
        ProfileResult(top_values=[TopValue(value="a", count=2)]),
    )

    profile = store.columns("main", "users")[0]["profile"]
    assert profile["null_ratio"]["null_ratio"] == 0.25
    assert profile["top_values"]["top_values"][0]["value"] == "a"


def test_reprofiling_the_same_mode_overwrites(store: StagingStore):
    store.replace_columns("main", None, "users", [_column("id")])

    for ratio in (0.1, 0.9):
        store.record_profile(
            "main",
            None,
            "users",
            "id",
            ProfileMode.NULL_RATIO,
            ProfileResult(null_ratio=ratio),
        )

    profile = store.columns("main", "users")[0]["profile"]
    assert profile["null_ratio"]["null_ratio"] == 0.9


def test_profiling_an_unknown_column_is_ignored(store: StagingStore):
    store.record_profile(
        "main", None, "users", "ghost", ProfileMode.NULL_RATIO, ProfileResult()
    )

    assert store.columns("main", "users") == []


def test_the_approximate_flag_survives_the_round_trip(store: StagingStore):
    store.replace_columns("main", None, "users", [_column("id")])

    store.record_profile(
        "main",
        None,
        "users",
        "id",
        ProfileMode.DISTINCT_COUNT,
        ProfileResult(distinct_count=5, approximate=True),
    )

    stored = store.columns("main", "users")[0]["profile"]["distinct_count"]
    assert stored["approximate"] is True


# ---- reading ----


def test_summary_counts_instead_of_returning_rows(store: StagingStore):
    for index in range(3):
        store.upsert_container(_container(f"t{index}", estimated_count=100), hash_="h")
        store.replace_columns(
            "main", None, f"t{index}", [_column("id"), _column("v", 2)]
        )
    store.upsert_container(_container("broken"), hash_="", error="denied")
    store.record_profile(
        "main", None, "t0", "id", ProfileMode.NULL_RATIO, ProfileResult(null_ratio=0.0)
    )

    summary = store.summary("main")

    assert summary["containers"] == 4
    assert summary["containers_failed"] == 1
    assert summary["estimated_rows"] == 300
    assert summary["columns"] == 6
    assert summary["columns_profiled"] == 1


def test_summary_of_an_empty_store(store: StagingStore):
    summary = store.summary()

    assert summary["containers"] == 0
    assert summary["columns_profiled"] == 0


def test_summary_can_be_scoped_to_one_database(store: StagingStore):
    store.upsert_container(_container("a", database="one"), hash_="h")
    store.upsert_container(_container("b", database="two"), hash_="h")

    assert store.summary("one")["containers"] == 1
    assert store.summary()["containers"] == 2


def test_containers_are_paged_by_keyset(store: StagingStore):
    for name in ("a", "b", "c", "d"):
        store.upsert_container(_container(name), hash_="h")

    first = store.containers("main", limit=2)
    assert [row["container_name"] for row in first] == ["a", "b"]

    second = store.containers("main", limit=2, cursor="b")
    assert [row["container_name"] for row in second] == ["c", "d"]

    assert store.containers("main", limit=2, cursor="d") == []


# ---- concurrency ----


def test_writes_from_several_threads_are_serialised(store: StagingStore):
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        barrier.wait()
        store.upsert_container(_container(f"t{index}"), hash_="h")
        store.replace_columns("main", None, f"t{index}", [_column("id")])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.summary("main")["containers"] == 8
    assert store.summary("main")["columns"] == 8


def test_a_reader_sees_what_a_writer_committed(store: StagingStore):
    done = threading.Event()

    def writer() -> None:
        store.upsert_container(_container("written"), hash_="h")
        done.set()

    threading.Thread(target=writer).start()
    assert done.wait(5)

    assert [row["container_name"] for row in store.containers("main")] == ["written"]


def test_stored_json_is_valid(store: StagingStore):
    store.replace_columns("main", None, "users", [_column("id")])
    store.record_profile(
        "main", None, "users", "id", ProfileMode.MIN_MAX, ProfileResult(min_value="1")
    )

    row = store._row("SELECT profile FROM columns WHERE container_name='users'")
    assert row is not None
    raw = row["profile"]
    assert json.loads(raw)["min_max"]["min_value"] == "1"


# ---- guards ----


def test_the_store_marks_the_files_it_creates(tmp_path: Path):
    with StagingStore(tmp_path / "staging.db") as store:
        row = store._row("SELECT marker, version FROM staging_meta")

    assert row is not None
    assert (row["marker"], row["version"]) == (MARKER, SCHEMA_VERSION)


def test_reopening_our_own_file_is_fine(tmp_path: Path):
    path = tmp_path / "staging.db"
    with StagingStore(path):
        pass
    with StagingStore(path) as store:
        assert len(store._rows("SELECT * FROM staging_meta")) == 1


def test_a_populated_database_without_our_marker_is_refused(tmp_path: Path):
    """The accident worth preventing: staging pointed at a source database."""
    source = tmp_path / "upstream.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE customers (id INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(NotAStagingStoreError, match="no staging marker"):
        StagingStore(source)

    remaining = (
        sqlite3.connect(source)
        .execute("SELECT name FROM sqlite_master WHERE type='table'")
        .fetchall()
    )
    assert [r[0] for r in remaining] == ["customers"], "the source was left alone"


def test_a_file_that_is_not_sqlite_at_all_is_refused(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"just some text, definitely not a database" * 10)

    with pytest.raises(NotAStagingStoreError, match="not a SQLite database"):
        StagingStore(path)


def test_a_newer_schema_version_is_refused(tmp_path: Path):
    path = tmp_path / "staging.db"
    with StagingStore(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute("UPDATE staging_meta SET version=?", (SCHEMA_VERSION + 5,))
    conn.commit()
    conn.close()

    with pytest.raises(NotAStagingStoreError, match="newer version"):
        StagingStore(path)


def test_staging_cannot_be_the_source_database(tmp_path: Path):
    path = tmp_path / "data.db"

    with pytest.raises(StagingPathConflictError, match="is the source database"):
        StagingStore(path, source_path=path)


def test_the_overlap_check_resolves_the_paths(tmp_path: Path):
    """A different spelling of the same file still counts."""
    path = tmp_path / "data.db"
    path.touch()

    with pytest.raises(StagingPathConflictError):
        StagingStore(path, source_path=tmp_path / "." / "data.db")


def test_a_different_source_path_is_allowed(tmp_path: Path):
    with StagingStore(tmp_path / "staging.db", source_path=tmp_path / "source.db") as s:
        assert s.summary()["containers"] == 0


def test_no_source_path_means_no_overlap_check(tmp_path: Path):
    with StagingStore(tmp_path / "staging.db", source_path=None) as store:
        assert store.path.exists()
