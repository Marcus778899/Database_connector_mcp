from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from src.adapter import mysql
from src.adapter.base import UnknownColumnError, UnknownContainerError
from src.adapter.mysql import MysqlAdapter
from src.core.config import ConnectionInfo
from src.core.contracts import ContainerType, ProfileMode, SourceAdaptor

CATALOG = (
    "SELECT TABLE_NAME AS name FROM information_schema.TABLES",
    ("name",),
    [("orders",), ("recent_orders",), ("users",)],
)

CONTAINERS = (
    "TABLE_ROWS AS estimated_count",
    ("name", "kind", "estimated_count", "comment", "updated_at"),
    [
        ("orders", "BASE TABLE", 120, "", datetime(2024, 5, 1, 9, 30)),
        ("recent_orders", "VIEW", None, "VIEW", None),
        ("users", "BASE TABLE", 3, "people who signed up", None),
    ],
)

COLUMNS = (
    "FROM information_schema.COLUMNS",
    ("name", "native_type", "nullable", "column_key", "comment"),
    [
        ("id", "int unsigned", "NO", "PRI", ""),
        ("email", "varchar(255)", "NO", "UNI", "login address"),
        ("note", "text", "YES", "", ""),
    ],
)

FOREIGN_KEYS = (
    "KEY_COLUMN_USAGE",
    ("name", "target_schema", "target", "target_column"),
    [("user_id", "shop", "users", "id")],
)

RULES = [CATALOG, CONTAINERS, COLUMNS, FOREIGN_KEYS]


@pytest.fixture
def adapter(wire) -> MysqlAdapter:
    return wire(MysqlAdapter, RULES, database="shop")


# ---- construction ----


def test_satisfies_source_adaptor_protocol(adapter: MysqlAdapter):
    checked: SourceAdaptor = adapter
    assert isinstance(checked, SourceAdaptor)


def test_mysql_serves_more_than_one_database():
    assert MysqlAdapter.SUPPORTS_MULTIPLE_DATABASES is True


def test_from_connection_without_a_host_is_refused():
    with pytest.raises(ValueError, match="requires a <REF>_HOST"):
        MysqlAdapter.from_connection(ConnectionInfo(user="reader"))


def test_a_connection_with_no_database_is_refused(connected):
    """`SELECT DATABASE()` would answer NULL, and every catalog query after it
    would come back empty rather than wrong-looking."""
    with pytest.raises(ValueError, match="requires a <REF>_DB"):
        MysqlAdapter.from_connection(ConnectionInfo(host="db.internal"))


def test_from_connection_passes_the_parts_to_the_driver(connected):
    MysqlAdapter.from_connection(
        ConnectionInfo(
            host="db.internal",
            port=3307,
            user="reader",
            password="s3cret",
            database="shop",
        )
    )

    assert connected.kwargs["host"] == "db.internal"
    assert connected.kwargs["port"] == 3307
    assert connected.kwargs["user"] == "reader"
    assert connected.kwargs["password"] == "s3cret"
    assert connected.kwargs["database"] == "shop"
    # mysql's "utf8" is three bytes wide and drops the rest of unicode
    assert connected.kwargs["charset"] == "utf8mb4"


def test_a_uri_is_taken_apart_because_the_driver_takes_arguments(connected):
    MysqlAdapter.from_connection(
        ConnectionInfo(uri="mysql://reader:p%40ss@db.internal:3307/shop")
    )

    assert connected.kwargs["host"] == "db.internal"
    assert connected.kwargs["port"] == 3307
    assert connected.kwargs["user"] == "reader"
    # percent-encoded, because a password is where an `@` turns up
    assert connected.kwargs["password"] == "p@ss"
    assert connected.kwargs["database"] == "shop"


def test_an_explicit_database_wins_over_the_uri(connected):
    MysqlAdapter.from_connection(
        ConnectionInfo(uri="mysql://db.internal/shop"), database="analytics"
    )

    assert connected.kwargs["database"] == "analytics"


def test_the_connection_refuses_writes(adapter: MysqlAdapter):
    assert "SET SESSION TRANSACTION READ ONLY" in adapter._conn.statements
    # the adapter's own bookkeeping is not a statement a tool call asked for
    assert adapter.pop_rendered_sql() is None


# ---- catalog ----


def test_list_databases_leaves_out_the_servers_own(wire):
    adapter = wire(
        MysqlAdapter,
        [("information_schema.SCHEMATA", ("name",), [("shop",), ("analytics",)])],
        database="shop",
    )

    assert adapter.list_databases() == ["shop", "analytics"]
    _, params = adapter._conn.find("SCHEMATA")
    assert "performance_schema" in params


