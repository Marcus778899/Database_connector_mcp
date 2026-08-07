from __future__ import annotations

import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar

from src.core.contracts import ColumnInfo, ContainerPage, ProfileMode, ProfileResult
from src.core.log import log


class UnknownContainerError(Exception):
    """The container is not in the catalog (rejected by the allowlist)."""


class UnknownColumnError(Exception):
    """The column is not in the container's schema."""


def render_sql(sql: str, params: Sequence[Any] = (), placeholder: str = "?") -> str:
    """
    The statement with its parameters inlined, for the audit trail.

    Split once rather than replacing the placeholder repeatedly: a value that
    itself contains one would become the next placeholder, and the audit line
    would then be quietly wrong about what ran — worse than having no line at
    all, because it reads as authoritative.
    """
    head, *tails = sql.split(placeholder)
    rendered = [head]
    for index, tail in enumerate(tails):
        # more placeholders than parameters: leave the extras as placeholders
        rendered.append(repr(params[index]) if index < len(params) else placeholder)
        rendered.append(tail)
    return "".join(rendered)


class AdapterBase(ABC):
    """Engine-agnostic base: the tool contract, sampling policy and statement log."""

    _DEFAULT_SAMPLE_LIMIT: ClassVar[int] = 3
    _MAX_SAMPLE_LIMIT: ClassVar[int] = 100
    _DEFAULT_TOP_N: ClassVar[int] = 20
    _DEFAULT_PAGE_SIZE: ClassVar[int] = 100
    _MAX_PAGE_SIZE: ClassVar[int] = 1000

    # False when `database` names the whole source instead of selecting one inside it.
    SUPPORTS_MULTIPLE_DATABASES: ClassVar[bool] = True

    # How long the allowlist reuses a catalog walk or a schema read. Zero
    # disables caching. Only validation caches; the tools always read through.
    _CATALOG_TTL: ClassVar[float] = 60.0

    # Substrings of a native type name, matched case-insensitively in this
    # order, which is what keeps "int" from claiming "interval" and "point".
    # Opaque first: a type no statistic describes, and the group that catches
    # the substring's false positives.
    _OPAQUE_TYPE_HINTS: ClassVar[tuple[str, ...]] = (
        "blob",
        "binary",
        "bytea",
        "geometry",
        "geography",
        "point",
        "polygon",
        "linestring",
    )
    _TEMPORAL_TYPE_HINTS: ClassVar[tuple[str, ...]] = (
        "date",
        "time",
        "year",
        "interval",
    )
    _NUMERIC_TYPE_HINTS: ClassVar[tuple[str, ...]] = (
        "int",
        "serial",
        "dec",
        "num",
        "float",
        "double",
        "real",
        "money",
    )
    _CATEGORICAL_TYPE_HINTS: ClassVar[tuple[str, ...]] = (
        "char",
        "text",
        "string",
        "clob",
        "enum",
        "bool",
        "uuid",
    )

    def __init__(
        self,
        *,
        database: str = "",
        max_sample_limit: int | None = None,
        catalog_ttl: float | None = None,
    ) -> None:
        self._database = database
        # per instance, so it must not be written back onto the class
        self._max_sample_limit = (
            self._MAX_SAMPLE_LIMIT if max_sample_limit is None else max_sample_limit
        )
        self._statement_log: list[str] = []

        self._catalog_ttl = self._CATALOG_TTL if catalog_ttl is None else catalog_ttl
        # Reentrant: validating a column reads the schema, and an engine's
        # get_schema validates the container, which comes back through here.
        self._catalog_lock = threading.RLock()
        self._catalog_cache: tuple[float, frozenset[str]] | None = None
        self._schema_cache: dict[str, tuple[float, list[ColumnInfo]]] = {}

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

    def default_profile_modes(self, column: ColumnInfo) -> tuple[ProfileMode, ...]:
        """
        Which statistics are worth the round trip for this column.

        A `min_max` over free text and a `top_values` over a high-cardinality
        column are pure waste, and a scan pays for them once per column of every
        container. Matching a substring of the type name is a guess — "point"
        contains "int" — so the groups are tried in an order that resolves the
        overlaps, and an unrecognised type gets the one statistic that means
        something for any of them.

        `distinct_count` comes before `top_values` deliberately: the scan uses
        the count it produces to decide whether the top values are worth asking
        for at all. An engine whose type names this cannot read overrides it.
        """
        native = column.native_type.lower()
        if any(hint in native for hint in self._OPAQUE_TYPE_HINTS):
            return (ProfileMode.NULL_RATIO,)
        if any(hint in native for hint in self._TEMPORAL_TYPE_HINTS):
            return (ProfileMode.NULL_RATIO, ProfileMode.MIN_MAX)
        if any(hint in native for hint in self._NUMERIC_TYPE_HINTS):
            return (ProfileMode.NULL_RATIO, ProfileMode.MIN_MAX)
        if any(hint in native for hint in self._CATEGORICAL_TYPE_HINTS):
            return (
                ProfileMode.NULL_RATIO,
                ProfileMode.DISTINCT_COUNT,
                ProfileMode.TOP_VALUES,
            )
        return (ProfileMode.NULL_RATIO,)

    def close(self) -> None:
        """No-op unless a subclass holds a connection."""

    def ping(self) -> bool:
        """Still usable? A SQL adapter should round-trip to its server."""
        return True

    # ---- policy ----

    def _fresh(self, stamp: float) -> bool:
        return self._catalog_ttl > 0 and (time.monotonic() - stamp) < self._catalog_ttl

    def _known_containers(self) -> frozenset[str]:
        """
        Every container name, reused for `catalog_ttl` seconds.

        The allowlist is consulted once per container *and* once per column of
        every scan, so without the cache each of those walks the whole catalog.
        The cost is that a container created seconds ago reads as unknown until
        the entry expires; `invalidate_catalog_cache` is the way out.
        """
        with self._catalog_lock:
            cached = self._catalog_cache
            if cached is not None and self._fresh(cached[0]):
                return cached[1]
            names = self._walk_containers()
            self._catalog_cache = (time.monotonic(), names)
            return names

    def _walk_containers(self) -> frozenset[str]:
        """Uncached walk of every page. Stopping at page one would reject the
        rest as unknown, so the full walk is deliberate. An engine that can list
        names more cheaply than it can list containers overrides this, not the
        caching wrapper."""
        names: set[str] = set()
        cursor: str | None = None
        while True:
            page = self.list_containers(limit=self._MAX_PAGE_SIZE, cursor=cursor)
            names.update(c.container_name for c in page.containers)
            if page.next_cursor is None or page.next_cursor == cursor:
                return frozenset(names)
            cursor = page.next_cursor

    def _cached_schema(self, container: str) -> list[ColumnInfo]:
        """
        A container's columns, for validation only — `get_schema` as a tool must
        keep reading through to the source.

        Same trade as `_known_containers`, one level down: a column dropped
        within the ttl still passes the check, and the source then raises about
        it instead of this layer answering `UnknownColumnError`.
        `invalidate_catalog_cache` is the way out.
        """
        with self._catalog_lock:
            cached = self._schema_cache.get(container)
            if cached is not None and self._fresh(cached[0]):
                return cached[1]
            columns = self.get_schema(container)
            self._schema_cache[container] = (time.monotonic(), columns)
            return columns

    def invalidate_catalog_cache(self) -> None:
        """Forget both caches, so the next validation re-reads the source."""
        with self._catalog_lock:
            self._catalog_cache = None
            self._schema_cache.clear()

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

    def _quote_container(self, container: str) -> str:
        """
        A container name, quoted a part at a time.

        An engine with a schema layer names a container `public.users`, and
        quoting that whole string would ask for one identifier that happens to
        contain a dot — a different table, if it exists at all. Each part still
        has to be a plain identifier on its own.
        """
        return ".".join(self._quote(part) for part in container.split("."))

    def _qualify(self, container: str) -> str:
        """
        The catalog's own name for a container, given what the caller typed.

        An engine with a schema layer catalogs `public.users`, but an agent
        relaying a name someone said will ask for `users`. Where exactly one
        schema holds that name it is the one meant; where several do, saying so
        is the only honest answer — picking one would quietly read the wrong
        table. A flat catalog never reaches the search at all.
        """
        known = self._known_containers()
        if container in known or "." in container:
            return container
        matches = sorted(name for name in known if name.rpartition(".")[2] == container)
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise UnknownContainerError(
                f"{container!r} is in more than one schema; name one of "
                f"{', '.join(matches)}"
            )
        return container  # unknown, and _require_container is where that is said

    def _require_container(self, container: str) -> str:
        if (name := self._qualify(container)) not in self._known_containers():
            raise UnknownContainerError(container)
        return self._quote_container(name)

    def _require_column(self, container: str, column: str) -> str:
        cols = {c.name for c in self._cached_schema(container)}
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


