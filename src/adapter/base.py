from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from src.core.tool import ColumnInfo, ContainerInfo, ProfileMode, ProfileResult


class UnknownContainerError(Exception):
    """The container is not in the catalog (rejected by the allowlist)."""


class UnknownColumnError(Exception):
    """The column is not in the container's schema."""


class AdapterBase(ABC):
    """
    Engine-agnostic base for every `SourceAdaptor`: the five-tool contract, the
    sampling/profiling policy and the executed-statement log. SQL-only machinery
    lives in `SqlAdapterBase`.
    """

    _DEFAULT_SAMPLE_LIMIT: ClassVar[int] = 3
    _MAX_SAMPLE_LIMIT: ClassVar[int] = 100
    _DEFAULT_TOP_N: ClassVar[int] = 20

    def __init__(
        self,
        *,
        database: str = "",
        max_sample_limit: int | None = None,
    ) -> None:
        self._database = database
        # per server instance (ServerConfig.max_sample_limit), so it must not be
        # written back onto the class
        self._max_sample_limit = (
            self._MAX_SAMPLE_LIMIT if max_sample_limit is None else max_sample_limit
        )
        self._statement_log: list[str] = []

    # ---- contract ----
    # Abstract, so a backend missing a tool fails on construction instead of
    # silently dropping out of the SourceAdaptor protocol.

    def list_databases(self) -> list[str]:
        return [self._database]

    @abstractmethod
    def list_containers(
        self, database: str | None = None, schema: str | None = None
    ) -> list[ContainerInfo]:
        raise NotImplementedError

    @abstractmethod
    def get_schema(self, container: str) -> list[ColumnInfo]:
        raise NotImplementedError

    @abstractmethod
    def get_sample(
        self, container: str, limit: int = _DEFAULT_SAMPLE_LIMIT
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult:
        raise NotImplementedError

    # ---- policy ----

    def _known_containers(self) -> set[str]:
        return {c.container_name for c in self.list_containers()}

    def _cap_limit(self, limit: int) -> int:
        return max(0, min(limit, self._max_sample_limit))

    # ---- rendered_sql ----

    def _record_sql(self, sql: str) -> None:
        """Record what was executed, for the audit layer. Non-SQL backends
        record their equivalent operation."""
        self._statement_log.append(sql)

    def pop_rendered_sql(self) -> str | None:
        """Take and clear the statements recorded since the last call."""
        if not self._statement_log:
            return None
        rendered = "; ".join(self._statement_log)
        self._statement_log = []
        return rendered


class SqlAdapterBase(AdapterBase):
    """Identifier quoting, parameter style and statement templates for SQL backends."""

    _IDENT_RE: ClassVar[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]+$")
    _QUOTE_OPEN: ClassVar[str] = '"'
    _QUOTE_CLOSE: ClassVar[str] = '"'
    _PARAM: ClassVar[str] = "?"

    # ---- identifier ----

    def _quote(self, ident: str) -> str:
        if not self._IDENT_RE.match(ident):
            raise ValueError(f"illegal identifier：{ident!r}")
        return f"{self._QUOTE_OPEN}{ident}{self._QUOTE_CLOSE}"

    def _require_container(self, container: str) -> str:
        if container not in self._known_containers():
            raise UnknownContainerError(container)
        return self._quote(container)

    def _require_column(self, container: str, column: str) -> str:
        cols = {c.name for c in self.get_schema(container)}
        if column not in cols:
            raise UnknownColumnError(f"{container}.{column}")
        return self._quote(column)

    # ---- fixed templates, returning (sql, params) ----
    # Identifiers arrive already quoted via _require_*; nothing here
    # concatenates an unchecked string.

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
