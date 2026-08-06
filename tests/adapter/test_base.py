from typing import Any

import pytest

from src.adapter.base import (
    AdapterBase,
    SqlAdapterBase,
    UnknownColumnError,
    UnknownContainerError,
)
from src.core.contracts import (
    ColumnInfo,
    ContainerInfo,
    ContainerPage,
    ContainerType,
    ProfileMode,
)

USERS_COLUMNS = [
    ColumnInfo(
        name="id",
        ordinal=1,
        native_type="INTEGER",
        nullable=False,
        is_pk=True,
        is_fk=False,
    ),
    ColumnInfo(
        name="name",
        ordinal=2,
        native_type="TEXT",
        nullable=True,
        is_pk=False,
        is_fk=False,
    ),
]


class _ContractMixin(AdapterBase):
    def list_containers(
        self,
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage:
        names = ["orders", "users"]
        if cursor is not None:
            names = [n for n in names if n > cursor]
        page = names[: self._cap_page_size(limit)]
        return ContainerPage(
            containers=[
                ContainerInfo(
                    database=self._database,
                    container_name=name,
                    container_type=ContainerType.TABLE,
                )
                for name in page
            ],
            next_cursor=page[-1] if page and len(names) > len(page) else None,
        )

    def get_schema(self, container: str) -> list[ColumnInfo]:
        return USERS_COLUMNS if container == "users" else []

    def get_sample(self, container: str, limit: int = 3) -> list[dict[str, Any]]:
        return [{"id": 1}][: self._cap_limit(limit)]

    def profile_column(self, container, column, mode):
        raise NotImplementedError


class DummyAdapter(_ContractMixin, AdapterBase):
    def __init__(self, db_name="test_db", **kwargs):
        super().__init__(database=db_name, **kwargs)


class DummySqlAdapter(_ContractMixin, SqlAdapterBase):
    def __init__(self, db_name="test_db", **kwargs):
        super().__init__(database=db_name, **kwargs)


# ---- contract ----


def test_list_databases():
    assert DummyAdapter("my_db").list_databases() == ["my_db"]
    assert DummyAdapter().list_databases() == ["test_db"]


def test_incomplete_adapter_cannot_be_constructed():
    """A missing tool must fail on construction, not on the MCP call."""

    class Incomplete(AdapterBase):
        def list_containers(self, database=None, schema=None, limit=None, cursor=None):
            return ContainerPage(containers=[])

    with pytest.raises(TypeError, match="abstract"):
        Incomplete()  # type: ignore[abstract]

    # the bases are abstract in their own right
    with pytest.raises(TypeError, match="abstract"):
        AdapterBase()  # type: ignore[abstract]
    with pytest.raises(TypeError, match="abstract"):
        SqlAdapterBase()  # type: ignore[abstract]


def test_known_containers():
    assert DummyAdapter()._known_containers() == {"orders", "users"}


def test_known_containers_follows_the_cursor_to_the_end():
    """Page one alone would reject the rest as unknown."""

    class OnePerPage(DummyAdapter):
        _MAX_PAGE_SIZE = 1

    assert OnePerPage()._known_containers() == {"orders", "users"}


def test_require_container_accepts_a_name_on_a_later_page():
    class OnePerPage(DummySqlAdapter):
        _MAX_PAGE_SIZE = 1

    assert OnePerPage()._require_container("users") == '"users"'


def test_known_containers_is_immutable():
    """The cache hands the same object to every caller."""
    assert isinstance(DummyAdapter()._known_containers(), frozenset)


# ---- catalog cache ----


class CountingAdapter(DummySqlAdapter):
    """Counts what actually reaches the source."""

    def __init__(self, **kwargs):
        self.walks = 0
        self.schema_reads = 0
        super().__init__(**kwargs)

    def _walk_containers(self):
        self.walks += 1
        return super()._walk_containers()

    def get_schema(self, container):
        self.schema_reads += 1
        return super().get_schema(container)


def test_the_catalog_is_walked_once_per_ttl():
    adapter = CountingAdapter()

    assert adapter._known_containers() == adapter._known_containers()
    assert adapter.walks == 1


def test_validation_shares_one_walk_and_one_schema_read():
    """Both checks run per column of every scan; each used to hit the source."""
    adapter = CountingAdapter()

    adapter._require_container("users")
    adapter._require_column("users", "id")
    adapter._require_column("users", "name")

    assert adapter.walks == 1
    assert adapter.schema_reads == 1


def test_a_zero_ttl_disables_the_cache():
    adapter = CountingAdapter(catalog_ttl=0)

    adapter._known_containers()
    adapter._known_containers()

    assert adapter.walks == 2


def test_an_expired_entry_is_read_again():
    adapter = CountingAdapter(catalog_ttl=30)
    adapter._known_containers()

    assert adapter._catalog_cache is not None
    stamp, names = adapter._catalog_cache
    # age the entry rather than sleeping through the ttl
    adapter._catalog_cache = (stamp - 60, names)

    assert adapter._known_containers() == names
    assert adapter.walks == 2


def test_invalidating_the_cache_forces_a_reread():
    """The way to see a container created since the last walk."""
    adapter = CountingAdapter()
    adapter._require_container("users")
    adapter._require_column("users", "id")

    adapter.invalidate_catalog_cache()
    adapter._require_container("users")
    adapter._require_column("users", "id")

    assert adapter.walks == 2
    assert adapter.schema_reads == 2


def test_the_cache_does_not_leak_between_adapters():
    first, second = CountingAdapter(), CountingAdapter()

    first._known_containers()

    assert second.walks == 0


def test_get_schema_as_a_tool_still_reads_through():
    """Only validation caches: a schema tool call must see the source."""
    adapter = CountingAdapter()

    adapter.get_schema("users")
    adapter.get_schema("users")

    assert adapter.schema_reads == 2


def test_cap_page_size():
    adapter = DummyAdapter()

    assert adapter._cap_page_size(None) == AdapterBase._DEFAULT_PAGE_SIZE
    assert adapter._cap_page_size(10) == 10
    assert adapter._cap_page_size(10_000) == AdapterBase._MAX_PAGE_SIZE
    assert adapter._cap_page_size(0) == 1
    assert adapter._cap_page_size(-5) == 1


# ---- policy ----


def test_cap_limit():
    adapter = DummyAdapter()

    assert adapter._cap_limit(-10) == 0
    assert adapter._cap_limit(50) == 50
    assert adapter._cap_limit(1000) == AdapterBase._MAX_SAMPLE_LIMIT


def test_max_sample_limit_is_per_instance():
    capped = DummyAdapter(max_sample_limit=10)

    assert capped._cap_limit(1000) == 10
    assert DummyAdapter()._cap_limit(1000) == AdapterBase._MAX_SAMPLE_LIMIT
    assert AdapterBase._MAX_SAMPLE_LIMIT == 100


# ---- statement log ----


def test_record_sql():
    adapter = DummyAdapter()

    assert adapter.pop_rendered_sql() is None

    adapter._record_sql("SELECT 1")
    adapter._record_sql("SELECT 2")

    assert adapter.pop_rendered_sql() == "SELECT 1; SELECT 2"
    assert adapter.pop_rendered_sql() is None


def test_statement_logs_are_not_shared_between_adapters():
    first, second = DummyAdapter(), DummyAdapter()

    first._record_sql("SELECT 1")

    assert second.pop_rendered_sql() is None
    assert first.pop_rendered_sql() == "SELECT 1"


# ---- SqlAdapterBase: identifiers ----


def test_quote():
    adapter = DummySqlAdapter()
    assert adapter._quote("valid_name_123") == '"valid_name_123"'

    with pytest.raises(ValueError, match="illegal identifier"):
        adapter._quote("invalid-name")
    with pytest.raises(ValueError, match="illegal identifier"):
        adapter._quote("drop table;")


def test_require_container():
    adapter = DummySqlAdapter()
    assert adapter._require_container("users") == '"users"'

    with pytest.raises(UnknownContainerError, match="not_exist"):
        adapter._require_container("not_exist")


def test_require_column():
    adapter = DummySqlAdapter()
    assert adapter._require_column("users", "id") == '"id"'
    assert adapter._require_column("users", "name") == '"name"'

    with pytest.raises(UnknownColumnError, match="users.age"):
        adapter._require_column("users", "age")


# ---- SqlAdapterBase: templates ----


def test_sql_templates():
    adapter = DummySqlAdapter()
    quoted_table = adapter._quote("users")
    quoted_col = adapter._quote("id")

    sql, params = adapter._sql_sample(quoted_table, 5)
    assert sql == 'SELECT * FROM "users" LIMIT ?'
    assert params == (5,)

    sql, params = adapter._sql_distinct_count(quoted_table, quoted_col)
    assert sql == 'SELECT COUNT(DISTINCT "id") AS n FROM "users"'
    assert params == ()

    sql, params = adapter._sql_null_ratio(quoted_table, quoted_col)
    assert (
        sql
        == 'SELECT AVG(CASE WHEN "id" IS NULL THEN 1.0 ELSE 0.0 END) AS r FROM "users"'
    )
    assert params == ()

    sql, params = adapter._sql_top_values(quoted_table, quoted_col, 10)
    assert (
        sql
        == 'SELECT "id" AS v, COUNT(*) AS c FROM "users" GROUP BY "id" ORDER BY c DESC, v LIMIT ?'
    )
    assert params == (10,)

    sql, params = adapter._sql_min_max(quoted_table, quoted_col)
    assert sql == 'SELECT MIN("id") AS lo, MAX("id") AS hi FROM "users"'
    assert params == ()


def test_profile_mode_enum_is_fully_covered_by_templates():
    adapter = DummySqlAdapter()
    builders = {
        ProfileMode.DISTINCT_COUNT: adapter._sql_distinct_count,
        ProfileMode.NULL_RATIO: adapter._sql_null_ratio,
        ProfileMode.MIN_MAX: adapter._sql_min_max,
    }

    assert set(builders) | {ProfileMode.TOP_VALUES} == set(ProfileMode)
    for build in builders.values():
        sql, _ = build('"users"', '"id"')
        assert sql.startswith("SELECT")
