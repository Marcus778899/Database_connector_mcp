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


def test_the_certificate_is_checked_unless_told_otherwise():
    """The default is the strict one. An on-premises server with a self-signed
    certificate is common, but so is a certificate that stopped verifying for a
    reason somebody should hear about."""
    conn = resolve_connection("my-db", env={"MY_DB_HOST": "localhost"})

    assert conn.trust_server_certificate is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_trusting_the_certificate_is_opt_in(raw: str):
    env = {"MY_DB_HOST": "localhost", "MY_DB_TRUST_SERVER_CERTIFICATE": raw}

    assert resolve_connection("my-db", env=env).trust_server_certificate is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_the_flag_can_be_turned_off_explicitly(raw: str):
    env = {"MY_DB_HOST": "localhost", "MY_DB_TRUST_SERVER_CERTIFICATE": raw}

    assert resolve_connection("my-db", env=env).trust_server_certificate is False


def test_a_flag_that_is_neither_is_refused():
    """Read as False it fails later as a certificate error; read as True it
    turns off a check the operator thought was on. Neither is worth guessing."""
    env = {"MY_DB_HOST": "localhost", "MY_DB_TRUST_SERVER_CERTIFICATE": "maybe"}

    with pytest.raises(ValueError, match="expects a boolean"):
        resolve_connection("my-db", env=env)


def test_a_flag_on_its_own_is_not_a_connection():
    """Half a configuration should read as the missing host, not as a
    connection with nothing in it."""
    env = {"MY_DB_TRUST_SERVER_CERTIFICATE": "1"}

    with pytest.raises(MissingConnectionEnvError):
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
