import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.adapter.base import UnknownColumnError, UnknownContainerError
from src.adapter.sqlite import SqliteAdapter, _render
from src.core.config import ConnectionInfo
from src.core.contracts import ContainerType, ProfileMode

_SCHEMA = """
CREATE TABLE users (
    id    INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    note  BLOB
);
CREATE TABLE orders (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    amount  REAL
);
CREATE TABLE blank (id INTEGER PRIMARY KEY, thing TEXT);
-- AUTOINCREMENT makes sqlite create its own sqlite_sequence table
CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT);
CREATE VIEW recent_orders AS SELECT * FROM orders;
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO users (email, note) VALUES (?,?)",
        [("a@x.com", b"\x00\x01"), ("b@x.com", None), ("a@x.com", None)],
    )
    conn.executemany(
        "INSERT INTO orders (user_id, amount) VALUES (?,?)", [(1, 9.5), (2, 3.0)]
    )
    conn.execute("INSERT INTO tickets (code) VALUES ('t-1')")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def adapter(db: Path) -> Iterator[SqliteAdapter]:
    built = SqliteAdapter(db)
    yield built
    built.close()


def _names(adapter: SqliteAdapter, **kwargs) -> list[str]:
    return [c.container_name for c in adapter.list_containers(**kwargs).containers]


# ---- construction ----


def test_from_connection_takes_a_path(db: Path):
    adapter = SqliteAdapter.from_connection(ConnectionInfo(path=str(db)))
    try:
        assert adapter.ping() is True
    finally:
        adapter.close()


def test_from_connection_falls_back_to_the_uri(db: Path):
    adapter = SqliteAdapter.from_connection(ConnectionInfo(uri=str(db)))
    try:
        assert adapter.list_databases() == ["main"]
    finally:
        adapter.close()


def test_from_connection_labels_the_database(db: Path):
    adapter = SqliteAdapter.from_connection(
        ConnectionInfo(path=str(db), database="shop")
    )
    try:
        assert adapter.list_databases() == ["shop"]
    finally:
        adapter.close()


def test_from_connection_without_a_location_is_refused():
    with pytest.raises(ValueError, match="requires a <REF>_PATH"):
        SqliteAdapter.from_connection(ConnectionInfo(host="localhost"))


def test_a_missing_file_is_refused(tmp_path: Path):
    """mode=ro would report `unable to open database file`; say what is wrong."""
    with pytest.raises(ValueError, match="no sqlite database at"):
        SqliteAdapter(tmp_path / "absent.db")


def test_one_file_is_one_database():
    assert SqliteAdapter.SUPPORTS_MULTIPLE_DATABASES is False


def test_the_connection_refuses_writes(adapter: SqliteAdapter):
    """The guarantee is the driver's, not a convention the tools follow."""
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        adapter._conn.execute("CREATE TABLE sneaky (x)")


def test_read_write_is_opt_in(db: Path):
    adapter = SqliteAdapter(db, read_only=False)
    try:
        adapter._conn.execute("CREATE TABLE allowed (x)")
    finally:
        adapter.close()


# ---- catalog ----


def test_list_containers_lists_tables_and_views(adapter: SqliteAdapter):
    assert _names(adapter) == ["blank", "orders", "recent_orders", "tickets", "users"]


def test_sqlite_internal_tables_are_hidden(adapter: SqliteAdapter):
    """`sqlite_%` needs the LIKE escape: bare `_` is a single-char wildcard."""
    assert "sqlite_sequence" not in _names(adapter)


def test_the_container_type_is_reported(adapter: SqliteAdapter):
    kinds = {
        c.container_name: c.container_type for c in adapter.list_containers().containers
    }

    assert kinds["users"] == ContainerType.TABLE
    assert kinds["recent_orders"] == ContainerType.VIEW


def test_only_tables_are_counted(adapter: SqliteAdapter):
    """Counting a view would run it."""
    counts = {
        c.container_name: c.estimated_count
        for c in adapter.list_containers().containers
    }

    assert counts["users"] == 3
    assert counts["orders"] == 2
    assert counts["blank"] == 0
    assert counts["recent_orders"] is None


def test_list_containers_pages_with_a_cursor(adapter: SqliteAdapter):
    first = adapter.list_containers(limit=2)

    assert [c.container_name for c in first.containers] == ["blank", "orders"]
    assert first.next_cursor == "orders"
    assert _names(adapter, limit=2, cursor=first.next_cursor) == [
        "recent_orders",
        "tickets",
    ]


def test_the_last_page_has_no_cursor(adapter: SqliteAdapter):
    assert adapter.list_containers(limit=2, cursor="tickets").next_cursor is None


