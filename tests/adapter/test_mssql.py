from datetime import datetime
from typing import Any

import pytest

from src.adapter import mssql
from src.adapter.base import UnknownColumnError, UnknownContainerError
from src.adapter.mssql import MssqlAdapter, _build_connection_string
from src.core.config import ConnectionInfo
from src.core.contracts import ContainerType, ProfileMode, SourceAdaptor

# dbo.users and sales.users both exist, which is why a container is named
# `schema.table` here.
CATALOG = (
    "SELECT s.name + '.' + o.name AS name",
    ("name",),
    [("dbo.orders",), ("dbo.users",), ("sales.users",)],
)

CONTAINERS = (
    "dm_db_partition_stats",
    (
        "schema_name",
        "container_name",
        "kind",
        "modified_at",
        "comment",
        "estimated_count",
    ),
    [
        ("dbo", "orders", "U", datetime(2024, 5, 1, 9, 30), None, 120),
        ("dbo", "recent_orders", "V", None, None, None),
        ("dbo", "users", "U", None, "people who signed up", 3),
    ],
)

COLUMNS = (
    "FROM sys.columns",
    ("name", "type_name", "max_length", "precision", "scale", "nullable", "comment"),
    [
        ("id", "int", 4, 10, 0, False, None),
        ("email", "nvarchar", 510, 0, 0, False, "login address"),
        ("amount", "decimal", 9, 12, 2, True, None),
    ],
)

PRIMARY_KEYS = ("is_primary_key = 1", ("name",), [("id",)])
FOREIGN_KEYS = (
    "sys.foreign_keys",
    ("name", "target", "target_column"),
    [("user_id", "dbo.users", "id")],
)

RULES = [CATALOG, CONTAINERS, COLUMNS, PRIMARY_KEYS, FOREIGN_KEYS]


@pytest.fixture
def adapter(wire) -> MssqlAdapter:
    return wire(MssqlAdapter, RULES, database="shop")


# ---- construction ----


def test_satisfies_source_adaptor_protocol(adapter: MssqlAdapter):
    checked: SourceAdaptor = adapter
    assert isinstance(checked, SourceAdaptor)


def test_from_connection_without_a_host_or_uri_is_refused():
    with pytest.raises(ValueError, match="requires a <REF>_HOST"):
        MssqlAdapter.from_connection(ConnectionInfo(user="reader"))


def test_the_connection_string_is_built_from_the_parts(connected):
    MssqlAdapter.from_connection(
        ConnectionInfo(
            host="db.internal",
            port=1434,
            user="reader",
            password="s3cret",
            database="shop",
        )
    )

    assert connected.args[0] == (
        "DRIVER={ODBC Driver 18 for SQL Server};SERVER=db.internal,1434;"
        "Encrypt=yes;DATABASE=shop;UID=reader;PWD=s3cret"
    )
    assert connected.kwargs == {"autocommit": True}


def test_encryption_stays_on_and_the_certificate_stays_checked():
    """Trusting any certificate is a decision to make on purpose, in a
    `<REF>_URI` — not this adapter's default."""
    built = _build_connection_string(
        driver="d", host="h", port=1, user=None, password=None, database=""
    )

    assert "Encrypt=yes" in built
    assert "TrustServerCertificate" not in built


def test_no_login_means_the_hosts_own_identity():
    built = _build_connection_string(
        driver="d", host="h", port=1, user=None, password=None, database="shop"
    )

    assert "Trusted_Connection=yes" in built
    assert "UID=" not in built


def test_a_uri_is_taken_as_a_whole_odbc_connection_string(connected):
    MssqlAdapter.from_connection(
        ConnectionInfo(uri="DRIVER={FreeTDS};SERVER=db;TrustServerCertificate=yes")
    )

    assert connected.args[0] == "DRIVER={FreeTDS};SERVER=db;TrustServerCertificate=yes"


def test_an_unnamed_database_is_read_back_from_the_server(wire):
    """The login's default database, whatever that turned out to be."""
    adapter = wire(MssqlAdapter, [("DB_NAME()", ("name",), [("shop",)])], database="")

    assert adapter._database == "shop"
    # the adapter's own bookkeeping is not a statement a tool call asked for
    assert adapter.pop_rendered_sql() is None


# ---- catalog ----


def test_list_databases_leaves_out_the_system_ones(wire):
    adapter = wire(
        MssqlAdapter,
        [("FROM sys.databases", ("name",), [("shop",), ("analytics",)])],
        database="shop",
    )

    assert adapter.list_databases() == ["shop", "analytics"]
    # master, tempdb, model and msdb are database_id 1..4
    assert "database_id > 4" in adapter._conn.find("sys.databases")[0]