def test_list_containers(adapter: MysqlAdapter):
    containers = adapter.list_containers().containers

    assert [c.container_name for c in containers] == [
        "orders",
        "recent_orders",
        "users",
    ]
    # mysql has no schema layer below the database
    assert all(c.schema_name is None for c in containers)
    assert all(c.database == "shop" for c in containers)


def test_the_container_type_is_reported(adapter: MysqlAdapter):
    kinds = {
        c.container_name: c.container_type for c in adapter.list_containers().containers
    }

    assert kinds["users"] == ContainerType.TABLE
    assert kinds["recent_orders"] == ContainerType.VIEW


def test_the_row_estimate_is_reported_where_there_is_one(adapter: MysqlAdapter):
    counts = {
        c.container_name: c.estimated_count
        for c in adapter.list_containers().containers
    }

    assert counts["orders"] == 120
    assert counts["recent_orders"] is None


def test_the_word_view_is_not_a_description_of_anything(adapter: MysqlAdapter):
    """mysql fills a view's TABLE_COMMENT with the word VIEW, which is the type
    `container_type` already carries."""
    comments = {
        c.container_name: c.native_description
        for c in adapter.list_containers().containers
    }

    assert comments["users"] == "people who signed up"
    assert comments["recent_orders"] is None
    assert comments["orders"] is None


def test_freshness_is_reported_where_mysql_tracks_it(adapter: MysqlAdapter):
    stamps = {
        c.container_name: c.last_modified_at
        for c in adapter.list_containers().containers
    }

    assert stamps["orders"] == "2024-05-01T09:30:00"
    assert stamps["users"] is None


def test_list_containers_pages_with_a_cursor(adapter: MysqlAdapter):
    page = adapter.list_containers(limit=2)

    assert [c.container_name for c in page.containers] == ["orders", "recent_orders"]
    assert page.next_cursor == "recent_orders"


def test_paging_compares_the_way_it_orders(adapter: MysqlAdapter):
    """information_schema's collation reads `Orders` and `orders` as one name,
    which is enough for a walk to skip a table."""
    adapter.list_containers(cursor="orders")

    statement, params = adapter._conn.find("TABLE_ROWS AS estimated_count")

    assert "AND CAST(TABLE_NAME AS BINARY) > %s" in statement
    assert "ORDER BY CAST(TABLE_NAME AS BINARY) LIMIT %s" in statement
    assert params == ("shop", "orders", 101)


def test_another_database_is_refused(adapter: MysqlAdapter):
    with pytest.raises(UnknownContainerError, match="serves 'shop'"):
        adapter.list_containers(database="other")


def test_a_schema_that_is_not_the_database_is_refused(adapter: MysqlAdapter):
    """mysql's SCHEMA is a synonym for DATABASE; silently ignoring the argument
    would mislead."""
    with pytest.raises(UnknownContainerError, match="schema is its database"):
        adapter.list_containers(schema="public")

    assert adapter.list_containers(schema="shop").containers


# ---- schema ----


def test_get_schema(adapter: MysqlAdapter):
    columns = adapter.get_schema("users")

    assert [c.name for c in columns] == ["id", "email", "note"]
    assert [c.ordinal for c in columns] == [1, 2, 3]
    # COLUMN_TYPE, not DATA_TYPE: `int unsigned` says what `int` does not
    assert [c.native_type for c in columns] == ["int unsigned", "varchar(255)", "text"]
    assert [c.nullable for c in columns] == [False, False, True]


def test_get_schema_reports_keys(adapter: MysqlAdapter):
    columns = {c.name: c for c in adapter.get_schema("users")}

    assert columns["id"].is_pk is True
    # a unique key is not a primary one
    assert columns["email"].is_pk is False


def test_a_column_comment_is_reported(adapter: MysqlAdapter):
    columns = {c.name: c for c in adapter.get_schema("users")}

    assert columns["email"].native_description == "login address"
    assert columns["note"].native_description is None


def test_get_schema_says_what_a_foreign_key_points_at(wire):
    adapter = wire(
        MysqlAdapter,
        [
            CATALOG,
            (
                "FROM information_schema.COLUMNS",
                ("name", "native_type", "nullable", "column_key", "comment"),
                [("user_id", "int", "YES", "MUL", "")],
            ),
            FOREIGN_KEYS,
        ],
        database="shop",
    )

    column = adapter.get_schema("orders")[0]

    assert column.is_fk is True
    assert column.references_container == "users"
    assert column.references_column == "id"


def test_a_reference_to_another_database_is_qualified(wire):
    """A bare name would read as a table in this one."""
    adapter = wire(
        MysqlAdapter,
        [
            CATALOG,
            (
                "FROM information_schema.COLUMNS",
                ("name", "native_type", "nullable", "column_key", "comment"),
                [("user_id", "int", "YES", "MUL", "")],
            ),
            (
                "KEY_COLUMN_USAGE",
                ("name", "target_schema", "target", "target_column"),
                [("user_id", "crm", "people", "id")],
            ),
        ],
        database="shop",
    )

    assert adapter.get_schema("orders")[0].references_container == "crm.people"


