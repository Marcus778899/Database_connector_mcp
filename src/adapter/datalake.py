from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any, ClassVar

import pyarrow.compute as _pc
import pyarrow.dataset as pads
from pyarrow.fs import FileSelector, FileSystem, FileType

from src.adapter.base import (
    AdapterBase,
    UnknownColumnError,
    UnknownContainerError,
)
from src.core.config import ConnectionInfo
from src.core.tool import (
    ColumnInfo,
    ContainerInfo,
    ContainerType,
    ProfileMode,
    ProfileResult,
    TopValue,
)
from src.utils.serialize import jsonify

# json here means line-delimited json; pyarrow cannot read a top-level array.
_FORMATS = {
    ".parquet": "parquet",
    ".csv": "csv",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
}

# Only parquet keeps its row count in metadata; csv/json have to be read.
_ROW_COUNT_FROM_METADATA = frozenset({"parquet"})

# pyarrow.compute builds its kernels into module globals at import time and ships
# no stubs, so count_distinct / value_counts / min_max are invisible to static
# analysers. One untyped handle beats a `type: ignore` per call site.
pc: Any = _pc


class DatalakeAdapter(AdapterBase):
    """SourceAdapter over a data lake（parquet/csv/json dataset）。"""

    # deepest partition nesting we probe for a data file
    _MAX_PROBE_DEPTH: ClassVar[int] = 3
    _PROFILE_BATCH_ROWS: ClassVar[int] = 65_536
    # distinct_count/top_values hold one entry per distinct value, so they stop
    # here and report the result as approximate
    _MAX_PROFILE_ROWS: ClassVar[int] = 5_000_000

    def __init__(
        self,
        root: str,
        filesystem: FileSystem,
        *,
        database: str = "datalake",
        max_sample_limit: int | None = None,
    ) -> None:
        super().__init__(database=database, max_sample_limit=max_sample_limit)
        self._root = root.rstrip("/")
        self._fs = filesystem

    @classmethod
    def from_connection(
        cls,
        conn_info: ConnectionInfo,
        *,
        database: str | None = None,
        max_sample_limit: int | None = None,
    ) -> "DatalakeAdapter":
        uri = conn_info.uri or conn_info.path
        if not uri:
            raise ValueError(
                "The datalake connection requires a <REF>_URI (s3://…/gs://…) or <REF>_PATH."
            )
        filesystem, root = FileSystem.from_uri(uri)
        return cls(
            root,
            filesystem,
            database=database or conn_info.database or "datalake",
            max_sample_limit=max_sample_limit,
        )

    # ---- catalog ----

    def _detect_format(self, dir_path: str, depth: int = 0) -> str | None:
        """
        Sniff a dataset's format from the first recognised data file, descending
        into subdirectories so hive-partitioned datasets are visible too.
        Entries are sorted to keep a mixed-format directory resolving the same
        way on every call.
        """
        subdirs: list[str] = []
        entries = sorted(
            self._fs.get_file_info(FileSelector(dir_path, recursive=False)),
            key=lambda info: info.base_name,
        )
        for info in entries:
            if info.type == FileType.Directory:
                subdirs.append(info.path)
                continue
            for ext, fmt in _FORMATS.items():
                if info.base_name.endswith(ext):
                    return fmt

        if depth >= self._MAX_PROBE_DEPTH:
            return None
        for subdir in subdirs:
            fmt = self._detect_format(subdir, depth + 1)
            if fmt:
                return fmt
        return None

    def _datasets(self) -> dict[str, str]:
        """Every dataset directly under the lake root, as name -> format."""
        out: dict[str, str] = {}
        for info in self._fs.get_file_info(FileSelector(self._root, recursive=False)):
            if info.type == FileType.Directory:
                fmt = self._detect_format(info.path)
                if fmt:
                    out[info.base_name] = fmt
        return out

    def _dataset_format(self, container: str) -> str | None:
        """
        Resolve one container without format-probing the whole lake.

        Doubles as the allowlist: only a direct child directory of the root
        holding a recognised data file can be named, so `..` cannot walk out of
        the lake and `a/b` cannot reach into a partition.
        """
        if container in ("", ".", "..") or "/" in container or "\\" in container:
            return None
        path = f"{self._root}/{container}"
        if self._fs.get_file_info(path).type != FileType.Directory:
            return None
        return self._detect_format(path)

    def _open(self, name: str, fmt: str) -> pads.Dataset:
        self._record_sql(f"scan {self._root}/{name} ({fmt})")
        # without partitioning="hive" a dt=2024-01-01 directory is read as data
        # but dt never appears in the schema; a no-op on flat layouts
        return pads.dataset(
            f"{self._root}/{name}",
            format=fmt,
            filesystem=self._fs,
            partitioning="hive",
        )

    def _require_dataset(self, container: str) -> pads.Dataset:
        fmt = self._dataset_format(container)
        if fmt is None:
            raise UnknownContainerError(container)
        return self._open(container, fmt)

    # 4 tools (READ ONLY)

    def list_containers(
        self, database: str | None = None, schema: str | None = None
    ) -> list[ContainerInfo]:
        if database is not None and database != self._database:
            raise UnknownContainerError(f"unknown database：{database!r}")
        if schema is not None:
            raise UnknownContainerError(
                f"the datalake has no schema layer, got schema={schema!r}"
            )

        result: list[ContainerInfo] = []
        for name, fmt in sorted(self._datasets().items()):
            dataset = self._open(name, fmt)
            result.append(
                ContainerInfo(
                    database=self._database,
                    schema_name=None,
                    container_name=name,
                    container_type=ContainerType.TABLE,
                    # counting csv/json means reading every row just to list
                    estimated_count=(
                        dataset.count_rows()
                        if fmt in _ROW_COUNT_FROM_METADATA
                        else None
                    ),
                )
            )
        return result

    def get_schema(self, container: str) -> list[ColumnInfo]:
        dataset = self._require_dataset(container)
        return [
            ColumnInfo(
                name=field.name,
                ordinal=ordinal,
                native_type=str(field.type),
                nullable=field.nullable,
                is_pk=False,
                is_fk=False,
            )
            for ordinal, field in enumerate(dataset.schema, start=1)
        ]

    def get_sample(
        self, container: str, limit: int = AdapterBase._DEFAULT_SAMPLE_LIMIT
    ) -> list[dict[str, Any]]:
        dataset = self._require_dataset(container)
        table = dataset.head(self._cap_limit(limit))
        return [{k: jsonify(v) for k, v in row.items()} for row in table.to_pylist()]

    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult:
        dataset = self._require_dataset(container)
        if column not in dataset.schema.names:
            raise UnknownColumnError(f"{container}.{column}")

        if mode == ProfileMode.NULL_RATIO:
            return self._profile_null_ratio(dataset, column)
        if mode == ProfileMode.MIN_MAX:
            return self._profile_min_max(dataset, column)
        if mode in (ProfileMode.DISTINCT_COUNT, ProfileMode.TOP_VALUES):
            return self._profile_counts(dataset, column, mode)

        raise ValueError(f"unsupported profile mode：{mode!r}")

    # ---- profiling ----
    # Batched throughout: to_table(columns=[column]) would pull a whole
    # lake-sized column into memory.

    def _column_batches(self, dataset: pads.Dataset, column: str) -> Iterator[Any]:
        scanner = dataset.scanner(columns=[column], batch_size=self._PROFILE_BATCH_ROWS)
        for batch in scanner.to_batches():
            yield batch.column(0)

    def _profile_null_ratio(self, dataset: pads.Dataset, column: str) -> ProfileResult:
        total = nulls = 0
        for arr in self._column_batches(dataset, column):
            total += len(arr)
            nulls += arr.null_count
        return ProfileResult(null_ratio=(nulls / total) if total else None)

    def _profile_min_max(self, dataset: pads.Dataset, column: str) -> ProfileResult:
        lo = hi = None
        for arr in self._column_batches(dataset, column):
            batch = pc.min_max(arr).as_py()
            batch_lo, batch_hi = batch.get("min"), batch.get("max")
            if batch_lo is not None and (lo is None or batch_lo < lo):
                lo = batch_lo
            if batch_hi is not None and (hi is None or batch_hi > hi):
                hi = batch_hi
        return ProfileResult(
            min_value=None if lo is None else str(lo),
            max_value=None if hi is None else str(hi),
        )

    def _profile_counts(
        self, dataset: pads.Dataset, column: str, mode: ProfileMode
    ) -> ProfileResult:
        """One pass for both modes: each needs a count per distinct value."""
        counts: Counter[Any] = Counter()
        scanned = 0
        approximate = False
        for arr in self._column_batches(dataset, column):
            value_counts = pc.value_counts(arr)
            values = value_counts.field("values").to_pylist()
            freqs = value_counts.field("counts").to_pylist()
            for value, freq in zip(values, freqs):
                counts[value] += freq
            scanned += len(arr)
            if scanned >= self._MAX_PROFILE_ROWS:
                approximate = True
                break

        if mode == ProfileMode.DISTINCT_COUNT:
            # value_counts keeps null; COUNT(DISTINCT x) does not
            distinct = len(counts) - (1 if None in counts else 0)
            return ProfileResult(distinct_count=distinct, approximate=approximate)

        top = sorted(counts.items(), key=lambda vc: (-vc[1], str(vc[0])))[
            : self._DEFAULT_TOP_N
        ]
        return ProfileResult(
            top_values=[
                TopValue(value="" if value is None else str(value), count=freq)
                for value, freq in top
            ],
            approximate=approximate,
        )
