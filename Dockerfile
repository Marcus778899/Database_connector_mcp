# syntax=docker/dockerfile:1.7
#
# One image, built for one engine.
#
#   docker build --build-arg MCP_ENGINES=mssql -t database-mcp-connector:mssql .
#
# The engine is a build argument rather than only a runtime one because the
# drivers cannot be a runtime decision: pyodbc binds to an ODBC driver that is
# not a python package, the image is read-only, and the process runs as a user
# that cannot install anything. `docker-compose.yml` passes MCP_ENGINE through
# to both, so a deployment names its engine once.
#
# There is no slim/full split any more. It existed to keep the mssql driver out
# of images that did not need it, and asking for the engine directly does that
# better: a customer on SQL Server stays on SQL Server, and their image carries
# their driver and nobody else's.
#
# Which extras and OS packages an engine implies is decided by
# src/core/engines.py, which is copied in ahead of the rest of the source and
# run as a script. The runtime reads the same table, so "this image was not
# built for that engine" is one fact rather than three that have to agree.
#
# Connection details are deliberately absent from every ARG below. A build
# argument survives in `docker history`, so hosts, users and passwords stay
# run-time environment (see `resolve_connection` in src/core/config.py).

ARG PYTHON_VERSION=3.12
# Comma separated, though one name is the normal case.
ARG MCP_ENGINES=sqlite


# --------------------------------------------------------------- builder ---
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ARG MCP_ENGINES

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build

# lib/ carries loggerhelper as a path-source wheel, so the lock cannot resolve
# without it. README.md is here because the project metadata names it.
COPY pyproject.toml uv.lock README.md ./
COPY lib/ ./lib/

# Ahead of the rest of the source, and on its own: this one file decides what
# the two steps below install, so a change to any other module must not
# invalidate their cache. It imports nothing — not even pydantic, which is not
# installed at this point.
COPY src/core/engines.py ./src/core/engines.py
RUN python src/core/engines.py extras "${MCP_ENGINES}" > /tmp/extras \
    && python src/core/engines.py build-apt "${MCP_ENGINES}" > /tmp/build-apt \
    && echo "building for ${MCP_ENGINES}: $(cat /tmp/extras)"

# Only where an engine needs it. pyodbc ships manylinux wheels but falls back
# to compiling when none match, and that fallback needs the odbc headers.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    set -eu; \
    packages="$(cat /tmp/build-apt)"; \
    if [ -n "$packages" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends $packages; \
        rm -rf /var/lib/apt/lists/*; \
    fi

# Dependencies before source, so editing a module does not re-resolve the lock.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project $(cat /tmp/extras)

COPY main.py ./
COPY src/ ./src/

# --no-editable installs the project as a real wheel, so the runtime image
# needs /opt/venv and nothing else from here.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable $(cat /tmp/extras)


# --------------------------------------------------------------- runtime ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG MCP_ENGINES

# LOG_DIR: loggerhelper builds log/<date>/ relative to the working directory
# unless told otherwise, and the working directory here is not writable.
# MCP_IN_CONTAINER: so a missing driver is explained as "rebuild the image"
# rather than as "run uv sync", which is the right answer only in a checkout.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_DIR=/data/log \
    MCP_IN_CONTAINER=1 \
    MCP_ROLES_FILE=/etc/mcp/roles.toml \
    MCP_AUTHORIZED_KEYS_DIR=/keys

# The engine table again, before the venv exists, for the same reason as in the
# builder: it is what decides the step below.
COPY src/core/engines.py /usr/local/lib/mcp-engines.py

# Microsoft's driver is not redistributable through pip, so mssql — and only
# mssql — needs an apt step and an extra repository. Every other engine leaves
# this a no-op and the image without curl, gnupg or an added key.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    set -eu; \
    packages="$(python /usr/local/lib/mcp-engines.py apt "${MCP_ENGINES}")"; \
    if [ -n "$packages" ]; then \
        apt-get update; \
        if python /usr/local/lib/mcp-engines.py ms-repo "${MCP_ENGINES}"; then \
            apt-get install -y --no-install-recommends ca-certificates curl gnupg; \
            curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
                | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg; \
            echo "deb [arch=amd64,arm64,armhf signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
                > /etc/apt/sources.list.d/mssql-release.list; \
            apt-get update; \
            ACCEPT_EULA=Y apt-get install -y --no-install-recommends $packages; \
            apt-get purge -y curl gnupg; \
            apt-get autoremove -y; \
        else \
            apt-get install -y --no-install-recommends $packages; \
        fi; \
        rm -rf /var/lib/apt/lists/*; \
    fi

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
# Read by `token issue --role`, by `role list`, and by provision when it writes
# each agent's SKILL.md. Bind-mount over it to change the roles without a
# rebuild — it is configuration, not code.
COPY docker/roles.toml /etc/mcp/roles.toml

ENV PROVISION_HOME=/opt/provision

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER app

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
