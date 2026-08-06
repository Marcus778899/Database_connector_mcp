from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

from src.adapter.base import (
    AdapterBase,
    SqlAdapterBase,
    UnknownContainerError,
)
from src.core.config import ConnectionInfo
from src.core.contracts import (
    ColumnInfo,
    ContainerInfo,
    ContainerPage,
    ContainerType,
    ProfileMode,
    ProfileResult,
    TopValue,
)
from src.core.log import log
from src.utils.serialize import jsonify

_CONTAINER_TYPES = {"table": ContainerType.TABLE, "view": ContainerType.VIEW}

# sqlite's own bookkeeping, never part of a user's catalog. The backslash escape
# matters: unescaped, `_` is a single-character wildcard.
_CATALOG_WHERE = "type IN ('table','view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"


def _render(sql: str, params: Sequence[Any] = ()) -> str:
    """The statement with its parameters inlined, for the audit trail. Our
    templates never contain a literal `?`, so positional replacement is safe."""
    rendered = sql
    for value in params:
        rendered = rendered.replace("?", repr(value), 1)
    return rendered


class SqliteAdapter(SqlAdapterBase):
    """
    SourceAdapter over one sqlite file.

    Every statistic is a full scan of a local file, so no result is ever
    approximate — which also makes this the cheapest engine to test against.
    """

    # one file is one database; `database` only labels it
    SUPPORTS_MULTIPLE_DATABASES: ClassVar[bool] = False
    DEFAULT_DATABASE: ClassVar[str] = "main"

    def __init__(
        self,
        path: str | Path,
        *,
        database: str = DEFAULT_DATABASE,
        max_sample_limit: int | None = None,
        read_only: bool = True,
    ) -> None:
        super().__init__(database=database, max_sample_limit=max_sample_limit)
        self._path = Path(path)
        self._read_only = read_only
        # One connection guarded by a lock: FastMCP runs sync tools in a
        # threadpool and the scan worker has a thread of its own.
        self._lock = threading.Lock()
        self._conn = self._connect()
        log.info(
            f"opened sqlite {self._path}"
            f"{' read-only' if read_only else ' READ-WRITE'}"
        )

    def _connect(self) -> sqlite3.Connection:
        """
        Read-only by default, and enforced by the driver rather than by
        convention: these tools never write, so a write should fail loudly if one
        ever slips in.
        """
        if not self._read_only:
            conn = sqlite3.connect(self._path, check_same_thread=False)
        else:
            if not self._path.is_file():
                raise ValueError(f"no sqlite database at {self._path}")
            # safe='/:' keeps a Windows drive letter readable; '?' and '%' still
            # get escaped, so they cannot start a URI query or an escape.
            target = f"file:{quote(self._path.as_posix(), safe='/:')}?mode=ro"
            conn = sqlite3.connect(target, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @classmethod
    def from_connection(
        cls,
        conn_info: ConnectionInfo,
        *,
        database: str | None = None,
        max_sample_limit: int | None = None,
    ) -> SqliteAdapter:
        target = conn_info.path or conn_info.uri
        if not target:
            raise ValueError(
                "The sqlite connection requires a <REF>_PATH (a .db file) or a "
                "<REF>_URI."
            )
        return cls(
            target,
            database=database or conn_info.database or cls.DEFAULT_DATABASE,
            max_sample_limit=max_sample_limit,
        )

    # ---- lifecycle ----

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        """Deliberately not through `_rows`: pool bookkeeping is not a statement
        the audit trail should attribute to a tool call."""
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    # ---- querying ----

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        self._record_sql(_render(sql, params))
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # 4 tools (READ ONLY)

    def list_containers(
        self,
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage:
        """
        One page of tables and views, ordered by name.

        Keyset, not offset: the cursor is the last name returned, so a table
        appearing or disappearing mid-walk cannot make the caller skip or repeat
        one.
        """
        if database is not None and database != self._database:
            raise UnknownContainerError(f"unknown database: {database!r}")
        if schema is not None:
            raise UnknownContainerError(
                f"sqlite has no schema layer, got schema={schema!r}"
            )

        page_size = self._cap_page_size(limit)
        sql = f"SELECT name, type FROM sqlite_master WHERE {_CATALOG_WHERE} "  # noqa: S608
        params: list[Any] = []
        if cursor is not None:
            sql += "AND name > ? "
            params.append(cursor)
        sql += "ORDER BY name LIMIT ?"
        params.append(page_size + 1)  # one extra row tells us whether more remain

        rows = self._rows(sql, tuple(params))
        page, has_more = rows[:page_size], len(rows) > page_size
        containers = [
            ContainerInfo(
                database=self._database,
                schema_name=None,
                container_name=row["name"],
                container_type=_CONTAINER_TYPES[row["type"]],
                # counting a view would run it; only tables are counted
                estimated_count=(
                    self._count(row["name"]) if row["type"] == "table" else None
                ),
            )
            for row in page
        ]
        return ContainerPage(
            containers=containers,
            next_cursor=page[-1]["name"] if has_more else None,
        )

    def get_schema(self, container: str) -> list[ColumnInfo]:
        quoted = self._require_container(container)
        foreign_keys = self._foreign_keys(quoted)
        rows = self._rows(f"PRAGMA table_info({quoted})")
        if not rows:
            # a view whose source table is gone still sits in sqlite_master
            raise UnknownContainerError(container)
        return [
            ColumnInfo(
                name=row["name"],
                # PRAGMA counts columns from zero, the contract from one
                ordinal=row["cid"] + 1,
                # sqlite allows a column with no declared type; report that
                # honestly rather than inventing an affinity for it
                native_type=row["type"] or "",
                nullable=not row["notnull"],
                is_pk=bool(row["pk"]),
                is_fk=row["name"] in foreign_keys,
            )
            for row in rows
        ]

    def get_sample(
        self, container: str, limit: int = AdapterBase._DEFAULT_SAMPLE_LIMIT
    ) -> list[dict[str, Any]]:
        quoted = self._require_container(container)
        sql, params = self._sql_sample(quoted, self._cap_limit(limit))
        return [
            {key: jsonify(row[key]) for key in row.keys()}
            for row in self._rows(sql, params)
        ]

    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult:
        quoted = self._require_container(container)
        quoted_column = self._require_column(container, column)

        if mode == ProfileMode.DISTINCT_COUNT:
            sql, params = self._sql_distinct_count(quoted, quoted_column)
            return ProfileResult(distinct_count=int(self._rows(sql, params)[0]["n"]))

        if mode == ProfileMode.NULL_RATIO:
            sql, params = self._sql_null_ratio(quoted, quoted_column)
            ratio = self._rows(sql, params)[0]["r"]
            # AVG over no rows is NULL: an empty table has no ratio to report
            return ProfileResult(null_ratio=None if ratio is None else float(ratio))

        if mode == ProfileMode.MIN_MAX:
            sql, params = self._sql_min_max(quoted, quoted_column)
            row = self._rows(sql, params)[0]
            return ProfileResult(
                min_value=None if row["lo"] is None else str(row["lo"]),
                max_value=None if row["hi"] is None else str(row["hi"]),
            )

        if mode == ProfileMode.TOP_VALUES:
            sql, params = self._sql_top_values(
                quoted, quoted_column, self._DEFAULT_TOP_N
            )
            return ProfileResult(
                top_values=[
                    TopValue(
                        value="" if row["v"] is None else str(row["v"]),
                        count=int(row["c"]),
                    )
                    for row in self._rows(sql, params)
                ]
            )

        raise ValueError(f"unsupported profile mode: {mode!r}")

    # ---- catalog helpers ----

    def _walk_containers(self) -> frozenset[str]:
        """Names in one statement. The inherited walk would page through
        `list_containers`, counting the rows of every table on the way."""
        sql = f"SELECT name FROM sqlite_master WHERE {_CATALOG_WHERE}"  # noqa: S608
        return frozenset(row["name"] for row in self._rows(sql))

    def _count(self, container: str) -> int | None:
        """
        A real COUNT(*): sqlite keeps no row count in metadata.

        Only for the page being listed. An unreadable table — or one whose name
        the identifier policy rejects — downgrades to an unknown count instead of
        failing the whole listing.
        """
        try:
            quoted = self._quote(container)
        except ValueError:
            log.warning(f"cannot count {container!r}: name is not a plain identifier")
            return None
        try:
            rows = self._rows(f"SELECT COUNT(*) AS n FROM {quoted}")  # noqa: S608
        except sqlite3.Error as exc:
            log.warning(f"cannot count {container!r}: {exc}")
            return None
        return int(rows[0]["n"])

    def _foreign_keys(self, quoted: str) -> dict[str, tuple[str, str | None]]:
        """Source column -> (target container, target column). Only the flag
        reaches `ColumnInfo` today; the target lands there with the relationship
        work."""
        rows = self._rows(f"PRAGMA foreign_key_list({quoted})")
        return {row["from"]: (row["table"], row["to"]) for row in rows}
