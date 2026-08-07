# syntax=docker/dockerfile:1.7
#
# Two runtime targets, because one of the engines is not a python package:
#
#   --target slim  (default)  sqlite, postgres, mysql/mariadb, mongodb, mcp
#   --target full             the above plus mssql and datalake
#
# `full` exists only because pyodbc needs unixODBC and Microsoft's own driver
# installed at the OS level. Splitting on that line rather than one image per
# engine keeps the tags down to two: which engine a container actually serves
# is a run-time choice (--engine / MCP_ENGINE), so an image only has to carry
# the drivers, not the decision.
#
# Connection details are deliberately absent from every ARG below. A build
# argument survives in `docker history`, so hosts, users and passwords stay
# run-time environment (see `resolve_connection` in src/core/config.py).

ARG PYTHON_VERSION=3.12


# --------------------------------------------------------------- builder ---
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

# lib/ carries loggerhelper as a path-source wheel, so the lock cannot resolve
# without it. README.md is here because the project metadata names it.
COPY pyproject.toml uv.lock README.md ./
COPY lib/ ./lib/


# ----------------------------------------------------------- deps: slim ---
FROM builder AS build-slim

ARG MCP_EXTRAS="--extra server --extra postgres --extra mysql --extra mongo --extra mcp"

# Dependencies before source, so editing a module does not re-resolve the lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project ${MCP_EXTRAS}

COPY main.py ./
COPY src/ ./src/

# --no-editable installs the project as a real wheel, so the runtime image
# needs /opt/venv and nothing else from here.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable ${MCP_EXTRAS}


# ----------------------------------------------------------- deps: full ---
FROM builder AS build-full

ARG MCP_EXTRAS="--extra server --extra postgres --extra mysql --extra mongo --extra mcp --extra mssql --extra datalake"

# pyodbc ships manylinux wheels, but falls back to compiling when none match.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project ${MCP_EXTRAS}

COPY main.py ./
COPY src/ ./src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable ${MCP_EXTRAS}


# ---------------------------------------------------------- runtime base ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-base

# LOG_DIR: loggerhelper builds log/<date>/ relative to the working directory
# unless told otherwise, and the working directory here is not writable.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_DIR=/data/log \
    MCP_AUTHORIZED_KEYS_DIR=/keys

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home app

# Created in the image rather than left to the mount: a named volume inherits
# the ownership of the directory it covers, and sqlite needs to write -wal and
# -shm siblings, which is permission on the directory rather than the file.
RUN mkdir -p /data /keys /out /private \
    && chown app:app /data /keys /out /private

COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/provision.py /opt/provision/provision.py
COPY docker/templates/ /opt/provision/templates/

ENV PROVISION_HOME=/opt/provision

WORKDIR /app

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]


# ---------------------------------------------------------------- full ----
FROM runtime-base AS full

# Microsoft's driver is not redistributable through pip; msodbcsql18 is the
# ODBC driver name the mssql adapter connects through.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg unixodbc \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64,arm64,armhf signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build-full /opt/venv /opt/venv

USER app


# ---------------------------------------------------------------- slim ----
# Last, so a bare `docker build .` gives the image most deployments want.
FROM runtime-base AS slim

COPY --from=build-slim /opt/venv /opt/venv

USER app
