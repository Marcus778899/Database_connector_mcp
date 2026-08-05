from __future__ import annotations

import re
from typing import ClassVar

from src.core.tool import ColumnInfo, ContainerInfo


class UnknownContainerError(Exception):
    """The container is not in the catalog (rejected by the allowlist)."""


class UnknownColumnError(Exception):
    """The column is not in the container's schema."""


class SqlAdapterBase:
    _IDENT_RE: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]+$")
    _QUOTE_OPEN: ClassVar[str] = '"'
    _QUOTE_CLOSE: ClassVar[str] = '"'
    _PARAM: ClassVar[str] = "?"
    _DEFAULT_SAMPLE_LIMIT: ClassVar[int] = 3
    _MAX_SAMPLE_LIMIT: ClassVar[int] = 100
    _DEFAULT_TOP_N: ClassVar[int] = 20

    def list_databases(self) -> list[str]:
        return [getattr(self, "_database", "")]

    # Subclasses must provide this (this is merely a type hint; the implementation resides in the subclass).
    def list_containers(
        self, database: str | None = None, schema: str | None = None
    ) -> list[ContainerInfo]:
        raise NotImplementedError

    def get_schema(self, container: str) -> list[ColumnInfo]:
        raise NotImplementedError

    # ---- identifier ----
    def _quote(self, ident: str) -> str:
        if not self._IDENT_RE.match(ident):
            raise ValueError(f"illegal identifier：{ident!r}")
        return f"{self._QUOTE_OPEN}{ident}{self._QUOTE_CLOSE}"

    def _known_containers(self) -> set[str]:
        return {c.container_name for c in self.list_containers()}

    def _require_container(self, container: str) -> str:
        if container not in self._known_containers():
            raise UnknownContainerError(container)
        return self._quote(container)

    def _require_column(self, container: str, column: str) -> str:
        cols = {c.name for c in self.get_schema(container)}
        if column not in cols:
            raise UnknownColumnError(f"{container}.{column}")
        return self._quote(column)

    def _cap_limit(self, limit: int) -> int:
        return max(0, min(limit, self._MAX_SAMPLE_LIMIT))

    # ---- Fixed SQL Templates (returning (sql, params)) ----
    # Quoted table/column names are obtained by the caller via _require_*;
    # the templates no longer concatenate unchecked strings.

    def _sql_sample(self, quoted_table: str, limit: int) -> tuple[str, tuple[int, ...]]:
        return (f"SELECT * FROM {quoted_table} LIMIT {self._PARAM}", (limit,))

    def _sql_distinct_count(
        self, quoted_table: str, quoted_col: str
    ) -> tuple[str, tuple[()]]:
        return (
            f"SELECT COUNT(DISTINCT {quoted_col}) AS n FROM {quoted_table}",
            (),
        )

    def _sql_null_ratio(
        self, quoted_table: str, quoted_col: str
    ) -> tuple[str, tuple[()]]:
        return (
            f"SELECT AVG(CASE WHEN {quoted_col} IS NULL THEN 1.0 ELSE 0.0 END) AS r "
            f"FROM {quoted_table}",
            (),
        )

    def _sql_top_values(
        self, quoted_table: str, quoted_col: str, top_n: int
    ) -> tuple[str, tuple[int, ...]]:
        return (
            f"SELECT {quoted_col} AS v, COUNT(*) AS c FROM {quoted_table} "
            f"GROUP BY {quoted_col} ORDER BY c DESC, v LIMIT {self._PARAM}",
            (top_n,),
        )

    def _sql_min_max(self, quoted_table: str, quoted_col: str) -> tuple[str, tuple[()]]:
        return (
            f"SELECT MIN({quoted_col}) AS lo, MAX({quoted_col}) AS hi "
            f"FROM {quoted_table}",
            (),
        )

    # ---- rendered_sql ----

    def _record_sql(self, sql: str) -> None:
        """
        Record the actual SQL executed so that the higher-level audit component can access it.
        """
        self.__dict__.setdefault("_sql_log", []).append(sql)

    def pop_rendered_sql(self) -> str | None:
        """
        Retrieve and clear the SQL statements recorded 
        since the last time (multiple statements concatenated with `; `).
        """
        log = self.__dict__.get("_sql_log")
        if not log:
            return None
        self.__dict__["_sql_log"] = []
        return "; ".join(log)
