from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyarrow.fs import LocalFileSystem

from src.adapter.base import UnknownColumnError, UnknownContainerError
from src.adapter.datalake import DatalakeAdapter
from src.core.config import ConnectionInfo
from src.core.tool import ContainerType, ProfileMode, SourceAdaptor

USERS = pa.table(
    {
        "id": [1, 2, 3, 4, None],
        "name": ["ada", "bob", "ada", "cid", "ada"],
    }
)

NDJSON = (
    '{"id": 1, "name": "ada"}\n'
    '{"id": 2, "name": "bob"}\n'
    '{"id": null, "name": "ada"}\n'
)


def _write_parquet(directory: Path, table: pa.Table = USERS) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, directory / "part-0.parquet")


def _write_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    """
    A lake with one flat parquet dataset, one hive-partitioned parquet dataset,
    a csv and a json dataset, plus directories that are not datasets at all.
    """
    _write_parquet(tmp_path / "users")
    _write_parquet(tmp_path / "events" / "dt=2024-01-01")
    _write_parquet(tmp_path / "events" / "dt=2024-01-02")

    _write_text(tmp_path / "cities" / "part-0.csv", "city,pop\ntaipei,2600000\n")
    _write_text(tmp_path / "logs" / "part-0.json", NDJSON)

    (tmp_path / "empty").mkdir()
    _write_text(tmp_path / "notes" / "readme.txt", "not a dataset")
    # a stray top-level file is not a container
    (tmp_path / "loose.parquet").write_bytes(b"")
    return tmp_path


@pytest.fixture
def adapter(lake: Path) -> DatalakeAdapter:
    return DatalakeAdapter(str(lake), LocalFileSystem())


# ---- discovery ----


def test_satisfies_source_adaptor_protocol(adapter: DatalakeAdapter):
    # the annotation is the point: a missing tool becomes a type error
    checked: SourceAdaptor = adapter
    assert isinstance(checked, SourceAdaptor)


def test_list_containers_finds_every_supported_format(adapter: DatalakeAdapter):
    containers = adapter.list_containers()

    assert [c.container_name for c in containers] == [
        "cities",
        "events",
        "logs",
        "users",
    ]
    assert all(c.container_type is ContainerType.TABLE for c in containers)
    assert all(c.database == "datalake" for c in containers)
    assert all(c.schema_name is None for c in containers)


def test_row_count_is_only_reported_when_it_is_free(adapter: DatalakeAdapter):
    counts = {c.container_name: c.estimated_count for c in adapter.list_containers()}

    # parquet keeps the count in its footer
    assert counts["users"] == 5
    assert counts["events"] == 10
    # csv/json would have to be read end to end
    assert counts["cities"] is None
    assert counts["logs"] is None


def test_list_containers_ignores_non_datasets(adapter: DatalakeAdapter):
    names = {c.container_name for c in adapter.list_containers()}

    assert "empty" not in names
    assert "notes" not in names
    assert "loose.parquet" not in names


def test_list_containers_rejects_a_scope_it_cannot_serve(adapter: DatalakeAdapter):
    assert adapter.list_containers(database="datalake")

    with pytest.raises(UnknownContainerError, match="unknown database"):
        adapter.list_containers(database="somewhere_else")
    with pytest.raises(UnknownContainerError, match="no schema layer"):
        adapter.list_containers(schema="public")


def test_hive_partition_keys_are_part_of_the_schema(adapter: DatalakeAdapter):
    columns = {c.name: c for c in adapter.get_schema("events")}

    assert set(columns) == {"id", "name", "dt"}
    assert columns["dt"].ordinal == 3


def test_trailing_slash_in_root_is_normalised(lake: Path):
    adapter = DatalakeAdapter(f"{lake}/", LocalFileSystem())

    assert "events" in {c.container_name for c in adapter.list_containers()}


def test_format_choice_is_stable_when_a_directory_mixes_formats(tmp_path: Path):
    _write_text(tmp_path / "mixed" / "a.csv", "x\n1\n")
    _write_text(tmp_path / "mixed" / "b.json", '{"x": 1}\n')
    adapter = DatalakeAdapter(str(tmp_path), LocalFileSystem())

    fmt = adapter._dataset_format("mixed")
    assert fmt == "csv"
    assert all(adapter._dataset_format("mixed") == fmt for _ in range(3))


