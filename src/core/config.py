from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal, Mapping, Any

from pydantic import BaseModel, model_validator

from src.core.contracts import ProfileMode
from src.core.log import log

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
}


class SourceEngine(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    MARIADB = "mariadb"
    MSSQL = "mssql"
    MONGODB = "mongodb"
    DATALAKE = "datalake"
    MCP = "mcp"


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
        else:
            values.setdefault(field, raw)

    if not values:
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
