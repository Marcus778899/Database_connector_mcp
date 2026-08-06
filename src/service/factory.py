from __future__ import annotations

import importlib
from typing import Protocol, runtime_checkable

from src.core.config import ConnectionInfo, SourceEngine
from src.core.contracts import SourceAdaptor
from src.core.log import log

_ADAPTER_REGISTRY: dict[SourceEngine, tuple[str, str]] = {
    SourceEngine.SQLITE: ("src.adapter.sqlite", "SqliteAdapter"),
    SourceEngine.POSTGRES: ("src.adapter.postgres", "PostgresAdapter"),
    SourceEngine.MYSQL: ("src.adapter.mysql", "MysqlAdapter"),
    SourceEngine.MARIADB: ("src.adapter.mysql", "MysqlAdapter"),
    SourceEngine.MSSQL: ("src.adapter.mssql", "MssqlAdapter"),
    SourceEngine.MONGODB: ("src.adapter.mongodb", "MongoAdapter"),
    SourceEngine.DATALAKE: ("src.adapter.datalake", "DatalakeAdapter"),
    SourceEngine.MCP: ("src.adapter.remote_mcp", "RemoteMcpAdapter"),
}

# Engine -> the `uv sync --extra <name>` that installs its driver.
_ENGINE_EXTRA: dict[SourceEngine, str] = {
    SourceEngine.POSTGRES: "postgres",
    SourceEngine.MYSQL: "mysql",
    SourceEngine.MARIADB: "mysql",
    SourceEngine.MSSQL: "mssql",
    SourceEngine.MONGODB: "mongo",
    SourceEngine.DATALAKE: "datalake",
    SourceEngine.MCP: "mcp",
}


class UnknownEngineError(Exception):
    """The engine is not a `SourceEngine`, or has no adapter registered."""


class AdapterNotAvailableError(Exception):
    """The adapter module is not implemented yet, or its driver is missing."""


@runtime_checkable
class AdapterFactory(Protocol):
    """What `create_adapter` needs from an adapter *class*, so the registry lookup
    is not just `type[Any]`."""

    SUPPORTS_MULTIPLE_DATABASES: bool

    @classmethod
    def from_connection(
        cls,
        conn_info: ConnectionInfo,
        *,
        database: str | None = None,
        max_sample_limit: int | None = None,
    ) -> SourceAdaptor: ...


def resolve_engine(engine: SourceEngine | str) -> SourceEngine:
    """One error type for "engine not supported", instead of ValueError."""
    try:
        return SourceEngine(engine)
    except ValueError as exc:
        supported = ", ".join(e.value for e in SourceEngine)
        raise UnknownEngineError(
            f"unknown engine {engine!r}; supported: {supported}"
        ) from exc


def _import_failure_message(
    engine: SourceEngine, module_path: str, exc: ImportError
) -> str:
    missing = exc.name or ""
    # The adapter module is missing: registered but not written yet, so an
    # install hint would be a lie.
    if missing == module_path or module_path.startswith(f"{missing}."):
        return f"the {engine} adapter is not implemented yet ({module_path} is missing)"
    extra = _ENGINE_EXTRA.get(engine)
    if extra:
        return (
            f"the {engine} adapter needs its driver: uv sync --extra {extra} "
            f"(missing module {missing!r})"
        )
    return f"the {engine} adapter could not be imported: {exc}"


def load_adapter_class(engine: SourceEngine | str) -> type[AdapterFactory]:
    resolved = resolve_engine(engine)
    try:
        module_path, cls_name = _ADAPTER_REGISTRY[resolved]
    except KeyError as exc:
        raise UnknownEngineError(f"no adapter registered for {resolved}") from exc

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        message = _import_failure_message(resolved, module_path, exc)
        log.critical(message)
        raise AdapterNotAvailableError(message) from exc

    cls = getattr(module, cls_name, None)
    if cls is None:
        message = f"{module_path} defines no {cls_name}"
        log.critical(message)
        raise AdapterNotAvailableError(message)
    return cls


def create_adapter(
    engine: SourceEngine | str,
    conn_info: ConnectionInfo,
    *,
    database: str | None = None,
    max_sample_limit: int | None = None,
) -> SourceAdaptor:
    cls = load_adapter_class(engine)
    return cls.from_connection(
        conn_info,
        database=database,
        max_sample_limit=max_sample_limit,
    )