def test_paging_reaches_every_container(adapter: SqliteAdapter):
    seen: list[str] = []
    cursor = None
    while True:
        page = adapter.list_containers(limit=2, cursor=cursor)
        seen.extend(c.container_name for c in page.containers)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert seen == ["blank", "orders", "recent_orders", "tickets", "users"]


def test_another_database_is_refused(adapter: SqliteAdapter):
    with pytest.raises(UnknownContainerError, match="unknown database"):
        adapter.list_containers(database="other")


def test_the_configured_database_is_accepted(adapter: SqliteAdapter):
    assert _names(adapter, database="main") == _names(adapter)


def test_a_schema_argument_is_refused(adapter: SqliteAdapter):
    """sqlite has no schema layer; silently ignoring it would mislead."""
    with pytest.raises(UnknownContainerError, match="no schema layer"):
        adapter.list_containers(schema="public")


# ---- schema ----


def test_get_schema(adapter: SqliteAdapter):
    columns = adapter.get_schema("users")

    assert [c.name for c in columns] == ["id", "email", "note"]
    # PRAGMA counts from zero, the contract from one
    assert [c.ordinal for c in columns] == [1, 2, 3]
    assert [c.native_type for c in columns] == ["INTEGER", "TEXT", "BLOB"]


def test_get_schema_reports_keys_and_nullability(adapter: SqliteAdapter):
    columns = {c.name: c for c in adapter.get_schema("users")}

    assert columns["id"].is_pk is True
    assert columns["email"].is_pk is False
    assert columns["email"].nullable is False  # NOT NULL
    assert columns["note"].nullable is True


def test_get_schema_marks_a_foreign_key(adapter: SqliteAdapter):
    columns = {c.name: c for c in adapter.get_schema("orders")}

    assert columns["user_id"].is_fk is True
    assert columns["amount"].is_fk is False


def test_a_column_with_no_declared_type_reports_no_type(db: Path):
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE loose (whatever)")
    conn.commit()
    conn.close()

    adapter = SqliteAdapter(db)
    try:
        assert adapter.get_schema("loose")[0].native_type == ""
    finally:
        adapter.close()


def test_a_view_can_be_read(adapter: SqliteAdapter):
    assert [c.name for c in adapter.get_schema("recent_orders")] == [
        "id",
        "user_id",
        "amount",
    ]
    assert len(adapter.get_sample("recent_orders", limit=5)) == 2


def test_get_schema_of_an_unknown_container(adapter: SqliteAdapter):
    with pytest.raises(UnknownContainerError, match="absent"):
        adapter.get_schema("absent")


# ---- sample ----


def test_get_sample(adapter: SqliteAdapter):
    rows = adapter.get_sample("orders", limit=2)

    assert rows == [
        {"id": 1, "user_id": 1, "amount": 9.5},
        {"id": 2, "user_id": 2, "amount": 3.0},
    ]


def test_get_sample_serialises_a_blob(adapter: SqliteAdapter):
    """str(b"x") is not round-trippable, so bytes come back base64."""
    assert adapter.get_sample("users", limit=1)[0]["note"] == "AAE="


def test_get_sample_caps_the_limit(db: Path):
    adapter = SqliteAdapter(db, max_sample_limit=1)
    try:
        assert len(adapter.get_sample("users", limit=100)) == 1
    finally:
        adapter.close()


def test_get_sample_of_an_unknown_container(adapter: SqliteAdapter):
    with pytest.raises(UnknownContainerError):
        adapter.get_sample("absent")


# ---- profile ----


def test_profile_distinct_count(adapter: SqliteAdapter):
    result = adapter.profile_column("users", "email", ProfileMode.DISTINCT_COUNT)

    assert result.distinct_count == 2
    # every statement is a full scan of a local file
    assert result.approximate is False


def test_profile_top_values(adapter: SqliteAdapter):
    result = adapter.profile_column("users", "email", ProfileMode.TOP_VALUES)

    assert result.top_values is not None
    assert [(v.value, v.count) for v in result.top_values] == [
        ("a@x.com", 2),
        ("b@x.com", 1),
    ]


def test_profile_null_ratio(adapter: SqliteAdapter):
    assert adapter.profile_column(
        "users", "note", ProfileMode.NULL_RATIO
    ).null_ratio == (pytest.approx(2 / 3))


def test_profile_min_max(adapter: SqliteAdapter):
    result = adapter.profile_column("orders", "amount", ProfileMode.MIN_MAX)

    assert (result.min_value, result.max_value) == ("3.0", "9.5")


