from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal, Mapping, Any

from pydantic import BaseModel, model_validator

from src.core.contracts import ProfileMode
from src.core.engines import SourceEngine
from src.core.log import log

# Re-exported: every caller says `from src.core.config import SourceEngine`, and
# the enum only moved because the Dockerfile has to read it before pydantic is
# installed. See src/core/engines.py.
__all__ = [
    "ConnectionInfo",
    "DEFAULT_AUDIENCE",
    "MissingConnectionEnvError",
    "ServerConfig",
    "SourceEngine",
    "Transport",
    "resolve_connection",
]

DEFAULT_AUDIENCE = "etl-agent-mcp"
_REF_NORMALISE = re.compile(r"[^A-Za-z0-9]+")

Transport = Literal["stdio", "http", "streamable-http", "sse"]


_CONN_SUFFIXES: dict[str, str] = {
    "HOST": "host",
    "PORT": "port",
    "USER": "user",
    "PASSWORD": "password",
    "DB": "database",
    "DATABASE": "database",
    "URI": "uri",
    "PATH": "path",
    "TOKEN": "token",
    "TRUST_SERVER_CERTIFICATE": "trust_server_certificate",
}

# The suffixes above whose value is a flag rather than a string.
_CONN_FLAGS: frozenset[str] = frozenset({"trust_server_certificate"})
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSY: frozenset[str] = frozenset({"0", "false", "no", "off"})


class MissingConnectionEnvError(Exception):
    """`connection_ref` None of the corresponding environment variables exist."""


class ConnectionInfo(BaseModel):
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    uri: str | None = None
    path: str | None = None
    token: str | None = None
    # Skip verification of the server's TLS certificate while keeping the
    # connection encrypted. Off by default and never inferred: an on-premises
    # SQL Server with a self-signed certificate is the common case, but so is
    # a certificate that stopped verifying for a reason worth knowing about.
    trust_server_certificate: bool = False


class ServerConfig(BaseModel):
    server_name: str = "etl-agent-mcp"
    engine: SourceEngine = SourceEngine.SQLITE
    connection_ref: str | None = None
    database: str | None = None
    max_sample_limit: int = 100

    # Where a scan accumulates. Unset means the inventory tools are not served
    # at all: there would be nowhere to put what they gather.
    staging_db_path: Path | None = None
    # An export may only be written under here. Unset means no export tool.
    export_dir: Path | None = None
    # Which statistics a scan gathers when the caller names none. Unset leaves
    # the choice to the engine, per column; empty means gather none.
    profile_modes: list[ProfileMode] | None = None

    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000

    # Auth applies to network transports only; stdio is trusted at the process
    # level (the client spawned us and already has our environment).
    require_auth: bool = False
    authorized_keys_dir: Path | None = None
    audience: str = DEFAULT_AUDIENCE
    audit_log_path: Path | None = None
    # The trail is the record of who read what, so it is kept by size and in
    # generations rather than allowed to grow without limit. Zero disables.
    audit_max_mb: int = 10
    audit_backups: int = 5
    # Escape hatch for serving a network transport without auth on purpose.
    allow_insecure_http: bool = False

    @model_validator(mode="after")
    def _check_auth_matches_transport(self) -> ServerConfig:
        if self.transport == "stdio":
            if self.require_auth:
                raise ValueError(
                    "require_auth is meaningless over stdio: whoever can spawn this "
                    "process already has its environment. Use an http transport."
                )
            return self

        if not self.require_auth and not (
            self.allow_insecure_http or _is_loopback(self.host)
        ):
            raise ValueError(
                f"transport {self.transport!r} on host {self.host!r} would serve the "
                "database without authentication. Set require_auth=True, bind to "
                "loopback, or set allow_insecure_http=True on purpose."
            )
        return self


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


def _ref_to_prefix(connection_ref: str) -> str:
    return _REF_NORMALISE.sub("_", connection_ref).strip("_").upper()


def _conn_flag(name: str, raw: str) -> bool:
    """
    A yes/no connection setting.

    Refused rather than guessed at: a `SHOP_TRUST_SERVER_CERTIFICATE=maybe` read
    as False fails later as a certificate error, and read as True quietly turns
    off a check the operator thought was on.
    """
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ValueError(f"{name} expects a boolean like 1/0 or true/false, got {raw!r}")


def resolve_connection(
    connection_ref: str, *, env: Mapping[str, str] | None = None
) -> ConnectionInfo:
    env = os.environ if env is None else env
    prefix = _ref_to_prefix(connection_ref)

    values: dict[str, Any] = {}
    for suffix, field in _CONN_SUFFIXES.items():
        raw = env.get(f"{prefix}_{suffix}")
        if raw is None or raw == "":
            continue
        if field == "port":
            values[field] = int(raw)
        elif field in _CONN_FLAGS:
            values[field] = _conn_flag(f"{prefix}_{suffix}", raw)
        else:
            values.setdefault(field, raw)

    # Flags alone are not a connection: `SHOP_TRUST_SERVER_CERTIFICATE=1` with
    # no host is a half-finished configuration, and should read as the missing
    # host rather than as a connection with nothing in it.
    if not set(values) - _CONN_FLAGS:
        message = (
            f"Could not find any environment variables for connection_ref {connection_ref!r}"
            f" (prefix {prefix}_*, e.g., {prefix}_HOST / {prefix}_URI / {prefix}_PATH)"
        )
        log.critical(message)
        raise MissingConnectionEnvError(message)

    log.info(
        f"resolved connection {connection_ref!r} from {prefix}_* "
        f"({', '.join(sorted(values))})"
    )
    return ConnectionInfo.model_validate(values)