def test_deeper_nesting_than_probe_depth_is_not_discovered(tmp_path: Path):
    _write_parquet(tmp_path / "deep" / "a=1" / "b=2" / "c=3" / "d=4")
    adapter = DatalakeAdapter(str(tmp_path), LocalFileSystem())

    assert adapter.list_containers() == []


def test_list_databases(adapter: DatalakeAdapter):
    assert adapter.list_databases() == ["datalake"]

    named = DatalakeAdapter("/x", LocalFileSystem(), database="lake2")
    assert named.list_databases() == ["lake2"]


# ---- schema / sample ----


def test_get_schema(adapter: DatalakeAdapter):
    columns = adapter.get_schema("users")

    assert [c.name for c in columns] == ["id", "name"]
    assert [c.ordinal for c in columns] == [1, 2]
    assert columns[0].native_type == "int64"
    assert columns[0].nullable is True
    # a data lake has no key metadata to report
    assert not any(c.is_pk or c.is_fk for c in columns)


def test_get_sample(adapter: DatalakeAdapter):
    rows = adapter.get_sample("users", limit=2)
    assert rows == [{"id": 1, "name": "ada"}, {"id": 2, "name": "bob"}]


def test_get_sample_is_capped_by_max_sample_limit(lake: Path):
    adapter = DatalakeAdapter(str(lake), LocalFileSystem(), max_sample_limit=2)

    assert len(adapter.get_sample("users", limit=100)) == 2

    # the cap is per adapter and must not leak onto the class
    uncapped = DatalakeAdapter(str(lake), LocalFileSystem())
    assert len(uncapped.get_sample("users", limit=4)) == 4


def test_get_sample_default_limit(adapter: DatalakeAdapter):
    assert len(adapter.get_sample("users")) == DatalakeAdapter._DEFAULT_SAMPLE_LIMIT


def test_unknown_container_is_rejected(adapter: DatalakeAdapter):
    with pytest.raises(UnknownContainerError, match="ghost"):
        adapter.get_schema("ghost")
    with pytest.raises(UnknownContainerError):
        adapter.get_sample("ghost")
    # directories that hold no data file are not containers either
    with pytest.raises(UnknownContainerError):
        adapter.get_sample("empty")


@pytest.mark.parametrize(
    "container", ["../etc", "..", ".", "", "events/dt=2024-01-01", "events\\dt=x"]
)
def test_container_name_cannot_escape_the_lake_root(
    adapter: DatalakeAdapter, container: str
):
    with pytest.raises(UnknownContainerError):
        adapter.get_sample(container)


def test_unknown_column_is_rejected(adapter: DatalakeAdapter):
    with pytest.raises(UnknownColumnError, match=r"users\.age"):
        adapter.profile_column("users", "age", ProfileMode.DISTINCT_COUNT)


# ---- json datasets ----


def test_json_dataset_schema_and_sample(adapter: DatalakeAdapter):
    assert [c.name for c in adapter.get_schema("logs")] == ["id", "name"]
    assert adapter.get_sample("logs", limit=2) == [
        {"id": 1, "name": "ada"},
        {"id": 2, "name": "bob"},
    ]


@pytest.mark.parametrize("suffix", [".json", ".jsonl", ".ndjson"])
def test_json_extensions(tmp_path: Path, suffix: str):
    _write_text(tmp_path / "logs" / f"part-0{suffix}", NDJSON)
    adapter = DatalakeAdapter(str(tmp_path), LocalFileSystem())

    assert [c.container_name for c in adapter.list_containers()] == ["logs"]
    distinct = adapter.profile_column("logs", "name", ProfileMode.DISTINCT_COUNT)
    assert distinct.distinct_count == 2


def test_json_dataset_can_be_profiled(adapter: DatalakeAdapter):
    assert adapter.profile_column("logs", "id", ProfileMode.MIN_MAX).max_value == "2"

    null_ratio = adapter.profile_column("logs", "id", ProfileMode.NULL_RATIO)
    assert null_ratio.null_ratio == 1 / 3


