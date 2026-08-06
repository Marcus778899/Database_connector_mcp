# database-mcp-connector

An MCP server that makes an upstream data source's catalog — **containers, columns,
types and descriptions** — readable by an AI agent, so two kinds of people can get
at it without holding a database credential:

- a **data engineer** who wants the whole catalog inventoried at once
- a **PM** who cannot write SQL, but can explore through an agent

The database credential lives only in this server's environment. Whoever uses the
server never sees it.

Where this is going, and why: [docs/inventory-roadmap.md](docs/inventory-roadmap.md).

## Status

sqlite and datalake (parquet/csv/json) work today. postgres, mysql, mssql and
mongodb are registered but not written yet — asking for one says so. There is no
authentication provider yet, so a network transport must stay on loopback (see
[docs/authentication.md](docs/authentication.md)).

## Install

```bash
uv sync --extra server
```

The server extra is what makes the MCP layer importable. Each engine's driver is
its own extra:

| engine | extra | notes |
|---|---|---|
| `sqlite` | — | stdlib, nothing to install |
| `datalake` | `uv sync --extra datalake` | parquet / csv / json via pyarrow |
| `mcp` | `uv sync --extra mcp` | front another MCP server as a source |
| `postgres` | `uv sync --extra postgres` | adapter not implemented yet |
| `mysql` / `mariadb` | `uv sync --extra mysql` | adapter not implemented yet |
| `mssql` | `uv sync --extra mssql` | adapter not implemented yet |
| `mongodb` | `uv sync --extra mongo` | adapter not implemented yet |

## Run

Connection details are **not** passed as flags. They live in environment
variables under a prefix you choose, and `--connection-ref` names that prefix:

```bash
export SHOP_PATH=./var/shop.db
uv run mcp-connector --engine sqlite --connection-ref shop --staging-db ./var/staging.db
```

`SHOP_PATH` above is `<REF>_PATH`. The recognised suffixes are `_HOST`, `_PORT`,
`_USER`, `_PASSWORD`, `_DB` / `_DATABASE`, `_URI`, `_PATH` and `_TOKEN`; an engine
uses the ones that apply to it. A repo-local `.env` is loaded automatically.

Every other setting has a flag and an `MCP_*` variable, and the flag wins:

| flag | variable | default |
|---|---|---|
| `--engine` | `MCP_ENGINE` | `sqlite` |
| `--connection-ref` | `MCP_CONNECTION_REF` | *required* |
| `--database` | `MCP_DATABASE` | the engine's own default |
| `--transport` | `MCP_TRANSPORT` | `stdio` |
| `--host` / `--port` | `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8000` |
| `--max-sample-limit` | `MCP_MAX_SAMPLE_LIMIT` | `100` |
| `--staging-db` | `MCP_STAGING_DB` | unset — **no inventory tools** |
| `--export-dir` | `MCP_EXPORT_DIR` | unset |
| `--audit-log` | `MCP_AUDIT_LOG` | unset — log only |
| `--profile-mode` | `MCP_PROFILE_MODES` | none gathered |
| `--server-name` | `MCP_SERVER_NAME` | `etl-agent-mcp` |
| `--require-auth` | `MCP_REQUIRE_AUTH` | `false` |
| `--allow-insecure-http` | `MCP_ALLOW_INSECURE_HTTP` | `false` |

`--staging-db` is deliberately off by default: without somewhere to accumulate a
scan there is nowhere to put what it gathers, so the inventory tools are not
served at all rather than served and broken. It must not point at the database
being inventoried; the store refuses that.

## Connect an agent

```json
{
  "mcpServers": {
    "shop-catalog": {
      "command": "uv",
      "args": [
        "run", "mcp-connector",
        "--engine", "sqlite",
        "--connection-ref", "shop",
        "--staging-db", "./var/staging.db"
      ],
      "cwd": "/path/to/Database_connector_mcp",
      "env": { "SHOP_PATH": "./var/shop.db" }
    }
  }
}
```

Over stdio the client spawns the process and already holds its environment, so
there is nothing for authentication to add — `--require-auth` is refused there on
purpose.

## Tools

Read-only against the source. Five answer live:

| tool | what it gives |
|---|---|
| `list_databases` | databases this connection can inventory |
| `list_containers` | one page of tables / views / collections, keyset paged |
| `get_schema` | a container's columns, with key and nullability flags |
| `get_sample` | a few rows, capped by `--max-sample-limit` |
| `profile_column` | one statistic about one column |

Six more appear when `--staging-db` is set. A scan of a large source outlasts any
single tool call, so it runs in the background:

| tool | what it does |
|---|---|
| `inventory_start` | begin a scan, return a job id |
| `inventory_status` | how far it got, and why it stopped |
| `inventory_cancel` | stop after the container in flight; progress is kept |
| `inventory_summary` | counts over what has been inventoried — **ask for this first** |
| `inventory_containers` | one page of inventoried containers |
| `inventory_columns` | recorded columns of one container |

A scan resumes from its cursor if it dies, and skips containers whose schema
fingerprint has not changed.

## Develop

```bash
uv sync --all-extras --dev
uv run pytest
```

Layering is enforced by a test, not by convention: `core` holds the contracts and
must not import any implementation. `tests/test_layering.py` fails if that
direction is broken, and a new package under `src/` has to be given a rule there.

```bash
uvx ruff@0.4.4 format . && uvx ruff@0.4.4 check .
uvx pyright
```
