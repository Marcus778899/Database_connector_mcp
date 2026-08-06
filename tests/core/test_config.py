from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.config import (
    MissingConnectionEnvError,
    ServerConfig,
    _ref_to_prefix,
    resolve_connection,
)
from src.core.contracts import ProfileMode


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


def test_the_default_config_is_stdio_without_auth():
    config = ServerConfig()

    assert config.transport == "stdio"
    assert config.require_auth is False


def test_the_audit_trail_is_kept_in_generations_by_default():
    """It becomes the record of who read what, so unbounded growth is not an
    option and neither is silently discarding it."""
    config = ServerConfig()

    assert config.audit_max_mb == 10
    assert config.audit_backups == 5


def test_the_staging_and_export_paths_are_off_by_default():
    """Both gate a set of tools, so their absence must be the quiet case."""
    config = ServerConfig()

    assert config.staging_db_path is None
    assert config.export_dir is None
    assert config.profile_modes is None


def test_paths_and_modes_are_coerced():
    """Through model_validate, the way the entry point supplies raw strings."""
    config = ServerConfig.model_validate(
        {
            "staging_db_path": "var/staging.db",
            "export_dir": "var/exports",
            "profile_modes": ["null_ratio"],
        }
    )

    assert config.staging_db_path == Path("var/staging.db")
    assert config.export_dir == Path("var/exports")
    assert config.profile_modes == [ProfileMode.NULL_RATIO]


def test_auth_over_stdio_is_refused():
    """Whoever can spawn the process already has its environment."""
    with pytest.raises(ValidationError, match="meaningless over stdio"):
        ServerConfig(transport="stdio", require_auth=True)


@pytest.mark.parametrize("transport", ["http", "streamable-http", "sse"])
def test_a_network_transport_without_auth_is_refused(transport):
    """The failure worth preventing: the database served to anyone who connects."""
    with pytest.raises(ValidationError, match="without authentication"):
        ServerConfig(transport=transport, host="0.0.0.0", require_auth=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_may_skip_auth(host):
    assert ServerConfig(transport="http", host=host).require_auth is False


def test_the_insecure_escape_hatch_is_explicit():
    config = ServerConfig(transport="http", host="0.0.0.0", allow_insecure_http=True)

    assert config.allow_insecure_http is True


def test_a_network_transport_with_auth_is_fine():
    assert ServerConfig(transport="http", host="0.0.0.0", require_auth=True)
