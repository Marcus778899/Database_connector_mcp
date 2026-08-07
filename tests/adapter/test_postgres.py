from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from src.adapter import postgres
from src.adapter.base import UnknownColumnError, UnknownContainerError
from src.adapter.postgres import PostgresAdapter
from src.core.config import ConnectionInfo
from src.core.contracts import ContainerType, ProfileMode, SourceAdaptor

# public.users and sales.users both exist, which is the whole reason a container
# is named `schema.table` here.
CATALOG = (
    "n.nspname || '.' || c.relname AS name",
    ("name",),
    [("public.orders",), ("public.users",), ("sales.users",)],
)

CONTAINERS = (
    "obj_description",
    ("schema_name", "container_name", "kind", "estimated_count", "comment"),
    [
        ("public", "orders", "r", 120, None),
        ("public", "users", "r", 3, "people who signed up"),
        ("public", "recent_orders", "v", 0, None),
        ("sales", "users", "r", -1, None),
    ],
)

COLUMNS = (
    "format_type",
    ("name", "native_type", "nullable", "comment"),
    [
        ("id", "integer", False, None),
        ("email", "text", False, "login address"),
        ("signed_up", "timestamp with time zone", True, None),
    ],
)

PRIMARY_KEYS = ("contype = 'p'", ("name",), [("id",)])
FOREIGN_KEYS = (
    "contype = 'f'",
    ("name", "target", "target_column"),
    [("user_id", "public.users", "id")],
)

SCHEMA_RULES = [CATALOG, CONTAINERS, COLUMNS, PRIMARY_KEYS, FOREIGN_KEYS]


@pytest.fixture
def adapter(wire) -> PostgresAdapter:
    return wire(PostgresAdapter, SCHEMA_RULES, database="shop")


def _names(adapter: PostgresAdapter, **kwargs: Any) -> list[str]:
    return [c.container_name for c in adapter.list_containers(**kwargs).containers]


# ---- construction ----


def test_satisfies_source_adaptor_protocol(adapter: PostgresAdapter):
    checked: SourceAdaptor = adapter
    assert isinstance(checked, SourceAdaptor)


def test_postgres_serves_more_than_one_database():
    """Each is a connection of its own, which the pool builds on demand."""
    assert PostgresAdapter.SUPPORTS_MULTIPLE_DATABASES is True


def test_from_connection_without_a_host_or_uri_is_refused():
    with pytest.raises(ValueError, match="requires a <REF>_HOST"):
        PostgresAdapter.from_connection(ConnectionInfo(user="reader"))


def test_from_connection_passes_the_parts_to_the_driver(connected):
    PostgresAdapter.from_connection(
        ConnectionInfo(
            host="db.internal",
            port=6432,
            user="reader",
            password="s3cret",
            database="shop",
        )
    )

    assert connected.kwargs == {
        "host": "db.internal",
        "port": 6432,
        "user": "reader",
        "password": "s3cret",
        "dbname": "shop",
        "autocommit": True,
    }


def test_a_uri_is_handed_over_whole(connected):
    """It is libpq's own format, and taking it apart could only lose from it —
    a unix socket has no host to put in a field."""
    PostgresAdapter.from_connection(
        ConnectionInfo(uri="postgresql:///shop?host=/var/run/postgresql")
    )

    assert connected.args == ("postgresql:///shop?host=/var/run/postgresql",)


def test_the_connection_refuses_writes(adapter: PostgresAdapter):
    """The guarantee is the server's, not a convention the tools follow."""
    assert "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY" in (
        adapter._conn.statements
    )


def test_hardening_the_session_is_not_audited_as_a_tool_call(
    adapter: PostgresAdapter,
):
    assert adapter.pop_rendered_sql() is None


def test_an_unnamed_database_is_read_back_from_the_server(wire):
    """libpq defaults the database to the user's name; reporting an empty one
    would leave every container labelled with nothing."""
    adapter = wire(
        PostgresAdapter,
        [("current_database", ("name",), [("reader",)])],
        database="",
    )

    assert adapter._database == "reader"
    assert adapter.pop_rendered_sql() is None


# ---- catalog ----


def test_list_databases(wire):
    adapter = wire(
        PostgresAdapter,
        [("FROM pg_database", ("datname",), [("shop",), ("analytics",)])],
        database="shop",
    )

    assert adapter.list_databases() == ["shop", "analytics"]


def test_list_containers_names_a_container_by_schema_and_table(
    adapter: PostgresAdapter,
):
    assert _names(adapter) == [
        "public.orders",
        "public.users",
        "public.recent_orders",
        "sales.users",
    ]


