from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from loggerhelper import log

from src.core.config import ConnectionInfo, SourceEngine
from src.core.contracts import SourceAdaptor
from src.core.engines import EXTRAS

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

# Engine -> the `uv sync --extra <name>` that installs its driver. The mapping
# itself lives in src/core/engines.py, because the Dockerfile reads it too.
_ENGINE_EXTRA: dict[SourceEngine, str] = {
    engine: extras[0] for engine, extras in EXTRAS.items() if extras
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


def _in_container() -> bool:
    """
    Whether the advice below should be about images or about this checkout.

    `/.dockerenv` is written by the docker runtime itself; the environment
    variable is set by this project's own Dockerfile, so the answer survives
    runtimes that do not write that file.
    """
    return Path("/.dockerenv").exists() or bool(os.environ.get("MCP_IN_CONTAINER"))


def _import_failure_message(
    engine: SourceEngine, module_path: str, exc: ImportError
) -> str:
    missing = exc.name or ""
    # The adapter module is missing: registered but not written yet, so an
    # install hint would be a lie.
    if missing == module_path or module_path.startswith(f"{missing}."):
        return f"the {engine} adapter is not implemented yet ({module_path} is missing)"
    extra = _ENGINE_EXTRA.get(engine)
    if not extra:
        return f"the {engine} adapter could not be imported: {exc}"
    if _in_container():
        # `uv sync` inside a running container is the wrong answer twice over:
        # it does not install the OS-level parts (mssql needs a driver that is
        # not a python package), and whatever it does install is gone on the
        # next start. The engine is a build argument here.
        return (
            f"this image was not built for {engine} (missing module {missing!r}). "
            f"An image carries the driver for the engine it was built with: "
            f"rebuild with MCP_ENGINE={engine}, e.g. "
            f"`MCP_ENGINE={engine} docker compose up --build`."
        )
    return (
        f"the {engine} adapter needs its driver: uv sync --extra {extra} "
        f"(missing module {missing!r})"
    )


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
