import pytest
from typing import Any

from src.core.tool import ColumnInfo, ContainerInfo, ContainerType
from src.adapter.db_interface import (
    SqlAdapterBase,
    UnknownContainerError,
    UnknownColumnError,
)

class DummySqlAdapter(SqlAdapterBase):
    def __init__(self, db_name="test_db"):
        self._database = db_name

    def list_containers(self, database: str | None = None, schema: str | None = None) -> list[ContainerInfo]:
        return [
            ContainerInfo(database=self._database, container_name="users", container_type=ContainerType.TABLE)
        ]

    def get_schema(self, container: str) -> list[ColumnInfo]:
        if container == "users":
            return [
                ColumnInfo(name="id", ordinal=1, native_type="INTEGER", nullable=False, is_pk=True, is_fk=False),
                ColumnInfo(name="name", ordinal=2, native_type="TEXT", nullable=True, is_pk=False, is_fk=False),
            ]
        return []

def test_list_databases():
    adapter = DummySqlAdapter("my_db")
    assert adapter.list_databases() == ["my_db"]

    base = SqlAdapterBase()
    assert base.list_databases() == [""]

def test_not_implemented_base():
    base = SqlAdapterBase()
    with pytest.raises(NotImplementedError):
        base.list_containers()
    with pytest.raises(NotImplementedError):
        base.get_schema("users")

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

def test_cap_limit():
    adapter = DummySqlAdapter()
    assert adapter._cap_limit(-10) == 0
    assert adapter._cap_limit(50) == 50
    assert adapter._cap_limit(1000) == SqlAdapterBase._MAX_SAMPLE_LIMIT

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
    assert sql == 'SELECT AVG(CASE WHEN "id" IS NULL THEN 1.0 ELSE 0.0 END) AS r FROM "users"'
    assert params == ()

    sql, params = adapter._sql_top_values(quoted_table, quoted_col, 10)
    assert sql == 'SELECT "id" AS v, COUNT(*) AS c FROM "users" GROUP BY "id" ORDER BY c DESC, v LIMIT ?'
    assert params == (10,)

    sql, params = adapter._sql_min_max(quoted_table, quoted_col)
    assert sql == 'SELECT MIN("id") AS lo, MAX("id") AS hi FROM "users"'
    assert params == ()

def test_record_sql():
    adapter = DummySqlAdapter()
    
    assert adapter.pop_rendered_sql() is None
    
    adapter._record_sql("SELECT 1")
    adapter._record_sql("SELECT 2")
    
    assert adapter.pop_rendered_sql() == "SELECT 1; SELECT 2"
    assert adapter.pop_rendered_sql() is None