def test_the_schema_is_reported_on_its_own_as_well(adapter: PostgresAdapter):
    containers = adapter.list_containers().containers

    assert [c.schema_name for c in containers] == [
        "public",
        "public",
        "public",
        "sales",
    ]
    assert all(c.database == "shop" for c in containers)


def test_a_materialised_view_is_still_a_view(wire):
    adapter = wire(
        PostgresAdapter,
        [
            (
                "obj_description",
                ("schema_name", "container_name", "kind", "estimated_count", "comment"),
                [
                    ("public", "t", "r", 1, None),
                    ("public", "part", "p", 2, None),
                    ("public", "remote", "f", 3, None),
                    ("public", "v", "v", 4, None),
                    ("public", "mv", "m", 5, None),
                ],
            )
        ],
        database="shop",
    )

    kinds = {
        c.container_name: c.container_type for c in adapter.list_containers().containers
    }

    assert kinds["public.t"] == ContainerType.TABLE
    assert kinds["public.part"] == ContainerType.TABLE
    assert kinds["public.remote"] == ContainerType.TABLE
    assert kinds["public.v"] == ContainerType.VIEW
    assert kinds["public.mv"] == ContainerType.VIEW


def test_the_row_estimate_is_reported_where_there_is_one(adapter: PostgresAdapter):
    counts = {
        c.container_name: c.estimated_count
        for c in adapter.list_containers().containers
    }

    assert counts["public.orders"] == 120
    # a view's reltuples is whatever the planner last guessed
    assert counts["public.recent_orders"] is None
    # -1 is postgres saying nobody has ever analysed this table
    assert counts["sales.users"] is None


def test_a_table_comment_is_reported(adapter: PostgresAdapter):
    comments = {
        c.container_name: c.native_description
        for c in adapter.list_containers().containers
    }

    assert comments["public.users"] == "people who signed up"
    assert comments["public.orders"] is None


def test_list_containers_pages_with_a_cursor(wire):
    rows = [("public", f"t{index}", "r", 0, None) for index in range(4)]
    adapter = wire(
        PostgresAdapter,
        [
            (
                "obj_description",
                ("schema_name", "container_name", "kind", "estimated_count", "comment"),
                rows,
            )
        ],
        database="shop",
    )

    page = adapter.list_containers(limit=3)

    # one row over the page tells the adapter more remain, and is not returned
    assert [c.container_name for c in page.containers] == [
        "public.t0",
        "public.t1",
        "public.t2",
    ]
    assert page.next_cursor == "public.t2"
    _, params = adapter._conn.find("obj_description")
    assert params[-1] == 4


def test_the_last_page_has_no_cursor(adapter: PostgresAdapter):
    assert adapter.list_containers(limit=100).next_cursor is None


def test_the_cursor_is_compared_the_way_the_page_is_ordered(adapter: PostgresAdapter):
    """A locale-aware ordering of two columns and a comparison of the joined
    name can disagree, and a walk then skips or repeats a table."""
    adapter.list_containers(cursor="public.orders")

    statement, params = adapter._conn.find("obj_description")

    assert "ORDER BY (n.nspname || '.' || c.relname) COLLATE \"C\"" in statement
    assert "(n.nspname || '.' || c.relname) COLLATE \"C\" > %s" in statement
    assert "public.orders" in params


def test_a_schema_narrows_the_listing(adapter: PostgresAdapter):
    adapter.list_containers(schema="sales")

    _, params = adapter._conn.find("obj_description")

    assert "sales" in params


def test_another_database_is_refused(adapter: PostgresAdapter):
    with pytest.raises(UnknownContainerError, match="serves 'shop'"):
        adapter.list_containers(database="other")


# ---- schema ----


def test_get_schema(adapter: PostgresAdapter):
    columns = adapter.get_schema("public.users")

    assert [c.name for c in columns] == ["id", "email", "signed_up"]
    assert [c.native_type for c in columns] == [
        "integer",
        "text",
        "timestamp with time zone",
    ]
    assert [c.nullable for c in columns] == [False, False, True]


def test_the_ordinals_have_no_gaps_in_them(adapter: PostgresAdapter):
    """postgres keeps a dropped column's attnum, and a gap would read as a
    column this failed to report."""
    assert [c.ordinal for c in adapter.get_schema("public.users")] == [1, 2, 3]


def test_get_schema_reports_keys(adapter: PostgresAdapter):
    columns = {c.name: c for c in adapter.get_schema("public.users")}

    assert columns["id"].is_pk is True
    assert columns["email"].is_pk is False


