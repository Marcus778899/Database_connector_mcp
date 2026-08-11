from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import unquote, urlparse

import pymysql
from loggerhelper import log

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
from src.utils.serialize import as_text, jsonify

_CONTAINER_TYPES = {
    "BASE TABLE": ContainerType.TABLE,
    "VIEW": ContainerType.VIEW,
    "SYSTEM VIEW": ContainerType.VIEW,
}

# The server's own schemas. Never a user's catalog, and listing them as
# databases would offer an inventory of the server's bookkeeping.
_SYSTEM_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")

# mysql fills a view's TABLE_COMMENT with the word VIEW. That is the type, which
# `container_type` already carries, so it is not a description of anything.
_NOT_A_COMMENT = frozenset({"", "VIEW"})

# Paging compares against the cursor, so the ordering has to be the one the
# comparison uses. information_schema's collation is case-insensitive, under
# which `Orders` and `orders` are one name — enough for a walk to skip a table.
_BINARY_NAME = "CAST(TABLE_NAME AS BINARY)"

# What a `<REF>_URI` may call itself. mariadb answers the same protocol, and the
# `+pymysql` form is what sqlalchemy writes.
_URI_SCHEMES = frozenset({"mysql", "mariadb", "mysql+pymysql"})


class MysqlAdapter(DbApiAdapterBase):
    """
    SourceAdapter over one mysql or mariadb database.

    mysql has no schema layer below the database — `SCHEMA` is a synonym for
    `DATABASE` — so a container is named by its bare table name and reaching
    another schema means another connection, which the pool builds on demand.
    """

    _PARAM: ClassVar[str] = "%s"
    _QUOTE_OPEN: ClassVar[str] = "`"
    _QUOTE_CLOSE: ClassVar[str] = "`"
    DEFAULT_PORT: ClassVar[int] = 3306

    def __init__(
        self,
        *,
        database: str,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        max_sample_limit: int | None = None,
    ) -> None:
        if not database:
            raise ValueError(
                "The mysql connection requires a <REF>_DB: a connection with no "
                "database selected has no catalog to read."
            )
        # Read before `super().__init__`, which is what connects.
        self._host = host or "127.0.0.1"
        self._port = port or self.DEFAULT_PORT
        self._user = user
        self._password = password
        super().__init__(database=database, max_sample_limit=max_sample_limit)
        log.info(f"opened mysql {self._host}:{self._port}/{self._database} read-only")

    def _connect(self) -> Any:
        return pymysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password or "",
            database=self._database,
            # utf8mb4 is the only charset that holds every unicode codepoint;
            # mysql's "utf8" is three bytes and drops the rest.
            charset="utf8mb4",
            autocommit=True,
        )

    def _after_connect(self) -> None:
        # These tools never write, and with autocommit every statement is its own
        # transaction — so this refuses a write at the server rather than by
        # convention. Older servers that do not know it are logged and carried on
        # from.
        self._session_sql("SET SESSION TRANSACTION READ ONLY")

    @classmethod
    def from_connection(
        cls,
        conn_info: ConnectionInfo,
        *,
        database: str | None = None,
        max_sample_limit: int | None = None,
    ) -> MysqlAdapter:
        parts = _from_uri(conn_info.uri) if conn_info.uri else {}
        host = conn_info.host or parts.get("host")
        if not host:
            raise ValueError(
                "The mysql connection requires a <REF>_HOST, or a <REF>_URI "
                "(mysql://user:password@host:port/database)."
            )
        return cls(
            host=host,
            port=conn_info.port or parts.get("port"),
            user=conn_info.user or parts.get("user"),
            password=conn_info.password or parts.get("password"),
            database=database or conn_info.database or parts.get("database") or "",
            max_sample_limit=max_sample_limit,
        )

    # 4 tools (READ ONLY)

    def list_databases(self) -> list[str]:
        rows = self._rows(
            "SELECT SCHEMA_NAME AS name FROM information_schema.SCHEMATA "
            f"WHERE SCHEMA_NAME NOT IN ({', '.join(['%s'] * len(_SYSTEM_SCHEMAS))}) "
            "ORDER BY SCHEMA_NAME",
            _SYSTEM_SCHEMAS,
        )
        return [str(row["name"]) for row in rows]

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
            raise UnknownContainerError(
                f"this connection serves {self._database!r}, got {database!r}"
            )
        if schema is not None and schema != self._database:
            raise UnknownContainerError(
                f"mysql's schema is its database ({self._database!r}), "
                f"got schema={schema!r}"
            )

        page_size = self._cap_page_size(limit)
        sql = """
            SELECT TABLE_NAME AS name,
                   TABLE_TYPE AS kind,
                   TABLE_ROWS AS estimated_count,
                   TABLE_COMMENT AS comment,
                   UPDATE_TIME AS updated_at
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
        """
        params: list[Any] = [self._database]
        if cursor is not None:
            sql += f" AND {_BINARY_NAME} > %s"
            params.append(cursor)
        sql += f" ORDER BY {_BINARY_NAME} LIMIT %s"
        params.append(page_size + 1)  # one extra row tells us whether more remain

        rows = self._rows(sql, tuple(params))
        page, has_more = rows[:page_size], len(rows) > page_size
        containers = [
            ContainerInfo(
                database=self._database,
                schema_name=None,
                container_name=str(row["name"]),
                container_type=_CONTAINER_TYPES.get(
                    str(row["kind"]), ContainerType.TABLE
                ),
                # An InnoDB TABLE_ROWS is sampled from the index, not counted —
                # which is what `estimated_count` is for. A view has none.
                estimated_count=(
                    None
                    if row["estimated_count"] is None
                    else int(row["estimated_count"])
                ),
                native_description=_comment(row["comment"]),
                last_modified_at=as_text(row["updated_at"]),
            )
            for row in page
        ]
        return ContainerPage(
            containers=containers,
            next_cursor=str(page[-1]["name"]) if has_more else None,
        )

    def get_schema(self, container: str) -> list[ColumnInfo]:
        self._require_container(container)
        foreign_keys = self._foreign_keys(container)
        rows = self._rows(
            """
            SELECT COLUMN_NAME AS name,
                   COLUMN_TYPE AS native_type,
                   IS_NULLABLE AS nullable,
                   COLUMN_KEY AS column_key,
                   COLUMN_COMMENT AS comment
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (self._database, container),
        )
        if not rows:
            raise UnknownContainerError(container)

        columns = []
        for ordinal, row in enumerate(rows, start=1):
            target, target_column = foreign_keys.get(str(row["name"]), (None, None))
            columns.append(
                ColumnInfo(
                    name=str(row["name"]),
                    ordinal=ordinal,
                    # COLUMN_TYPE, not DATA_TYPE: `varchar(50) `and `int unsigned`
                    # say something DATA_TYPE's `varchar` and `int` do not.
                    native_type=str(row["native_type"]),
                    nullable=row["nullable"] == "YES",
                    is_pk=row["column_key"] == "PRI",
                    is_fk=target is not None,
                    native_description=_comment(row["comment"]),
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
                min_value=as_text(row["lo"]), max_value=as_text(row["hi"])
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
        `list_containers`, reading a comment and a row estimate on the way."""
        rows = self._rows(
            "SELECT TABLE_NAME AS name FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s",
            (self._database,),
        )
        return frozenset(str(row["name"]) for row in rows)

    def _foreign_keys(self, container: str) -> dict[str, tuple[str, str | None]]:
        """Source column -> (target container, target column). A reference to
        another database is qualified, since a bare name would read as a table in
        this one."""
        rows = self._rows(
            """
            SELECT COLUMN_NAME AS name,
                   REFERENCED_TABLE_SCHEMA AS target_schema,
                   REFERENCED_TABLE_NAME AS target,
                   REFERENCED_COLUMN_NAME AS target_column
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY ORDINAL_POSITION
            """,
            (self._database, container),
        )
        keys: dict[str, tuple[str, str | None]] = {}
        for row in rows:
            target = str(row["target"])
            if row["target_schema"] and row["target_schema"] != self._database:
                target = f"{row['target_schema']}.{target}"
            keys[str(row["name"])] = (target, row["target_column"])
        return keys


def _from_uri(uri: str) -> dict[str, Any]:
    """
    The parts of a `mysql://user:password@host:port/database` url. pymysql takes
    arguments rather than a url, so somebody has to take it apart.

    The scheme is checked because nothing else will: `urlparse` reads a postgres
    url quite happily, and the mistake would otherwise surface as a connection
    refused on the wrong port.
    """
    parsed = urlparse(uri)
    if parsed.scheme and parsed.scheme not in _URI_SCHEMES:
        raise ValueError(
            f"<REF>_URI names {parsed.scheme!r}, which is not a mysql url; "
            f"expected one of {', '.join(sorted(_URI_SCHEMES))}"
        )
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "database": parsed.path.lstrip("/") or None,
    }


def _comment(value: Any) -> str | None:
    """A comment, or None where mysql has written something that is not one."""
    text = "" if value is None else str(value)
    return None if text in _NOT_A_COMMENT else text
