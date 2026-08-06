import pytest
from src.core.config import (
    resolve_connection,
    _ref_to_prefix,
    MissingConnectionEnvError,
)


def test_ref_to_prefix():
    assert _ref_to_prefix("my-db") == "MY_DB"
    assert _ref_to_prefix("my.db") == "MY_DB"
    assert _ref_to_prefix("my_db_conn") == "MY_DB_CONN"


def test_resolve_connection_valid():
    env = {
        "MY_DB_HOST": "localhost",
        "MY_DB_PORT": "5432",
        "MY_DB_USER": "admin",
        "MY_DB_PASSWORD": "password123",
        "MY_DB_DB": "testdb",
    }
    conn = resolve_connection("my-db", env=env)
    assert conn.host == "localhost"
    assert conn.port == 5432
    assert conn.user == "admin"
    assert conn.password == "password123"
    assert conn.database == "testdb"


def test_resolve_connection_uri():
    env = {"MY_DB_URI": "sqlite:///test.db"}
    conn = resolve_connection("my-db", env=env)
    assert conn.uri == "sqlite:///test.db"


def test_resolve_connection_missing():
    env = {"OTHER_DB_HOST": "localhost"}
    with pytest.raises(
        MissingConnectionEnvError, match="Could not find any environment variables"
    ):
        resolve_connection("my-db", env=env)