def test_list_containers_names_a_container_by_schema_and_table(adapter: MssqlAdapter):
    containers = adapter.list_containers().containers

    assert [c.container_name for c in containers] == [
        "dbo.orders",
        "dbo.recent_orders",
        "dbo.users",
    ]
    assert [c.schema_name for c in containers] == ["dbo", "dbo", "dbo"]


def test_the_container_type_is_reported(adapter: MssqlAdapter):
    kinds = {
        c.container_name: c.container_type for c in adapter.list_containers().containers
    }

    assert kinds["dbo.users"] == ContainerType.TABLE
    assert kinds["dbo.recent_orders"] == ContainerType.VIEW


def test_only_what_has_partitions_is_counted(adapter: MssqlAdapter):
    """Counting a view would mean running it."""
    counts = {
        c.container_name: c.estimated_count
        for c in adapter.list_containers().containers
    }

    assert counts["dbo.orders"] == 120
    assert counts["dbo.recent_orders"] is None


def test_a_table_description_is_reported(adapter: MssqlAdapter):
    """sql server keeps one as an extended property rather than a comment."""
    comments = {
        c.container_name: c.native_description
        for c in adapter.list_containers().containers
    }

    assert comments["dbo.users"] == "people who signed up"
    assert comments["dbo.orders"] is None


def test_freshness_is_reported(adapter: MssqlAdapter):
    stamps = {
        c.container_name: c.last_modified_at
        for c in adapter.list_containers().containers
    }

    assert stamps["dbo.orders"] == "2024-05-01T09:30:00"
    assert stamps["dbo.users"] is None


def test_the_page_size_is_a_parameter_of_top_because_t_sql_has_no_limit(
    adapter: MssqlAdapter,
):
    adapter.list_containers(limit=2, cursor="dbo.orders")

    statement, params = adapter._conn.find("dm_db_partition_stats")

    assert statement.startswith("SELECT TOP (?)")
    # TOP comes first in the statement, so it comes first in the parameters
    assert params == (3, "dbo.orders")


def test_paging_compares_the_way_it_orders(adapter: MssqlAdapter):
    """A server collation is usually case-insensitive, under which `Orders` and
    `orders` are one name — enough for a walk to skip a table."""
    adapter.list_containers(cursor="dbo.orders")

    statement, _ = adapter._conn.find("dm_db_partition_stats")

    assert "(s.name + '.' + o.name) COLLATE Latin1_General_BIN2 > ?" in statement
    assert statement.endswith(
        "ORDER BY (s.name + '.' + o.name) COLLATE Latin1_General_BIN2"
    )


def test_another_database_is_refused(adapter: MssqlAdapter):
    with pytest.raises(UnknownContainerError, match="serves 'shop'"):
        adapter.list_containers(database="other")


# ---- schema ----


def test_get_schema(adapter: MssqlAdapter):
    columns = adapter.get_schema("dbo.users")

    assert [c.name for c in columns] == ["id", "email", "amount"]
    assert [c.ordinal for c in columns] == [1, 2, 3]
    assert [c.nullable for c in columns] == [False, False, True]


def test_the_declared_type_is_reported_not_just_its_family(adapter: MssqlAdapter):
    """`varchar` alone does not say whether the column holds a code or a
    document, and sys.columns keeps the parts apart."""
    types = {c.name: c.native_type for c in adapter.get_schema("dbo.users")}

    assert types["id"] == "int"
    # nvarchar counts its length in bytes, two per character
    assert types["email"] == "nvarchar(255)"
    assert types["amount"] == "decimal(12,2)"


def test_a_max_length_type_says_max(wire):
    adapter = wire(
        MssqlAdapter,
        [
            CATALOG,
            (
                "FROM sys.columns",
                (
                    "name",
                    "type_name",
                    "max_length",
                    "precision",
                    "scale",
                    "nullable",
                    "comment",
                ),
                [("body", "varchar", -1, 0, 0, True, None)],
            ),
        ],
        database="shop",
    )

    assert adapter.get_schema("dbo.users")[0].native_type == "varchar(max)"


def test_get_schema_reports_keys(adapter: MssqlAdapter):
    columns = {c.name: c for c in adapter.get_schema("dbo.users")}

    assert columns["id"].is_pk is True
    assert columns["email"].is_pk is False


def test_get_schema_says_what_a_foreign_key_points_at(wire):
    adapter = wire(
        MssqlAdapter,
        [
            CATALOG,
            (
                "FROM sys.columns",
                (
                    "name",
                    "type_name",
                    "max_length",
                    "precision",
                    "scale",
                    "nullable",
                    "comment",
                ),
                [("user_id", "int", 4, 10, 0, True, None)],
            ),
            PRIMARY_KEYS,
            FOREIGN_KEYS,
        ],
        database="shop",
    )

    column = adapter.get_schema("dbo.orders")[0]

    assert column.is_fk is True
    assert column.references_container == "dbo.users"
    assert column.references_column == "id"


