from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal, Mapping, Any

from pydantic import BaseModel

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
}


class SourceEngine(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    MARIADB = "mariadb"
    MSSQL = "mssql"
    MONGODB = "mongodb"
    DATALAKE = "datalake"


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


class ServerConfig(BaseModel):
    server_name: str = "etl-agent-mcp"
    engine: SourceEngine = SourceEngine.SQLITE
    connection_ref: str | None = None
    database: str | None = None
    max_sample_limit: int = 100

    transport: Transport = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000

    """
    Mandatory verification applies only to cross-network (HTTP-based) requests;
    stdio communication is trusted at the application level and does not require JWTs.
    """
    require_auth: bool = False
    authorized_keys_dir: Path | None = None
    audience: str = DEFAULT_AUDIENCE
    audit_log_path: Path | None = None


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
        raise MissingConnectionEnvError(
            f"Could not find any environment variables for connection_ref {connection_ref!r}"
            f" (prefix {prefix}_*, e.g., {prefix}_HOST / {prefix}_URI / {prefix}_PATH)"
        )
    return ConnectionInfo.model_validate(values)
