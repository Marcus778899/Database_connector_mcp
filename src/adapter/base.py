from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from src.core.contracts import ColumnInfo, ContainerPage, ProfileMode, ProfileResult


class UnknownContainerError(Exception):
    """The container is not in the catalog (rejected by the allowlist)."""


class UnknownColumnError(Exception):
    """The column is not in the container's schema."""


class AdapterBase(ABC):
    """Engine-agnostic base: the tool contract, sampling policy and statement log."""

    _DEFAULT_SAMPLE_LIMIT: ClassVar[int] = 3
    _MAX_SAMPLE_LIMIT: ClassVar[int] = 100
    _DEFAULT_TOP_N: ClassVar[int] = 20
    _DEFAULT_PAGE_SIZE: ClassVar[int] = 100
    _MAX_PAGE_SIZE: ClassVar[int] = 1000

    # False when `database` names the whole source instead of selecting one inside it.
    SUPPORTS_MULTIPLE_DATABASES: ClassVar[bool] = True

    def __init__(
        self,
        *,
        database: str = "",
        max_sample_limit: int | None = None,
    ) -> None:
        self._database = database
        # per instance, so it must not be written back onto the class
        self._max_sample_limit = (
            self._MAX_SAMPLE_LIMIT if max_sample_limit is None else max_sample_limit
        )
        self._statement_log: list[str] = []

    # ---- contract: abstract so a missing tool fails on construction ----

    def list_databases(self) -> list[str]:
        return [self._database]

    @abstractmethod
    def list_containers(
        self,
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage:
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

    def close(self) -> None:
        """No-op unless a subclass holds a connection."""

    def ping(self) -> bool:
        """Still usable? A SQL adapter should round-trip to its server."""
        return True

    # ---- policy ----

    def _known_containers(self) -> set[str]:
        """Every container name. Stopping at page one would reject the rest as
        unknown, so the full walk is deliberate."""
        names: set[str] = set()
        cursor: str | None = None
        while True:
            page = self.list_containers(limit=self._MAX_PAGE_SIZE, cursor=cursor)
            names.update(c.container_name for c in page.containers)
            if page.next_cursor is None or page.next_cursor == cursor:
                return names
            cursor = page.next_cursor

    def _cap_limit(self, limit: int) -> int:
        return max(0, min(limit, self._max_sample_limit))

    def _cap_page_size(self, limit: int | None) -> int:
        """At least one, so a paging caller always makes progress."""
        if limit is None:
            return self._DEFAULT_PAGE_SIZE
        return max(1, min(limit, self._MAX_PAGE_SIZE))

    # ---- rendered_sql ----

    def _record_sql(self, sql: str) -> None:
        """What was executed, for the audit layer."""
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

    # ---- templates -> (sql, params). Identifiers arrive quoted via _require_* ----

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