def test_get_schema_of_an_unknown_container(adapter: MysqlAdapter):
    with pytest.raises(UnknownContainerError, match="absent"):
        adapter.get_schema("absent")


# ---- sample and profile ----


def test_get_sample_quotes_with_backticks(wire):
    adapter = wire(
        MysqlAdapter,
        [CATALOG, ("SELECT * FROM", ("id", "amount"), [(1, Decimal("9.50"))])],
        database="shop",
    )

    rows = adapter.get_sample("users", limit=2)

    assert rows == [{"id": 1, "amount": "9.50"}]
    statement, params = adapter._conn.find("SELECT * FROM")
    assert statement == "SELECT * FROM `users` LIMIT %s"
    assert params == (2,)


def test_get_sample_caps_the_limit(wire):
    adapter = wire(
        MysqlAdapter,
        [CATALOG, ("SELECT * FROM", ("id",), [])],
        database="shop",
        max_sample_limit=5,
    )

    adapter.get_sample("users", limit=100)

    assert adapter._conn.find("SELECT * FROM")[1] == (5,)


def _profiled(wire, rule) -> MysqlAdapter:
    return wire(MysqlAdapter, [CATALOG, COLUMNS, rule], database="shop")


def test_profile_distinct_count(wire):
    adapter = _profiled(wire, ("COUNT(DISTINCT", ("n",), [(7,)]))

    result = adapter.profile_column("users", "email", ProfileMode.DISTINCT_COUNT)

    assert result.distinct_count == 7
    assert (
        "COUNT(DISTINCT `email`) AS n FROM `users`"
        in (adapter._conn.find("COUNT(DISTINCT")[0])
    )


def test_profile_null_ratio(wire):
    adapter = _profiled(wire, ("AVG(CASE", ("r",), [(Decimal("0.25"),)]))

    assert (
        adapter.profile_column("users", "email", ProfileMode.NULL_RATIO).null_ratio
        == 0.25
    )


def test_profile_of_an_empty_table_has_no_ratio(wire):
    adapter = _profiled(wire, ("AVG(CASE", ("r",), [(None,)]))

    assert (
        adapter.profile_column("users", "email", ProfileMode.NULL_RATIO).null_ratio
        is None
    )


def test_profile_min_max(wire):
    adapter = _profiled(wire, ("MIN(", ("lo", "hi"), [(1, 99)]))

    result = adapter.profile_column("users", "id", ProfileMode.MIN_MAX)

    assert (result.min_value, result.max_value) == ("1", "99")


def test_profile_top_values(wire):
    adapter = _profiled(wire, ("GROUP BY", ("v", "c"), [("a@x.com", 2), (None, 1)]))

    result = adapter.profile_column("users", "email", ProfileMode.TOP_VALUES)

    assert result.top_values is not None
    assert [(v.value, v.count) for v in result.top_values] == [("a@x.com", 2), ("", 1)]


def test_profile_of_an_unknown_column(adapter: MysqlAdapter):
    with pytest.raises(UnknownColumnError, match="users.age"):
        adapter.profile_column("users", "age", ProfileMode.NULL_RATIO)


def test_an_unsupported_mode_is_refused(adapter: MysqlAdapter):
    with pytest.raises(ValueError, match="unsupported profile mode"):
        adapter.profile_column("users", "email", "median")  # type: ignore[arg-type]


# ---- audit and liveness ----


def test_the_statement_reaching_the_source_is_recorded(wire):
    adapter = wire(
        MysqlAdapter,
        [CATALOG, ("SELECT * FROM", ("id",), [(1,)])],
        database="shop",
    )
    adapter.pop_rendered_sql()

    adapter.get_sample("users", limit=2)

    assert "SELECT * FROM `users` LIMIT 2" in (adapter.pop_rendered_sql() or "")


def test_the_catalog_walk_reads_names_and_nothing_else(adapter: MysqlAdapter):
    adapter._known_containers()

    assert not any("TABLE_ROWS" in s for s in adapter._conn.statements)


def test_ping_after_close(adapter: MysqlAdapter):
    adapter.close()

    assert adapter.ping() is False


@pytest.fixture
def connected(monkeypatch, connection) -> Any:
    """`pymysql.connect` replaced, so `from_connection` can be tested without a
    server."""

    class Recorder:
        kwargs: dict[str, Any] = {}

    recorder = Recorder()

    def fake_connect(**kwargs: Any) -> Any:
        recorder.kwargs = kwargs
        return connection()

    monkeypatch.setattr(mysql.pymysql, "connect", fake_connect)
    return recorder