class DbApiAdapterBase(SqlAdapterBase):
    """
    What every PEP 249 driver needs alike: one connection, rows as dicts, and
    the statement recorded as it was sent.

    One connection guarded by a lock, not one per thread. FastMCP runs sync
    tools in a threadpool and the scan worker has a thread of its own, so the
    connection is shared; the pool holds one adapter per database rather than
    one per thread. The cost is that statements serialise, which is the same
    trade sqlite makes — if it ever bites, the fix is a connection per thread,
    not a finer lock.

    A subclass sets its connection parameters **before** calling
    `super().__init__()`, which is what connects.
    """

    _PING_SQL: ClassVar[str] = "SELECT 1"

    def __init__(
        self,
        *,
        database: str = "",
        max_sample_limit: int | None = None,
        catalog_ttl: float | None = None,
    ) -> None:
        super().__init__(
            database=database,
            max_sample_limit=max_sample_limit,
            catalog_ttl=catalog_ttl,
        )
        self._lock = threading.Lock()
        self._closed = False
        self._conn = self._connect()
        self._after_connect()

    @abstractmethod
    def _connect(self) -> Any:
        """A live DB-API connection to this adapter's source."""
        raise NotImplementedError

    def _after_connect(self) -> None:
        """Session settings the engine wants — refusing writes, mostly."""

    # ---- lifecycle ----

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.close()

    def ping(self) -> bool:
        """
        A real round-trip, so the pool rebuilds a connection the server dropped.

        Deliberately not through `_rows`: pool bookkeeping is not a statement the
        audit trail should attribute to a tool call.
        """
        if self._closed:
            return False
        try:
            with self._lock:
                self._fetch(self._PING_SQL, ())
        except Exception:  # noqa: BLE001 - any failure means unusable
            return False
        return True

    # ---- querying ----

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        self._record_sql(render_sql(sql, params, self._PARAM))
        with self._lock:
            return self._fetch(sql, params)

    def _fetch(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        """Rows as dicts, keyed by `cursor.description` — the one thing every
        driver reports the same way, where row objects differ per driver."""
        cursor = self._conn.cursor()
        try:
            # An empty parameter sequence is not the same as none: a driver that
            # interpolates client-side reads the statement's own `%` as a
            # placeholder the moment parameters are passed at all.
            if params:
                cursor.execute(sql, tuple(params))
            else:
                cursor.execute(sql)
            if cursor.description is None:
                return []
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def _unrecorded(self, sql: str) -> list[dict[str, Any]]:
        """
        Rows for the adapter's own bookkeeping — a session setting, or asking the
        server what it just connected to.

        Kept out of the audit trail on purpose: the trail answers "what did this
        tool call read", and a statement no caller asked for does not belong in
        the answer.
        """
        with self._lock:
            return self._fetch(sql, ())

    def _session_sql(self, sql: str) -> None:
        """A session setting. A server that does not know the statement is logged
        and carried on from — these harden a connection that already works."""
        try:
            self._unrecorded(sql)
        except Exception as exc:  # noqa: BLE001 - a hardening step, not the job
            log.warning(f"{self._database or '<default>'}: {sql!r} refused: {exc}")

    def _scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """The first column of the first row, or None where there is no row."""
        rows = self._rows(sql, params)
        return next(iter(rows[0].values())) if rows else None
