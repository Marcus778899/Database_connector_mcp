"""
Which engine needs what, in one table.

The same three questions get asked from three places and have to agree:

  * the Dockerfile, at build time, deciding which extras to `uv sync` and
    which OS packages to install;
  * the adapter factory, at import time, explaining what is missing when an
    adapter will not load;
  * the entry point, at startup, refusing to serve an engine whose driver is
    not installed rather than failing on the first tool call.

Stdlib only, and importing nothing from this package. The Dockerfile runs it as
a script in the builder stage — before `uv sync`, so before pydantic exists —
and copies this one file in ahead of the rest of the source for that reason.

`SourceEngine` lives here rather than in `config` because that module needs
pydantic and this one may not have it.
"""

from __future__ import annotations

import re
import sys
from enum import StrEnum


class SourceEngine(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    MARIADB = "mariadb"
    MSSQL = "mssql"
    MONGODB = "mongodb"
    DATALAKE = "datalake"
    MCP = "mcp"


# Always installed: the container serves over HTTP and verifies tokens, whatever
# it is pointed at. `mcp` is not here — fastmcp arrives with `server` — but the
# remote_mcp adapter names it separately, so it stays a real answer below.
BASE_EXTRAS: tuple[str, ...] = ("server",)

# The `[project.optional-dependencies]` group each engine's driver lives in.
# sqlite is absent on purpose: its driver is the standard library.
EXTRAS: dict[SourceEngine, tuple[str, ...]] = {
    SourceEngine.SQLITE: (),
    SourceEngine.POSTGRES: ("postgres",),
    SourceEngine.MYSQL: ("mysql",),
    SourceEngine.MARIADB: ("mysql",),
    SourceEngine.MSSQL: ("mssql",),
    SourceEngine.MONGODB: ("mongo",),
    SourceEngine.DATALAKE: ("datalake",),
    SourceEngine.MCP: ("mcp",),
}

# What the import of the driver actually is, so "is this image able to serve
# that engine" can be answered without opening a connection.
DRIVER_MODULES: dict[SourceEngine, str] = {
    SourceEngine.POSTGRES: "psycopg",
    SourceEngine.MYSQL: "pymysql",
    SourceEngine.MARIADB: "pymysql",
    SourceEngine.MSSQL: "pyodbc",
    SourceEngine.MONGODB: "pymongo",
    SourceEngine.DATALAKE: "pyarrow",
    SourceEngine.MCP: "fastmcp",
}

# Debian packages the *runtime* image needs. Only mssql has any: pyodbc is a
# binding, and the thing it binds to is not a python package.
SYSTEM_PACKAGES: dict[SourceEngine, tuple[str, ...]] = {
    SourceEngine.MSSQL: ("unixodbc", "msodbcsql18"),
}

# Debian packages the *builder* needs, for the case where no wheel matches and
# pip falls back to compiling.
BUILD_PACKAGES: dict[SourceEngine, tuple[str, ...]] = {
    SourceEngine.MSSQL: ("unixodbc-dev",),
}

# msodbcsql18 comes from Microsoft's own apt repository rather than Debian's,
# and installing it means accepting their EULA. Named here so the Dockerfile
# can ask "does this build need that repository at all" instead of always
# adding it.
MS_REPO_PACKAGES: frozenset[str] = frozenset({"msodbcsql18"})

# How the engine is spelled to a person: `mssql` is what the flag takes, "SQL
# Server" is what the customer calls it.
DISPLAY_NAMES: dict[SourceEngine, str] = {
    SourceEngine.SQLITE: "SQLite",
    SourceEngine.POSTGRES: "PostgreSQL",
    SourceEngine.MYSQL: "MySQL",
    SourceEngine.MARIADB: "MariaDB",
    SourceEngine.MSSQL: "SQL Server",
    SourceEngine.MONGODB: "MongoDB",
    SourceEngine.DATALAKE: "Parquet / data lake",
    SourceEngine.MCP: "another MCP server",
}


class UnknownEngineError(ValueError):
    """A name that is not a `SourceEngine`."""


def parse_engine(name: str) -> SourceEngine:
    try:
        return SourceEngine(name.strip().lower())
    except ValueError as exc:
        supported = ", ".join(engine.value for engine in SourceEngine)
        raise UnknownEngineError(
            f"unknown engine {name!r}; supported: {supported}"
        ) from exc


def parse_engines(text: str) -> tuple[SourceEngine, ...]:
    """
    `mssql` or `mssql,postgres`, in the order given and without repeats.

    Plural because one image can carry more than one driver, even though the
    deployment it was built for serves a single engine: a customer on SQL
    Server stays on SQL Server, so the normal case is one name.
    """
    seen: list[SourceEngine] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        engine = parse_engine(part)
        if engine not in seen:
            seen.append(engine)
    if not seen:
        raise UnknownEngineError("no engine named")
    return tuple(seen)


def extras_for(engines: tuple[SourceEngine, ...]) -> tuple[str, ...]:
    """The extras to install, base first and then each engine's, deduplicated."""
    ordered: list[str] = list(BASE_EXTRAS)
    for engine in engines:
        for extra in EXTRAS[engine]:
            if extra not in ordered:
                ordered.append(extra)
    return tuple(ordered)


def packages_for(
    engines: tuple[SourceEngine, ...], table: dict[SourceEngine, tuple[str, ...]]
) -> tuple[str, ...]:
    ordered: list[str] = []
    for engine in engines:
        for package in table.get(engine, ()):
            if package not in ordered:
                ordered.append(package)
    return tuple(ordered)


def image_tag_for(engines: tuple[SourceEngine, ...]) -> str:
    """
    The tag an image built for these engines takes.

    The tag has to encode the engines, because `docker compose up` without
    `--build` reuses whatever image already carries the name. A tag that stayed
    the same while the build arguments changed would hand you a container whose
    configuration says one engine and whose drivers are another's — which fails
    at the first tool call, a long way from the cause.
    """
    return "-".join(engine.value for engine in engines)


def missing_driver(engine: SourceEngine) -> str | None:
    """
    The driver module this engine needs and this interpreter has not got.

    None when the engine is servable here. sqlite always is; its driver is the
    standard library.
    """
    import importlib.util

    module = DRIVER_MODULES.get(engine)
    if module is None:
        return None
    try:
        found = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        found = False
    return None if found else module


def _main(argv: list[str]) -> int:
    """
    A little CLI, for the Dockerfile.

        python src/core/engines.py extras   mssql   -> --extra server --extra mssql
        python src/core/engines.py apt      mssql   -> unixodbc msodbcsql18
        python src/core/engines.py build-apt mssql  -> unixodbc-dev
        python src/core/engines.py ms-repo  mssql   -> exit 0 if needed, 1 if not
        python src/core/engines.py tag      mssql   -> mssql

    Shell-shaped output — one line, space separated — because the caller is
    `RUN` and its only parser is word splitting.
    """
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    question, names = argv
    try:
        engines = parse_engines(names)
    except UnknownEngineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if question == "extras":
        print(" ".join(f"--extra {extra}" for extra in extras_for(engines)))
    elif question == "apt":
        print(" ".join(packages_for(engines, SYSTEM_PACKAGES)))
    elif question == "build-apt":
        print(" ".join(packages_for(engines, BUILD_PACKAGES)))
    elif question == "ms-repo":
        needed = set(packages_for(engines, SYSTEM_PACKAGES)) & MS_REPO_PACKAGES
        return 0 if needed else 1
    elif question == "tag":
        print(image_tag_for(engines))
    else:
        print(f"error: unknown question {question!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
