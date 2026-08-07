from __future__ import annotations

from typing import Any, ClassVar

import psycopg

from src.adapter.base import (
    AdapterBase,
    DbApiAdapterBase,
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
from src.utils.serialize import as_text, jsonify

# relkind -> what to call it. A partitioned table (`p`) and a foreign table (`f`)
# are read exactly like a table; a materialised view holds rows but is still a
# view, and calling it one is what tells a reader the rows are derived.
_CONTAINER_TYPES = {
    "r": ContainerType.TABLE,
    "p": ContainerType.TABLE,
    "f": ContainerType.TABLE,
    "v": ContainerType.VIEW,
    "m": ContainerType.VIEW,
}

# Only these keep a row estimate worth reporting. A plain view has none — its
# `reltuples` is whatever the planner last guessed — and a foreign table's would
# describe the local stub rather than the remote data.
_COUNTED_KINDS = frozenset({"r", "p", "m"})

# The catalog, minus postgres' own. `left(nspname, 3)` rather than a LIKE
# pattern: a `%` in the statement is a placeholder to the driver the moment any
# parameter is passed with it.
_CATALOG_WHERE = """
    c.relkind IN ('r','p','v','m','f')
    AND NOT c.relispartition
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND left(n.nspname, 3) <> 'pg_'
"""

# Ordering and paging both run on the qualified name, under the C collation, so
# the cursor comparison cannot disagree with the sort the way a locale-aware
# ordering of two separate columns can.
_QUALIFIED = """(n.nspname || '.' || c.relname) COLLATE "C" """


class PostgresAdapter(DbApiAdapterBase):
    """
    SourceAdapter over one postgres database.

    A container is named `schema.table` throughout, because a bare name is not
    unique in a database with more than one schema and the tools have only the
    one string to go on. `get_schema("users")` still works where exactly one
    schema has a `users`.
    """

    _PARAM: ClassVar[str] = "%s"
    DEFAULT_PORT: ClassVar[int] = 5432

    def __init__(
        self,
        *,
        conninfo: str | None = None,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str = "",
        max_sample_limit: int | None = None,
    ) -> None:
        # Read before `super().__init__`, which is what connects.
        self._conninfo = conninfo
        self._params = {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "dbname": database or None,
        }
        super().__init__(database=database, max_sample_limit=max_sample_limit)
        log.info(f"opened postgres {self._database} read-only")

    def _connect(self) -> Any:
        """
        Autocommit, so a failed statement leaves nothing to roll back.

        Without it psycopg opens a transaction for the first statement and every
        later one fails with "current transaction is aborted" — one unreadable
        table would take the rest of the scan with it.
        """
        params = {k: v for k, v in self._params.items() if v is not None}
        if self._conninfo:
            # A url names its own database, and the pool builds one adapter per
            # database off the same `<REF>_URI` — so a requested one has to be
            # able to win. psycopg merges keywords over the url, which is what
            # makes that a two-line matter rather than string surgery.
            override = {"dbname": params["dbname"]} if params.get("dbname") else {}
            return psycopg.connect(self._conninfo, autocommit=True, **override)
        return psycopg.connect(autocommit=True, **params)

    def _after_connect(self) -> None:
        # These tools never write. Enforced by the server rather than by
        # convention, so a write that ever slips in fails loudly.
        self._session_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        # libpq defaults the database to the user's name, so an unnamed one has
        # to be asked for rather than reported empty. Naming one is checked
        # rather than trusted: every row this serves is labelled with it.
        rows = self._unrecorded("SELECT current_database() AS name")
        self._adopt_database(str(rows[0]["name"]) if rows else "")

    @classmethod
    def from_connection(
        cls,
        conn_info: ConnectionInfo,
        *,
        database: str | None = None,
        max_sample_limit: int | None = None,
    ) -> PostgresAdapter:
        if not conn_info.uri and not conn_info.host:
            raise ValueError(
                "The postgres connection requires a <REF>_HOST, or a <REF>_URI "
                "(postgresql://… — also the way to reach a unix socket)."
            )
        return cls(
            conninfo=conn_info.uri,
            host=conn_info.host,
            port=conn_info.port,
            user=conn_info.user,
            password=conn_info.password,
            database=database or conn_info.database or "",
            max_sample_limit=max_sample_limit,
        )

    # 4 tools (READ ONLY)

    def list_databases(self) -> list[str]:
        """Every database this connection could be pointed at. Reaching one means
        a connection of its own, which the pool builds on demand."""
        rows = self._rows(
            "SELECT datname FROM pg_database "
            "WHERE datallowconn AND NOT datistemplate "
            'ORDER BY datname COLLATE "C"'
        )
        return [str(row["datname"]) for row in rows]

    def list_containers(
        self,
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage:
        """
        One page of the catalog, ordered by qualified name.

        Keyset, not offset: the cursor is the last name returned, so a table
        appearing or disappearing mid-walk cannot make the caller skip or repeat
        one.
        """
        if database is not None and database != self._database:
            raise UnknownContainerError(
                f"this connection serves {self._database!r}, got {database!r}"
            )

        page_size = self._cap_page_size(limit)
        sql = f"""
            SELECT n.nspname AS schema_name,
                   c.relname AS container_name,
                   c.relkind AS kind,
                   c.reltuples::bigint AS estimated_count,
                   obj_description(c.oid, 'pg_class') AS comment
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {_CATALOG_WHERE}
        """  # noqa: S608 - no caller input reaches this; the filters are below
        params: list[Any] = []
        if schema is not None:
            sql += " AND n.nspname = %s"
            params.append(schema)
        if cursor is not None:
            sql += f" AND {_QUALIFIED} > %s"
            params.append(cursor)
        sql += f" ORDER BY {_QUALIFIED} LIMIT %s"
        params.append(page_size + 1)  # one extra row tells us whether more remain

        rows = self._rows(sql, tuple(params))
        page, has_more = rows[:page_size], len(rows) > page_size
        containers = [
            ContainerInfo(
                database=self._database,
                schema_name=row["schema_name"],
                container_name=f"{row['schema_name']}.{row['container_name']}",
                container_type=_CONTAINER_TYPES[row["kind"]],
                estimated_count=_estimate(row["kind"], row["estimated_count"]),
                native_description=row["comment"],
            )
            for row in page
        ]
        last = page[-1] if page else None
        return ContainerPage(
            containers=containers,
            next_cursor=(
                f"{last['schema_name']}.{last['container_name']}"
                if has_more and last is not None
                else None
            ),
        )

    def get_schema(self, container: str) -> list[ColumnInfo]:
        name = self._qualify(container)
        quoted = self._require_container(name)
        primary_keys = self._primary_keys(quoted)
        foreign_keys = self._foreign_keys(quoted)

        rows = self._rows(
            """
            SELECT a.attname AS name,
                   format_type(a.atttypid, a.atttypmod) AS native_type,
                   NOT a.attnotnull AS nullable,
                   col_description(a.attrelid, a.attnum) AS comment
            FROM pg_attribute a
            WHERE a.attrelid = %s::regclass
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (quoted,),
        )
        if not rows:
            # every relation has columns, so none means it went away under us
            raise UnknownContainerError(container)

        columns = []
        for ordinal, row in enumerate(rows, start=1):
            target, target_column = foreign_keys.get(row["name"], (None, None))
            columns.append(
                ColumnInfo(
                    name=row["name"],
                    # Dense, not `attnum`: postgres keeps the numbers of dropped
                    # columns, and a gap in the ordinals would read as a column
                    # this failed to report.
                    ordinal=ordinal,
                    native_type=row["native_type"],
                    nullable=bool(row["nullable"]),
                    is_pk=row["name"] in primary_keys,
                    is_fk=target is not None,
                    native_description=row["comment"],
                    references_container=target,
                    references_column=target_column,
                )
            )
        return columns

    def get_sample(
        self, container: str, limit: int = AdapterBase._DEFAULT_SAMPLE_LIMIT
    ) -> list[dict[str, Any]]:
        quoted = self._require_container(container)
        sql, params = self._sql_sample(quoted, self._cap_limit(limit))
        return [
            {key: jsonify(value) for key, value in row.items()}
            for row in self._rows(sql, params)
        ]

    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult:
        name = self._qualify(container)
        quoted = self._require_container(name)
        quoted_column = self._require_column(name, column)

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
                min_value=as_text(row["lo"]),
                max_value=as_text(row["hi"]),
            )

        if mode == ProfileMode.TOP_VALUES:
            sql, params = self._sql_top_values(
                quoted, quoted_column, self._DEFAULT_TOP_N
            )
            return ProfileResult(
                top_values=[
                    TopValue(value=as_text(row["v"]) or "", count=int(row["c"]))
                    for row in self._rows(sql, params)
                ]
            )

        raise ValueError(f"unsupported profile mode: {mode!r}")

    # ---- catalog helpers ----

    def _walk_containers(self) -> frozenset[str]:
        """Names in one statement. The inherited walk pages through
        `list_containers`, reading a comment per container on the way."""
        rows = self._rows(
            f"""
            SELECT n.nspname || '.' || c.relname AS name
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE {_CATALOG_WHERE}
            """  # noqa: S608 - a constant filter, no caller input
        )
        return frozenset(str(row["name"]) for row in rows)

    def _primary_keys(self, quoted: str) -> frozenset[str]:
        rows = self._rows(
            """
            SELECT a.attname AS name
            FROM pg_constraint con
            JOIN LATERAL unnest(con.conkey) AS k(attnum) ON TRUE
            JOIN pg_attribute a
              ON a.attrelid = con.conrelid AND a.attnum = k.attnum
            WHERE con.conrelid = %s::regclass AND con.contype = 'p'
            """,
            (quoted,),
        )
        return frozenset(str(row["name"]) for row in rows)

    def _foreign_keys(self, quoted: str) -> dict[str, tuple[str, str | None]]:
        """Source column -> (qualified target container, target column).

        Position by position: a composite key's third column references the
        third column of the target, and pairing them by ordinal is the only way
        to say which.
        """
        rows = self._rows(
            """
            SELECT a.attname AS name,
                   tn.nspname || '.' || t.relname AS target,
                   ta.attname AS target_column
            FROM pg_constraint con
            JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
              ON TRUE
            JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS f(attnum, ord)
              ON f.ord = k.ord
            JOIN pg_attribute a
              ON a.attrelid = con.conrelid AND a.attnum = k.attnum
            JOIN pg_attribute ta
              ON ta.attrelid = con.confrelid AND ta.attnum = f.attnum
            JOIN pg_class t ON t.oid = con.confrelid
            JOIN pg_namespace tn ON tn.oid = t.relnamespace
            WHERE con.conrelid = %s::regclass AND con.contype = 'f'
            """,
            (quoted,),
        )
        return {
            str(row["name"]): (str(row["target"]), row["target_column"]) for row in rows
        }


def _estimate(kind: str, reltuples: int | None) -> int | None:
    """
    The planner's row estimate, where there is one to report.

    Negative means never analysed (postgres 14 and later say so with -1), and
    an estimate nobody has ever gathered is not a count — reporting the -1, or
    reading it as zero, would both be wrong.
    """
    if kind not in _COUNTED_KINDS or reltuples is None or reltuples < 0:
        return None
    return int(reltuples)
