from __future__ import annotations

from typing import Any, ClassVar

import pyodbc
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

# sys.objects.type: 'U' is a user table, 'V' a view.
_CONTAINER_TYPES = {"U": ContainerType.TABLE, "V": ContainerType.VIEW}

# Types whose declared length is part of what they are. A `varchar(10)` and a
# `varchar(max)` are not the same column to anyone reading the catalog.
_SIZED_TYPES = frozenset(
    {"char", "varchar", "nchar", "nvarchar", "binary", "varbinary"}
)
_PRECISION_TYPES = frozenset({"decimal", "numeric"})
# nchar/nvarchar count max_length in bytes, two per character.
_WIDE_TYPES = frozenset({"nchar", "nvarchar"})

# A binary collation, so the ordering paging relies on is the one the cursor
# comparison uses. A server's own collation is usually case-insensitive, under
# which `Orders` and `orders` are one name — enough for a walk to skip a table.
_ORDER_COLLATION = "Latin1_General_BIN2"
_QUALIFIED = f"(s.name + '.' + o.name) COLLATE {_ORDER_COLLATION}"


class MssqlAdapter(DbApiAdapterBase):
    """
    SourceAdapter over one sql server database.

    A container is named `schema.table` throughout, because `dbo.users` and
    `sales.users` are different tables and the tools have only the one string to
    go on. `get_schema("users")` still works where exactly one schema has one.
    """

    _QUOTE_OPEN: ClassVar[str] = "["
    _QUOTE_CLOSE: ClassVar[str] = "]"
    DEFAULT_PORT: ClassVar[int] = 1433
    # The driver has to be installed on this host; the name is what odbc looks it
    # up by. A `<REF>_URI` is the way to name a different one.
    DEFAULT_DRIVER: ClassVar[str] = "ODBC Driver 18 for SQL Server"
    # Seconds to wait for the login to complete. The driver's own default is 15,
    # which is short for a first connection across a site-to-site link and shows
    # up as HYT00 — an error that says nothing about the network being the cause.
    DEFAULT_LOGIN_TIMEOUT: ClassVar[int] = 30

    def __init__(
        self,
        *,
        connection_string: str | None = None,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str = "",
        driver: str | None = None,
        trust_server_certificate: bool = False,
        trust_variable: str = "<REF>_TRUST_SERVER_CERTIFICATE",
        login_timeout: int | None = None,
        max_sample_limit: int | None = None,
    ) -> None:
        # Read before `super().__init__`, which is what connects.
        self._login_timeout = (
            self.DEFAULT_LOGIN_TIMEOUT if login_timeout is None else login_timeout
        )
        # The variable an operator would actually type, so the certificate error
        # can name `SHOP_TRUST_SERVER_CERTIFICATE` rather than a placeholder the
        # reader has to translate — and would otherwise paste verbatim.
        self._trust_var = trust_variable
        self._connection_string = connection_string or _build_connection_string(
            driver=driver or self.DEFAULT_DRIVER,
            host=host or "127.0.0.1",
            port=port or self.DEFAULT_PORT,
            user=user,
            password=password,
            database=database,
            trust_server_certificate=trust_server_certificate,
            login_timeout=self._login_timeout,
        )
        if trust_server_certificate:
            # Loud, once per connection, because it is a check the operator
            # turned off rather than a default anyone can be assumed to know
            # about. The traffic is still encrypted; what is gone is the
            # assurance that the other end is who it claims to be.
            log.warning(
                "mssql: TrustServerCertificate is on for this connection. Traffic "
                "stays encrypted, but the server's identity is not verified, so "
                "this connection can be intercepted. Intended for a self-signed "
                "certificate on a network you control."
            )
        super().__init__(database=database, max_sample_limit=max_sample_limit)
        log.info(f"opened mssql {self._database}")

    def _connect(self) -> Any:
        """
        Autocommit, so a failed statement leaves no open transaction holding
        locks on a database this server is only meant to read.

        There is no session switch that makes a sql server connection read-only
        — `ApplicationIntent=ReadOnly` only routes within an availability group —
        so the guarantee here is the same one the datalake adapter gives: this
        code issues nothing but SELECTs.
        """
        try:
            return pyodbc.connect(
                self._connection_string,
                autocommit=True,
                timeout=self._login_timeout,
            )
        except pyodbc.Error as exc:
            raise _connection_error(
                exc, self._connection_string, self._login_timeout, self._trust_var
            ) from exc

    def _after_connect(self) -> None:
        # Unnamed, it is the login's default, whatever that turned out to be.
        # Named, it is checked rather than trusted: a whole `<REF>_URI` carries
        # its own `DATABASE=`, and appending a second one is read differently by
        # different drivers — so the mismatch is caught here instead.
        rows = self._unrecorded("SELECT DB_NAME() AS name")
        self._adopt_database(str(rows[0]["name"]) if rows else "")

    @classmethod
    def from_connection(
        cls,
        conn_info: ConnectionInfo,
        *,
        database: str | None = None,
        max_sample_limit: int | None = None,
    ) -> MssqlAdapter:
        if not conn_info.uri and not conn_info.host:
            raise ValueError(
                f"The mssql connection requires a {conn_info.variable('HOST')}, or a "
                f"{conn_info.variable('URI')} holding a full odbc connection "
                f"string (DRIVER={{…}};SERVER=…)."
            )
        return cls(
            connection_string=conn_info.uri,
            host=conn_info.host,
            port=conn_info.port,
            user=conn_info.user,
            password=conn_info.password,
            database=database or conn_info.database or "",
            trust_server_certificate=conn_info.trust_server_certificate,
            trust_variable=conn_info.variable("TRUST_SERVER_CERTIFICATE"),
            max_sample_limit=max_sample_limit,
        )

    # ---- statement templates ----

    def _sql_sample(self, quoted_table: str, limit: int) -> tuple[str, tuple[int, ...]]:
        """`TOP (?)`, because t-sql has no LIMIT."""
        return (f"SELECT TOP ({self._PARAM}) * FROM {quoted_table}", (limit,))

    def _sql_top_values(
        self, quoted_table: str, quoted_col: str, top_n: int
    ) -> tuple[str, tuple[int, ...]]:
        return (
            f"SELECT TOP ({self._PARAM}) {quoted_col} AS v, COUNT(*) AS c "
            f"FROM {quoted_table} GROUP BY {quoted_col} ORDER BY c DESC, v",
            (top_n,),
        )

    def _sql_null_ratio(
        self, quoted_table: str, quoted_col: str
    ) -> tuple[str, tuple[()]]:
        """`1.0` is a numeric literal to sql server, so AVG over it stays exact
        rather than rounding to the integer average the untyped form would give."""
        return (
            f"SELECT AVG(CASE WHEN {quoted_col} IS NULL THEN 1.0 ELSE 0.0 END) AS r "
            f"FROM {quoted_table}",
            (),
        )

    # 4 tools (READ ONLY)

    def list_databases(self) -> list[str]:
        """Every database this login can see. Reaching one means a connection of
        its own, which the pool builds on demand."""
        rows = self._rows(
            "SELECT name FROM sys.databases "
            "WHERE database_id > 4 AND state_desc = 'ONLINE' "
            "AND HAS_DBACCESS(name) = 1 "
            f"ORDER BY name COLLATE {_ORDER_COLLATION}"
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
        params: list[Any] = [page_size + 1]  # TOP comes first in the statement
        filters = ""
        if schema is not None:
            filters += " AND s.name = ?"
            params.append(schema)
        if cursor is not None:
            filters += f" AND {_QUALIFIED} > ?"
            params.append(cursor)

        rows = self._rows(
            f"""
            SELECT TOP (?)
                   s.name AS schema_name,
                   o.name AS container_name,
                   o.type AS kind,
                   o.modify_date AS modified_at,
                   CAST(ep.value AS nvarchar(max)) AS comment,
                   (SELECT SUM(ps.row_count)
                      FROM sys.dm_db_partition_stats ps
                     WHERE ps.object_id = o.object_id
                       AND ps.index_id IN (0, 1)) AS estimated_count
            FROM sys.objects o
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            LEFT JOIN sys.extended_properties ep
              ON ep.class = 1
             AND ep.major_id = o.object_id
             AND ep.minor_id = 0
             AND ep.name = 'MS_Description'
            WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0{filters}
            ORDER BY {_QUALIFIED}
            """,  # noqa: S608 - the interpolated parts are constants, not input
            tuple(params),
        )
        page, has_more = rows[:page_size], len(rows) > page_size
        containers = [
            ContainerInfo(
                database=self._database,
                schema_name=str(row["schema_name"]),
                container_name=f"{row['schema_name']}.{row['container_name']}",
                container_type=_CONTAINER_TYPES[str(row["kind"]).strip()],
                # A view has no partitions, so the subquery gives it no count —
                # counting one would mean running it.
                estimated_count=(
                    None
                    if row["estimated_count"] is None
                    else int(row["estimated_count"])
                ),
                native_description=row["comment"] or None,
                last_modified_at=as_text(row["modified_at"]),
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
            SELECT c.name AS name,
                   t.name AS type_name,
                   c.max_length AS max_length,
                   c.precision AS precision,
                   c.scale AS scale,
                   c.is_nullable AS nullable,
                   CAST(ep.value AS nvarchar(max)) AS comment
            FROM sys.columns c
            JOIN sys.types t ON t.user_type_id = c.user_type_id
            LEFT JOIN sys.extended_properties ep
              ON ep.class = 1
             AND ep.major_id = c.object_id
             AND ep.minor_id = c.column_id
             AND ep.name = 'MS_Description'
            WHERE c.object_id = OBJECT_ID(?)
            ORDER BY c.column_id
            """,
            (quoted,),
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
                    native_type=_native_type(row),
                    nullable=bool(row["nullable"]),
                    is_pk=str(row["name"]) in primary_keys,
                    is_fk=target is not None,
                    native_description=row["comment"] or None,
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
        `list_containers`, summing partition statistics on the way."""
        rows = self._rows(
            """
            SELECT s.name + '.' + o.name AS name
            FROM sys.objects o
            JOIN sys.schemas s ON s.schema_id = o.schema_id
            WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0
            """
        )
        return frozenset(str(row["name"]) for row in rows)

    def _primary_keys(self, quoted: str) -> frozenset[str]:
        rows = self._rows(
            """
            SELECT c.name AS name
            FROM sys.indexes i
            JOIN sys.index_columns ic
              ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c
              ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.object_id = OBJECT_ID(?) AND i.is_primary_key = 1
            """,
            (quoted,),
        )
        return frozenset(str(row["name"]) for row in rows)

    def _foreign_keys(self, quoted: str) -> dict[str, tuple[str, str | None]]:
        """Source column -> (qualified target container, target column)."""
        rows = self._rows(
            """
            SELECT pc.name AS name,
                   rs.name + '.' + rt.name AS target,
                   rc.name AS target_column
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc
              ON fkc.constraint_object_id = fk.object_id
            JOIN sys.columns pc
              ON pc.object_id = fkc.parent_object_id
             AND pc.column_id = fkc.parent_column_id
            JOIN sys.columns rc
              ON rc.object_id = fkc.referenced_object_id
             AND rc.column_id = fkc.referenced_column_id
            JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
            WHERE fk.parent_object_id = OBJECT_ID(?)
            """,
            (quoted,),
        )
        return {
            str(row["name"]): (str(row["target"]), row["target_column"]) for row in rows
        }


class MssqlConnectionError(Exception):
    """A failed login, said in terms of what to change."""


# What odbc reports, and what it actually means for this deployment. Matched on
# the sqlstate plus a phrase, because 08001 covers everything from "no route"
# to "wrong certificate" and the remedies are nothing alike.
_CONNECTION_HINTS: tuple[tuple[str, str, str], ...] = (
    (
        "HYT00",
        "",
        "nothing answered at {server} within {timeout}s. From inside a container "
        "this is usually the address rather than the database: `localhost` is the "
        "container itself, and the host it runs on is `host.docker.internal`. "
        "Check the route and the firewall before the credentials.",
    ),
    (
        "08001",
        "certificate verify failed",
        "{server} presented a certificate this host does not trust, which is what "
        "a self-signed certificate looks like — and what a client with 'trust "
        "server certificate' ticked, such as DBeaver, connects through without "
        "saying so. Put `{trust}=1` in the environment (the .env the server "
        "reads) to keep the encryption and skip the check, or install the "
        "issuing CA where the container can see it.",
    ),
    (
        "08001",
        "",
        "could not reach {server}. The port may be closed, or the instance may "
        "not be listening on TCP.",
    ),
    (
        "28000",
        "",
        "{server} refused the login. The account, the password or the default "
        "database is wrong; the network is fine.",
    ),
    (
        "IM002",
        "",
        "the odbc driver named in the connection string is not installed here. "
        "This image was not built for mssql — rebuild it with MCP_ENGINE=mssql.",
    ),
)


def _server_of(connection_string: str) -> str:
    """
    The `SERVER=` value, for an error message.

    Only that one keyword: the string it comes from also holds `PWD=`, and an
    error is a thing that gets pasted into tickets and chat.
    """
    for part in connection_string.split(";"):
        keyword, _, value = part.partition("=")
        if keyword.strip().upper() == "SERVER":
            return value.strip().strip("{}") or "the server"
    return "the server"


def _connection_error(
    exc: Exception, connection_string: str, timeout: int, trust_var: str
) -> MssqlConnectionError:
    """
    Turn pyodbc's tuple into one sentence naming the thing to change.

    The driver's own text is kept on the end rather than replaced: it is the
    only part an internet search will match, and whoever ends up reading this
    may well need to search it.
    """
    args = getattr(exc, "args", ())
    sqlstate = str(args[0]) if args else ""
    detail = str(args[1]) if len(args) > 1 else str(exc)
    lowered = detail.lower()

    for state, phrase, hint in _CONNECTION_HINTS:
        if sqlstate != state:
            continue
        if phrase and phrase not in lowered:
            continue
        return MssqlConnectionError(
            hint.format(
                server=_server_of(connection_string),
                timeout=timeout,
                trust=trust_var,
            )
            + f" (odbc {sqlstate}: {detail})"
        )
    return MssqlConnectionError(f"mssql connection failed (odbc {sqlstate}: {detail})")


def _build_connection_string(
    *,
    driver: str,
    host: str,
    port: int,
    user: str | None,
    password: str | None,
    database: str,
    trust_server_certificate: bool = False,
    login_timeout: int | None = None,
) -> str:
    """
    An odbc connection string from the parts `<REF>_*` carries.

    Encryption stays on and, by default, the certificate stays checked.
    `<REF>_TRUST_SERVER_CERTIFICATE=1` keeps the encryption and drops the
    check, which is what an on-premises server with a self-signed certificate
    needs. It is a flag of its own rather than something to be inferred: the
    same failure means "the certificate is self-signed, as expected" and "the
    certificate stopped verifying", and only an operator can tell those apart.
    A whole `<REF>_URI` still overrides everything here.
    """
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={_odbc_value(f'{host},{port}')}",
        "Encrypt=yes",
    ]
    if trust_server_certificate:
        parts.append("TrustServerCertificate=yes")
    if login_timeout is not None:
        # Named `Connection Timeout` in the connection string; pyodbc's own
        # `timeout=` argument sets the same thing on the handle. Both are set,
        # because which one a given driver honours has moved between versions.
        parts.append(f"Connection Timeout={int(login_timeout)}")
    if database:
        parts.append(f"DATABASE={_odbc_value(database)}")
    if user:
        parts.append(f"UID={_odbc_value(user)}")
        parts.append(f"PWD={_odbc_value(password or '')}")
    else:
        # No login named: the odbc driver takes the caller's windows identity.
        parts.append("Trusted_Connection=yes")
    return ";".join(parts)


def _odbc_value(value: str) -> str:
    """
    A connection-string value, braced where it has to be.

    A `;` in a password ends the keyword as far as odbc is concerned, and the
    rest of the password is then read as connection settings — which fails as a
    login error, so it reads as a wrong password rather than as a string this
    built wrong. Braces are odbc's own escape, and a `}` inside them is doubled.

    Only where it is needed, so an ordinary value goes out looking like itself.
    """
    if value == value.strip() and not any(char in value for char in ";{}"):
        return value
    return "{" + value.replace("}", "}}") + "}"


def _native_type(row: dict[str, Any]) -> str:
    """
    The type as it was declared, not just its family.

    `sys.columns` splits a type into name, length, precision and scale, and only
    the family is worth anything on its own — `varchar` alone does not say
    whether the column holds a code or a document.
    """
    name = str(row["type_name"])
    if name in _PRECISION_TYPES:
        return f"{name}({row['precision']},{row['scale']})"
    if name in _SIZED_TYPES:
        length = int(row["max_length"])
        if length == -1:
            return f"{name}(max)"
        # nchar/nvarchar measure bytes; everyone talks about them in characters
        return f"{name}({length // 2 if name in _WIDE_TYPES else length})"
    return name