def test_a_json_array_file_is_not_supported(tmp_path: Path):
    """pyarrow reads line-delimited json only; a top-level array cannot be read."""
    _write_text(tmp_path / "arrayish" / "part-0.json", '[{"id": 1}, {"id": 2}]')
    adapter = DatalakeAdapter(str(tmp_path), LocalFileSystem())

    with pytest.raises(pa.ArrowInvalid):
        adapter.get_schema("arrayish")


# ---- profiling ----


def test_profile_distinct_count(adapter: DatalakeAdapter):
    result = adapter.profile_column("users", "id", ProfileMode.DISTINCT_COUNT)

    # matches COUNT(DISTINCT x): nulls do not count
    assert result.distinct_count == 4
    assert result.approximate is False


def test_profile_null_ratio(adapter: DatalakeAdapter):
    with_nulls = adapter.profile_column("users", "id", ProfileMode.NULL_RATIO)
    without_nulls = adapter.profile_column("users", "name", ProfileMode.NULL_RATIO)

    assert with_nulls.null_ratio == 0.2
    assert without_nulls.null_ratio == 0.0


def test_profile_top_values(adapter: DatalakeAdapter):
    result = adapter.profile_column("users", "name", ProfileMode.TOP_VALUES)

    assert result.top_values is not None
    top = [(v.value, v.count) for v in result.top_values]
    # count desc, then value for a stable tie-break
    assert top == [("ada", 3), ("bob", 1), ("cid", 1)]


def test_profile_top_values_renders_null_as_empty_string(adapter: DatalakeAdapter):
    result = adapter.profile_column("users", "id", ProfileMode.TOP_VALUES)

    assert result.top_values is not None
    assert ("", 1) in [(v.value, v.count) for v in result.top_values]


def test_profile_top_values_respects_default_top_n(tmp_path: Path):
    _write_parquet(tmp_path / "wide", pa.table({"v": [str(i) for i in range(50)]}))
    adapter = DatalakeAdapter(str(tmp_path), LocalFileSystem())

    result = adapter.profile_column("wide", "v", ProfileMode.TOP_VALUES)

    assert result.top_values is not None
    assert len(result.top_values) == DatalakeAdapter._DEFAULT_TOP_N


def test_profile_min_max(adapter: DatalakeAdapter):
    result = adapter.profile_column("users", "id", ProfileMode.MIN_MAX)
    assert (result.min_value, result.max_value) == ("1", "4")


def test_profile_spans_every_partition(adapter: DatalakeAdapter):
    """Aggregates must fold across batches/files, not report the first one."""
    assert adapter.profile_column("events", "id", ProfileMode.MIN_MAX).max_value == "4"

    null_ratio = adapter.profile_column("events", "id", ProfileMode.NULL_RATIO)
    assert null_ratio.null_ratio == 0.2
    top = adapter.profile_column("events", "name", ProfileMode.TOP_VALUES).top_values
    assert top is not None
    # "ada" appears 3x in each of the two partitions
    assert (top[0].value, top[0].count) == ("ada", 6)