def test_the_object_is_named_to_the_server_the_way_it_was_quoted(
    adapter: MssqlAdapter,
):
    adapter.get_schema("dbo.users")

    assert adapter._conn.find("FROM sys.columns")[1] == ("[dbo].[users]",)


def test_a_bare_name_is_resolved_where_only_one_schema_has_it(adapter: MssqlAdapter):
    adapter.get_schema("orders")

    assert adapter._conn.find("FROM sys.columns")[1] == ("[dbo].[orders]",)


def test_a_bare_name_in_two_schemas_is_refused_rather_than_guessed(
    adapter: MssqlAdapter,
):
    with pytest.raises(UnknownContainerError, match="dbo.users, sales.users"):
        adapter.get_schema("users")


def test_get_schema_of_an_unknown_container(adapter: MssqlAdapter):
    with pytest.raises(UnknownContainerError, match="absent"):
        adapter.get_schema("absent")


# ---- sample and profile ----


def test_get_sample_uses_top_and_brackets(wire):
    adapter = wire(
        MssqlAdapter,
        [CATALOG, ("SELECT TOP", ("id", "email"), [(1, "a@x.com")])],
        database="shop",
    )

    rows = adapter.get_sample("dbo.users", limit=2)

    assert rows == [{"id": 1, "email": "a@x.com"}]
    statement, params = adapter._conn.find("SELECT TOP")
    assert statement == "SELECT TOP (?) * FROM [dbo].[users]"
    assert params == (2,)


def _profiled(wire, rule) -> MssqlAdapter:
    return wire(MssqlAdapter, [CATALOG, COLUMNS, rule], database="shop")


def test_profile_distinct_count(wire):
    adapter = _profiled(wire, ("COUNT(DISTINCT", ("n",), [(7,)]))

    result = adapter.profile_column("dbo.users", "email", ProfileMode.DISTINCT_COUNT)

    assert result.distinct_count == 7


def test_profile_null_ratio(wire):
    adapter = _profiled(wire, ("AVG(CASE", ("r",), [(0.25,)]))

    result = adapter.profile_column("dbo.users", "email", ProfileMode.NULL_RATIO)

    assert result.null_ratio == 0.25
    # `1.0` rather than `1`, or sql server averages integers into an integer
    assert (
        "WHEN [email] IS NULL THEN 1.0 ELSE 0.0" in (adapter._conn.find("AVG(CASE")[0])
    )


def test_profile_top_values_takes_the_top_rather_than_limiting(wire):
    adapter = _profiled(wire, ("GROUP BY", ("v", "c"), [("a@x.com", 2), (None, 1)]))

    result = adapter.profile_column("dbo.users", "email", ProfileMode.TOP_VALUES)

    assert result.top_values is not None
    assert [(v.value, v.count) for v in result.top_values] == [("a@x.com", 2), ("", 1)]
    statement, params = adapter._conn.find("GROUP BY")
    assert statement.startswith("SELECT TOP (?)")
    assert params == (20,)


def test_profile_min_max(wire):
    adapter = _profiled(wire, ("MIN(", ("lo", "hi"), [(1, 99)]))

    result = adapter.profile_column("dbo.users", "id", ProfileMode.MIN_MAX)

    assert (result.min_value, result.max_value) == ("1", "99")


def test_profile_of_an_unknown_column(adapter: MssqlAdapter):
    with pytest.raises(UnknownColumnError, match="dbo.users.age"):
        adapter.profile_column("dbo.users", "age", ProfileMode.NULL_RATIO)


def test_an_unsupported_mode_is_refused(adapter: MssqlAdapter):
    with pytest.raises(ValueError, match="unsupported profile mode"):
        adapter.profile_column("dbo.users", "email", "median")  # type: ignore[arg-type]


# ---- audit and liveness ----


def test_the_statement_reaching_the_source_is_recorded(wire):
    adapter = wire(
        MssqlAdapter,
        [CATALOG, ("SELECT TOP", ("id",), [(1,)])],
        database="shop",
    )
    adapter.pop_rendered_sql()

    adapter.get_sample("dbo.users", limit=2)

    assert "SELECT TOP (2) * FROM [dbo].[users]" in (adapter.pop_rendered_sql() or "")


def test_ping_after_close(adapter: MssqlAdapter):
    adapter.close()

    assert adapter.ping() is False


@pytest.fixture
def connected(monkeypatch, connection) -> Any:
    """`pyodbc.connect` replaced, so `from_connection` can be tested without a
    server."""

    class Recorder:
        args: tuple[Any, ...] = ()
        kwargs: dict[str, Any] = {}

    recorder = Recorder()

    def fake_connect(*args: Any, **kwargs: Any) -> Any:
        recorder.args = args
        recorder.kwargs = kwargs
        return connection()

    monkeypatch.setattr(mssql.pyodbc, "connect", fake_connect)
    return recorder