def test_get_schema_says_what_a_foreign_key_points_at(wire):
    adapter = wire(
        PostgresAdapter,
        [
            CATALOG,
            (
                "format_type",
                ("name", "native_type", "nullable", "comment"),
                [("user_id", "integer", True, None)],
            ),
            PRIMARY_KEYS,
            FOREIGN_KEYS,
        ],
        database="shop",
    )

    column = adapter.get_schema("public.orders")[0]

    assert column.is_fk is True
    # qualified, because that is the name the tools take
    assert column.references_container == "public.users"
    assert column.references_column == "id"


def test_a_column_comment_is_reported(adapter: PostgresAdapter):
    """Which is what makes a catalog readable by somebody who did not build it."""
    columns = {c.name: c for c in adapter.get_schema("public.users")}

    assert columns["email"].native_description == "login address"
    assert columns["id"].native_description is None


def test_get_schema_of_an_unknown_container(adapter: PostgresAdapter):
    with pytest.raises(UnknownContainerError, match="absent"):
        adapter.get_schema("absent")


def test_a_bare_name_is_resolved_where_only_one_schema_has_it(
    adapter: PostgresAdapter,
):
    """An agent relaying a name somebody said will not have the schema."""
    assert [c.name for c in adapter.get_schema("orders")] == [
        "id",
        "email",
        "signed_up",
    ]
    statement, params = adapter._conn.find("format_type")
    assert params == ('"public"."orders"',)


def test_a_bare_name_in_two_schemas_is_refused_rather_than_guessed(
    adapter: PostgresAdapter,
):
    with pytest.raises(UnknownContainerError, match="public.users, sales.users"):
        adapter.get_schema("users")


# ---- sample ----


def test_get_sample_quotes_each_part_of_the_name(wire):
    adapter = wire(
        PostgresAdapter,
        [CATALOG, ("SELECT * FROM", ("id", "email"), [(1, "a@x.com")])],
        database="shop",
    )

    rows = adapter.get_sample("public.users", limit=2)

    assert rows == [{"id": 1, "email": "a@x.com"}]
    statement, params = adapter._conn.find("SELECT * FROM")
    assert statement == 'SELECT * FROM "public"."users" LIMIT %s'
    assert params == (2,)


def test_get_sample_caps_the_limit(wire):
    adapter = wire(
        PostgresAdapter,
        [CATALOG, ("SELECT * FROM", ("id",), [])],
        database="shop",
        max_sample_limit=5,
    )

    adapter.get_sample("public.users", limit=100)

    assert adapter._conn.find("SELECT * FROM")[1] == (5,)


def test_get_sample_serialises_what_json_cannot_hold(wire):
    adapter = wire(
        PostgresAdapter,
        [
            CATALOG,
            (
                "SELECT * FROM",
                ("amount", "on", "note"),
                [(Decimal("9.50"), date(2024, 1, 2), b"\x00\x01")],
            ),
        ],
        database="shop",
    )

    assert adapter.get_sample("public.users") == [
        {"amount": "9.50", "on": "2024-01-02", "note": "AAE="}
    ]


# ---- profile ----


def _profiled(wire, rule) -> PostgresAdapter:
    return wire(PostgresAdapter, [CATALOG, COLUMNS, rule], database="shop")


def test_profile_distinct_count(wire):
    adapter = _profiled(wire, ("COUNT(DISTINCT", ("n",), [(7,)]))

    result = adapter.profile_column("public.users", "email", ProfileMode.DISTINCT_COUNT)

    assert result.distinct_count == 7
    assert (
        'COUNT(DISTINCT "email") AS n FROM "public"."users"'
        in (adapter._conn.find("COUNT(DISTINCT")[0])
    )


def test_profile_null_ratio(wire):
    """postgres answers AVG over numeric with a Decimal, which is not a float."""
    adapter = _profiled(wire, ("AVG(CASE", ("r",), [(Decimal("0.25"),)]))

    assert (
        adapter.profile_column(
            "public.users", "email", ProfileMode.NULL_RATIO
        ).null_ratio
        == 0.25
    )


def test_profile_of_an_empty_table_has_no_ratio(wire):
    """AVG over no rows is NULL, which is not 0.0."""
    adapter = _profiled(wire, ("AVG(CASE", ("r",), [(None,)]))

    assert (
        adapter.profile_column(
            "public.users", "email", ProfileMode.NULL_RATIO
        ).null_ratio
        is None
    )


def test_profile_min_max_reports_a_date_as_itself(wire):
    adapter = _profiled(
        wire, ("MIN(", ("lo", "hi"), [(date(2024, 1, 2), date(2024, 3, 4))])
    )

    result = adapter.profile_column("public.users", "signed_up", ProfileMode.MIN_MAX)

    assert (result.min_value, result.max_value) == ("2024-01-02", "2024-03-04")