def test_profile_streams_in_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A tiny batch size must not change any answer."""
    _write_parquet(tmp_path / "users")
    adapter = DatalakeAdapter(str(tmp_path), LocalFileSystem())
    monkeypatch.setattr(DatalakeAdapter, "_PROFILE_BATCH_ROWS", 2)

    assert adapter.profile_column("users", "id", ProfileMode.MIN_MAX).min_value == "1"

    null_ratio = adapter.profile_column("users", "id", ProfileMode.NULL_RATIO)
    distinct = adapter.profile_column("users", "id", ProfileMode.DISTINCT_COUNT)
    assert null_ratio.null_ratio == 0.2
    assert distinct.distinct_count == 4


def test_hash_based_profiling_stops_at_the_row_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_parquet(tmp_path / "users")
    adapter = DatalakeAdapter(str(tmp_path), LocalFileSystem())
    monkeypatch.setattr(DatalakeAdapter, "_PROFILE_BATCH_ROWS", 2)
    monkeypatch.setattr(DatalakeAdapter, "_MAX_PROFILE_ROWS", 2)

    truncated = adapter.profile_column("users", "id", ProfileMode.DISTINCT_COUNT)
    assert truncated.approximate is True
    assert truncated.distinct_count == 2  # only the first batch was read

    # the streaming modes are exact
    exact = adapter.profile_column("users", "id", ProfileMode.NULL_RATIO)
    assert exact.approximate is False
    assert exact.null_ratio == 0.2


def test_profile_empty_dataset(tmp_path: Path):
    _write_parquet(tmp_path / "blank", USERS.slice(0, 0))
    adapter = DatalakeAdapter(str(tmp_path), LocalFileSystem())

    null_ratio = adapter.profile_column("blank", "id", ProfileMode.NULL_RATIO)
    min_max = adapter.profile_column("blank", "id", ProfileMode.MIN_MAX)
    distinct = adapter.profile_column("blank", "id", ProfileMode.DISTINCT_COUNT)

    assert null_ratio.null_ratio is None
    assert (min_max.min_value, min_max.max_value) == (None, None)
    assert distinct.distinct_count == 0


def test_profile_unsupported_mode(adapter: DatalakeAdapter):
    with pytest.raises(ValueError, match="unsupported profile mode"):
        adapter.profile_column("users", "id", "median")  # type: ignore[arg-type]


# ---- statement log ----


def test_scans_are_recorded_for_audit(adapter: DatalakeAdapter):
    assert adapter.pop_rendered_sql() is None

    adapter.get_sample("users", 1)
    rendered = adapter.pop_rendered_sql()

    assert rendered is not None
    assert "scan" in rendered and "users (parquet)" in rendered
    # popping clears the log
    assert adapter.pop_rendered_sql() is None


def test_resolving_one_container_does_not_scan_the_whole_lake(adapter: DatalakeAdapter):
    adapter.get_schema("users")

    rendered = adapter.pop_rendered_sql()
    assert rendered is not None
    assert rendered.count("scan") == 1
    assert "events" not in rendered


# ---- from_connection ----


def test_from_connection_with_path(lake: Path):
    adapter = DatalakeAdapter.from_connection(ConnectionInfo(path=str(lake)))

    assert adapter.list_databases() == ["datalake"]
    assert "users" in {c.container_name for c in adapter.list_containers()}


def test_from_connection_with_uri(lake: Path):
    adapter = DatalakeAdapter.from_connection(
        ConnectionInfo(uri=lake.as_uri()), database="lake2", max_sample_limit=1
    )

    assert adapter.list_databases() == ["lake2"]
    assert len(adapter.get_sample("users", 100)) == 1


def test_from_connection_takes_the_database_name_from_the_connection(lake: Path):
    adapter = DatalakeAdapter.from_connection(
        ConnectionInfo(path=str(lake), database="warehouse")
    )

    assert adapter.list_databases() == ["warehouse"]
    # an explicit argument still wins
    override = DatalakeAdapter.from_connection(
        ConnectionInfo(path=str(lake), database="warehouse"), database="lake2"
    )
    assert override.list_databases() == ["lake2"]


def test_from_connection_requires_uri_or_path():
    with pytest.raises(ValueError, match="requires a <REF>_URI"):
        DatalakeAdapter.from_connection(ConnectionInfo(host="localhost"))


# ---- value serialisation ----


def test_get_sample_jsonifies_non_native_types(tmp_path: Path):
    table = pa.table(
        {
            "when": pa.array([datetime(2024, 1, 2, 3, 4, 5)], type=pa.timestamp("us")),
            "amount": pa.array([Decimal("1.20")], type=pa.decimal128(5, 2)),
            "blob": pa.array([b"raw"], type=pa.binary()),
        }
    )
    _write_parquet(tmp_path / "orders", table)
    adapter = DatalakeAdapter(str(tmp_path), LocalFileSystem())

    assert adapter.get_sample("orders") == [
        {"when": "2024-01-02T03:04:05", "amount": "1.20", "blob": "cmF3"}
    ]