def test_profile_of_an_empty_table(adapter: SqliteAdapter):
    """AVG over no rows is NULL: there is no ratio to report, which is not 0.0."""
    assert (
        adapter.profile_column("blank", "thing", ProfileMode.NULL_RATIO).null_ratio
        is None
    )

    min_max = adapter.profile_column("blank", "thing", ProfileMode.MIN_MAX)
    assert (min_max.min_value, min_max.max_value) == (None, None)
    assert (
        adapter.profile_column(
            "blank", "thing", ProfileMode.DISTINCT_COUNT
        ).distinct_count
        == 0
    )


def test_profile_of_an_unknown_column(adapter: SqliteAdapter):
    with pytest.raises(UnknownColumnError, match="users.age"):
        adapter.profile_column("users", "age", ProfileMode.NULL_RATIO)


def test_profile_of_an_unknown_container(adapter: SqliteAdapter):
    with pytest.raises(UnknownContainerError):
        adapter.profile_column("absent", "id", ProfileMode.NULL_RATIO)


def test_an_unsupported_mode_is_refused(adapter: SqliteAdapter):
    with pytest.raises(ValueError, match="unsupported profile mode"):
        adapter.profile_column("users", "email", "median")  # type: ignore[arg-type]


def test_every_profile_mode_is_implemented(adapter: SqliteAdapter):
    for mode in ProfileMode:
        assert adapter.profile_column("orders", "amount", mode) is not None


# ---- audit and liveness ----


def test_the_statement_reaching_the_source_is_recorded(adapter: SqliteAdapter):
    adapter.pop_rendered_sql()
    adapter.get_sample("orders", limit=2)

    rendered = adapter.pop_rendered_sql()

    assert rendered is not None
    assert 'SELECT * FROM "orders" LIMIT 2' in rendered
    assert adapter.pop_rendered_sql() is None


def test_a_parameter_is_inlined_for_the_audit(adapter: SqliteAdapter):
    adapter.pop_rendered_sql()
    adapter.list_containers(limit=1, cursor="orders")

    assert "'orders'" in (adapter.pop_rendered_sql() or "")


def test_a_value_holding_a_placeholder_does_not_shift_the_rest():
    """
    The bug this guards: repeated `replace('?', ...)` would find the `?` inside
    the value it had just inlined and substitute there, so the audit line came
    out wrong about what ran while still reading as authoritative.
    """
    rendered = _render("SELECT * FROM t WHERE name > ? LIMIT ?", ("a?b", 5))

    assert rendered == "SELECT * FROM t WHERE name > 'a?b' LIMIT 5"


def test_a_cursor_holding_a_placeholder_is_audited_intact(adapter: SqliteAdapter):
    """`cursor` comes from the caller, so this path is reachable."""
    adapter.pop_rendered_sql()
    adapter.list_containers(limit=1, cursor="a?b")

    rendered = adapter.pop_rendered_sql() or ""

    assert "'a?b'" in rendered
    assert "LIMIT 2" in rendered


@pytest.mark.parametrize(
    ("sql", "params", "expected"),
    [
        ("SELECT 1", (), "SELECT 1"),
        # an unfilled placeholder stays one rather than vanishing
        ("SELECT ?, ?", (1,), "SELECT 1, ?"),
        ("SELECT ?", (1, 2), "SELECT 1"),
        ("SELECT ?", (None,), "SELECT None"),
    ],
)
def test_render_handles_a_mismatch_between_placeholders_and_parameters(
    sql: str, params: tuple, expected: str
):
    assert _render(sql, params) == expected


def test_ping(adapter: SqliteAdapter):
    assert adapter.ping() is True


def test_ping_does_not_pollute_the_audit_trail(adapter: SqliteAdapter):
    adapter.pop_rendered_sql()
    adapter.ping()

    assert adapter.pop_rendered_sql() is None


def test_ping_after_close(adapter: SqliteAdapter):
    """The pool rebuilds an adapter that fails its ping."""
    adapter.close()

    assert adapter.ping() is False


# ---- catalog walk ----


def test_the_cheap_walk_does_not_count_rows(adapter: SqliteAdapter):
    """The inherited walk pages through list_containers, counting every table."""
    adapter.pop_rendered_sql()

    assert adapter._walk_containers() == {
        "blank",
        "orders",
        "recent_orders",
        "tickets",
        "users",
    }
    assert "COUNT(*)" not in (adapter.pop_rendered_sql() or "")


def test_a_name_the_identifier_policy_rejects_still_lists(db: Path):
    """A hyphenated table is legal in sqlite. It cannot be counted, but hiding it
    would be worse than reporting an unknown count."""
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE "odd-name" (x)')
    conn.commit()
    conn.close()

    adapter = SqliteAdapter(db)
    try:
        counts = {
            c.container_name: c.estimated_count
            for c in adapter.list_containers().containers
        }
        assert "odd-name" in counts
        assert counts["odd-name"] is None
    finally:
        adapter.close()