def test_profile_top_values(wire):
    adapter = _profiled(wire, ("GROUP BY", ("v", "c"), [("a@x.com", 2), (None, 1)]))

    result = adapter.profile_column("public.users", "email", ProfileMode.TOP_VALUES)

    assert result.top_values is not None
    assert [(v.value, v.count) for v in result.top_values] == [("a@x.com", 2), ("", 1)]


def test_profile_of_an_unknown_column(adapter: PostgresAdapter):
    with pytest.raises(UnknownColumnError, match="public.users.age"):
        adapter.profile_column("public.users", "age", ProfileMode.NULL_RATIO)


def test_profile_of_an_unknown_container(adapter: PostgresAdapter):
    with pytest.raises(UnknownContainerError):
        adapter.profile_column("absent", "id", ProfileMode.NULL_RATIO)


def test_an_unsupported_mode_is_refused(adapter: PostgresAdapter):
    with pytest.raises(ValueError, match="unsupported profile mode"):
        adapter.profile_column("public.users", "email", "median")  # type: ignore[arg-type]


# ---- audit and liveness ----


def test_the_statement_reaching_the_source_is_recorded(wire):
    adapter = wire(
        PostgresAdapter,
        [CATALOG, ("SELECT * FROM", ("id",), [(1,)])],
        database="shop",
    )
    adapter.pop_rendered_sql()

    adapter.get_sample("public.users", limit=2)

    rendered = adapter.pop_rendered_sql() or ""
    assert 'SELECT * FROM "public"."users" LIMIT 2' in rendered
    assert adapter.pop_rendered_sql() is None


def test_the_catalog_walk_reads_names_and_nothing_else(adapter: PostgresAdapter):
    """The inherited walk pages through list_containers, reading a comment and a
    row estimate for every container on the way."""
    adapter._known_containers()

    assert not any("obj_description" in s for s in adapter._conn.statements)


def test_ping(adapter: PostgresAdapter):
    assert adapter.ping() is True


def test_ping_does_not_pollute_the_audit_trail(adapter: PostgresAdapter):
    adapter.pop_rendered_sql()
    adapter.ping()

    assert adapter.pop_rendered_sql() is None


def test_ping_after_close(adapter: PostgresAdapter):
    """The pool rebuilds an adapter that fails its ping."""
    adapter.close()

    assert adapter.ping() is False
    assert adapter._conn.closed is True


def test_close_is_idempotent(adapter: PostgresAdapter):
    adapter.close()
    adapter.close()


@pytest.fixture
def connected(monkeypatch, connection) -> Any:
    """`psycopg.connect` replaced, so `from_connection` can be tested without a
    server. Records what the driver was handed."""

    class Recorder:
        args: tuple[Any, ...] = ()
        kwargs: dict[str, Any] = {}

    recorder = Recorder()

    def fake_connect(*args: Any, **kwargs: Any) -> Any:
        recorder.args = args
        recorder.kwargs = kwargs
        return connection()

    monkeypatch.setattr(postgres.psycopg, "connect", fake_connect)
    return recorder


# ---- which database this is actually on ----


def test_a_requested_database_overrides_the_one_the_url_names(connected):
    """The pool builds one adapter per database off the same `<REF>_URI`. Without
    the override every one of them connects to the url's own database and serves
    its catalog under a different name."""
    PostgresAdapter.from_connection(
        ConnectionInfo(uri="postgresql://db.internal/shop"), database="analytics"
    )

    assert connected.args == ("postgresql://db.internal/shop",)
    assert connected.kwargs == {"autocommit": True, "dbname": "analytics"}


def test_a_url_with_no_database_asked_for_is_left_alone(connected):
    PostgresAdapter.from_connection(ConnectionInfo(uri="postgresql://db.internal/shop"))

    assert connected.kwargs == {"autocommit": True}


def test_the_database_is_checked_rather_than_trusted(wire):
    """Every container is labelled with this name, so a connection that quietly
    went elsewhere would file one database's catalog under another's."""
    with pytest.raises(ValueError, match="but the connection is on 'shop'"):
        wire(
            PostgresAdapter,
            [("current_database", ("name",), [("shop",)])],
            database="analytics",
        )


def test_the_database_agreeing_is_not_an_error(wire):
    adapter = wire(
        PostgresAdapter,
        [("current_database", ("name",), [("shop",)])],
        database="shop",
    )

    assert adapter._database == "shop"
