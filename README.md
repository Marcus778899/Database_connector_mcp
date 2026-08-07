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

Every engine in the table below has an adapter: sqlite, datalake
(parquet/csv/json), postgres, mysql/mariadb, mssql, mongodb, and another MCP
server fronted as a source. A network transport can be authenticated with a
signed token (see [Authentication](#authentication)); without one it must stay
on loopback.

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
| `postgres` | `uv sync --extra postgres` | psycopg 3 |
| `mysql` / `mariadb` | `uv sync --extra mysql` | one adapter serves both |
| `mssql` | `uv sync --extra mssql` | pyodbc — the ODBC driver itself is not a python package |
| `mongodb` | `uv sync --extra mongo` | schema inferred from a sample; see [Collections have no schema](#collections-have-no-schema) |

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

Which ones each engine wants:

| engine | needs | instead of, or as well |
|---|---|---|
| `sqlite` | `_PATH` (a `.db` file) | `_URI` |
| `datalake` | `_URI` (`s3://…`, `gs://…`) or `_PATH` | |
| `postgres` | `_HOST`, `_USER`, `_PASSWORD`, `_DB` | `_URI` — a whole libpq conninfo, and the only way to reach a unix socket |
| `mysql` / `mariadb` | `_HOST`, `_USER`, `_PASSWORD`, `_DB` | `_URI` (`mysql://user:pass@host:port/db`) |
| `mssql` | `_HOST`, `_USER`, `_PASSWORD`, `_DB` | `_URI` — a whole ODBC connection string |
| `mongodb` | `_URI` (`mongodb://…`) or `_HOST`, plus `_DB` | the url's path counts as `_DB` |
| `mcp` | `_URI` (`http://…`) or `_PATH`, plus `_TOKEN` | |

`_DB` is what `--database` overrides, and for postgres, mysql, mssql and mongodb
it selects one database on a server that has several. Asking for another one
opens a second connection, which the pool keeps beside the first.

The mssql connection string is built with `Encrypt=yes` and the certificate
checked. A server with a self-signed certificate needs
`TrustServerCertificate=yes`, and the way to say so is a whole `<REF>_URI` — it
is a decision to take on purpose, not a default to inherit.

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
| `--export-dir` | `MCP_EXPORT_DIR` | unset — **no export tool** |
| `--audit-log` | `MCP_AUDIT_LOG` | unset — log only |
| `--audit-max-mb` | `MCP_AUDIT_MAX_MB` | `10` — `0` never rotates |
| `--audit-backups` | `MCP_AUDIT_BACKUPS` | `5` |
| `--profile-mode` | `MCP_PROFILE_MODES` | unset — chosen per column |
| `--no-profile` | `MCP_PROFILE=false` | off — statistics are gathered |
| `--server-name` | `MCP_SERVER_NAME` | `etl-agent-mcp` |
| `--require-auth` | `MCP_REQUIRE_AUTH` | `false` |
| `--allow-insecure-http` | `MCP_ALLOW_INSECURE_HTTP` | `false` |

`--staging-db` is deliberately off by default: without somewhere to accumulate a
scan there is nowhere to put what it gathers, so the inventory tools are not
served at all rather than served and broken. It must not point at the database
being inventoried; the store refuses that.

`--profile-mode` applies the modes you name to every column. Left unset, each
column gets what its type warrants — a range for a number or a date, the
commonest values for a categorical one, and nothing beyond a null ratio for a
type that means nothing to us. `--no-profile` turns it off entirely.

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

## Authentication

Only for the http transports. The threat is "whoever learns the URL can read the
database"; over stdio there is nothing to defend, because spawning the process
already hands over the credential.

`mcp.json` can carry a static header but cannot compute a signature, so the
signing happens outside the serving process. Same command, one word apart:

```bash
uv run mcp-connector token keygen --kid pm-explorer --keys-dir ./keys --out ./pm-explorer.pem
```

That writes `./keys/pm-explorer.pub`, which the server reads, and the signing key,
which the server never reads. Then per token:

```bash
uv run mcp-connector token issue --key ./pm-explorer.pem --kid pm-explorer --scope list_containers --scope get_schema --lifetime 30d
```

The result goes in the agent's `Authorization: Bearer …` header. Serve with:

```bash
uv run mcp-connector --engine sqlite --connection-ref shop --transport http --host 0.0.0.0 --require-auth --authorized-keys-dir ./keys
```

| claim | what it does |
|---|---|
| `kid` (header) | names the public key in `--authorized-keys-dir` that verifies it |
| `sub` | who the caller is, and what the audit trail records |
| `aud` | must equal `--audience`, so a token signed for elsewhere is refused here |
| `exp` | required; expiry is the main way a token stops working |
| `scopes` | the tools it may call, by name. No scopes, no tools. |

**Revoking** is `rm ./keys/<kid>.pub` — no restart, effective within 30 seconds.
Keys are re-read that often rather than cached for the life of the process, which
is the whole reason revocation works without one.

`exp` is checked with 60 seconds of leeway, so a token is accepted for up to a
minute past its expiry — a token that has just run out was almost certainly
issued against a clock a shade different from this one. It matters only if you
issue very short lifetimes: a `--lifetime 5m` token is good for six.

Signatures are asymmetric only (EdDSA, ES256, RS256). An HMAC algorithm is never
accepted: the keys here are public, so a token signed with one of them as a
shared secret would be a forgery anyone could produce.

Authentication is not authorisation. A valid token still gets a tool error for a
tool outside its scopes, and the refusal is recorded in the audit trail like any
other call.

### In a container

`token keygen --if-missing` succeeds quietly when the signing key is already
there, which is what an entrypoint that runs on every start needs — generating a
fresh pair would invalidate every token already handed out. It also puts the
public half back if only that is gone, since the two can sit on different
volumes.

```bash
#!/bin/sh
set -e
mcp-connector token keygen --kid pm-explorer \
    --keys-dir /keys --out /secrets/pm-explorer.pem --if-missing
mcp-connector token issue --key /secrets/pm-explorer.pem --kid pm-explorer \
    --scope list_containers --scope get_schema --scope get_sample \
    --lifetime 30d --out /tokens/pm-explorer.jwt
exec mcp-connector --engine sqlite --connection-ref shop \
    --transport http --host 0.0.0.0 --require-auth --authorized-keys-dir /keys
```

Hand `/tokens/pm-explorer.jwt` to the agent that needs it — that is the thing
that goes in `mcp.json`, not the public key, which never leaves the server.

One thing to be deliberate about: this puts the signing key on the server's
host, which is the arrangement the split was meant to avoid — whoever takes the
container can then mint tokens for any subject, and the audit trail's account of
who did what stops being evidence. It is a defensible trade for a single-tenant
deployment, where that container already holds the database credential. Keep
`/secrets` a mounted volume rather than a baked-in layer, and if the audit trail
ever has to stand up to scrutiny, move `keygen` out to wherever you run it by
hand and let the container do nothing but `issue` — or nothing at all.

## Tools

Read-only against the source, and where the engine can enforce that rather than
be trusted about it, it does: sqlite opens the file `mode=ro`, postgres and
mysql set the session read-only, and a write that ever slipped in would fail at
the server. sql server has no such switch and the datalake and mongo drivers
none either, so there the guarantee is that this code issues nothing but reads.

Five tools answer live:

| tool | what it gives |
|---|---|
| `list_databases` | databases this connection can inventory |
| `list_containers` | one page of tables / views / collections, keyset paged |
| `get_schema` | a container's columns, with key and nullability flags |
| `get_sample` | a few rows, capped by `--max-sample-limit` |
| `profile_column` | one statistic about one column |

Seven more appear when `--staging-db` is set. A scan of a large source outlasts
any single tool call, so it runs in the background:

| tool | what it does |
|---|---|
| `inventory_start` | begin a scan, return a job id |
| `inventory_status` | how far it got, and why it stopped |
| `inventory_cancel` | stop after the container in flight; progress is kept |
| `inventory_summary` | counts over what has been inventoried — **ask for this first** |
| `inventory_containers` | one page of inventoried containers |
| `inventory_columns` | one page of a container's columns |
| `inventory_search` | containers and columns matching a keyword |
| `inventory_relationships` | every foreign key, as edges to draw an ER diagram from |
| `inventory_changes` | what the upstream schema did between scans |
| `inventory_annotate` | describe a table or its columns |

A scan resumes from its cursor if it dies, and skips containers whose schema
fingerprint has not changed. What it does notice — a table appearing or
disappearing, a column added, removed or retyped — is kept append-only and read
back with `inventory_changes`. A run that resumed from a cursor never reports
anything removed: it did not look at what came before it.

One more appears when `--export-dir` is set: `inventory_export`.

### What a container is called

Every tool takes a container as one string, so that string has to identify one
container. On postgres and sql server it therefore always carries the schema:

```
public.users        dbo.orders        sales.users
```

A bare `users` still works where exactly one schema has one — an agent relaying
a name somebody said will not have the schema. Where several do, the call is
refused with the list rather than answered from whichever came first: reading
the wrong table and saying nothing about it is the worse failure.

sqlite, mysql and mongodb have no schema layer below the database, so their
containers are named plainly. mysql's `SCHEMA` is a synonym for `DATABASE`, and
passing one that is not the connected database is refused rather than ignored.

### Collections have no schema

mongodb has no declared schema to read, so `get_schema` **infers** one from the
first hundred documents of a collection: the fields they carry, and the BSON
types each was seen holding. That makes it a description of the sample rather
than a guarantee about the collection, which is why:

- a field seen holding more than one type is reported as all of them, `int|string`
- a field missing from a document counts as null in that document
- only top-level fields are reported — a nested document is `object` and an
  array is `array`, because flattening `a.b.c` turns one collection into an
  unbounded list of paths that would still only describe the sample
- profiling a field no sampled document carried is refused, rather than
  answered with a null ratio of 1.0 that reads as a fact about the collection

## Reading a catalog that will not fit

A catalog of any size does not belong in an agent's context, and the fix is not
a bigger window — it is not putting it there:

| you want | ask for |
|---|---|
| how big is this | `inventory_summary` — counts only, a few hundred bytes |
| where is the thing I mean | `inventory_search` — narrow hits, no statistics |
| what is in this table | `inventory_columns` — one page, `include_profile=False` on a wide one |
| all of it | `inventory_export` — **a file**, and only its path comes back |

`inventory_export` writes `markdown` (a data dictionary to read), `csv` (a row
per column) or `dbt_yaml` (a `schema.yml` for a dbt project), and returns where
it wrote and how much — never the contents. That is what makes "inventory the
whole warehouse" a request this server can answer.

```bash
uv run mcp-connector --engine sqlite --connection-ref shop \
    --staging-db ./var/staging.db --export-dir ./var/exports
```

Without `--export-dir` the tool is not served at all: there would be nowhere to
put what it writes. A path given to it is relative to that directory and cannot
leave it — `../` and symlinks are resolved before the check, because the caller
is an agent relaying a path someone gave it.

The budgets are measured, not hoped for: `tests/test_context_budget.py` builds a
500-table catalog and asserts what each tool costs.

## Personal data

`get_sample` puts real rows into an agent's context, and from there into every
log, transcript and history that context touches. So **rows are masked by
default**:

```
{"id": 1, "email": "a***@***.com", "note": "hello", "api_key": "***"}
```

The shape survives where it is useful — an agent reasoning about the table can
still see that the column holds email addresses — and a secret keeps nothing,
because there is no shape worth showing.

Which columns count is decided by the scan, from the column name first and then
from a small sample of values for the names that give nothing away. Only the
verdict is stored, never the values it was reached from. It is a guess, and
`inventory_annotate` overrides it:

```json
{"column": "internal_ref", "sensitivity": "pii"}
```

A verdict written that way is never overruled by a later scan — somebody looked
at the thing, and a pattern match did not.

`mask=False` returns the rows as they are. Over stdio that is allowed, because
whoever spawned the process already holds the database credential. Over an
authenticated transport the key has to have been granted it, and the audit trail
records that it happened.

## What a key may see

Beyond the list of tools, a token can carry:

```bash
uv run mcp-connector token issue --key ./pm.pem --kid pm-explorer \
    --scope list_containers --scope get_schema --scope get_sample \
    --database analytics \
    --allow-container 'dim_*' --allow-container 'fct_*' \
    --deny-container '*_pii'
```

| flag | claim | absent means |
|---|---|---|
| `--database` | `databases` | every database |
| `--allow-container` / `--deny-container` | `containers` | everything not denied |
| `--allow-raw-sample` | `allow_raw_sample` | **no** — masked rows only |
| `--annotate-as-human` | `annotate_as_human` | descriptions are recorded as an agent's |

Deny beats allow. A container a key may not read is refused **by name** when
asked for directly — an agent told a table is not there goes looking for it,
one told it may not look asks for access — and left out of listings, searches
and exports, so the catalog's shape does not leak to someone who cannot read it.

These are claims *added* to the token, so one issued before any of them existed
still means exactly what it meant: all tools it was scoped for, every database,
every container, and no raw rows.

## Descriptions

A schema without descriptions is a list of names and types, which is exactly
what a PM cannot read. So the inventory keeps two kinds of description, in
separate columns:

- **`native_description`** — the source's own comment. A scan reads it and
  overwrites it, and it is part of the fingerprint, so a comment edited upstream
  is a change worth rescanning for. sqlite has no comments at all.
- **`description`** — written through `inventory_annotate` by an agent or a
  person. **A scan never touches it**, and it stays out of the fingerprint so
  that writing one cannot trigger its own rescan.

`inventory_annotate` is the only tool here that writes, and the only thing it
can write to is the inventory — the source database is never touched by
anything in this server. Who a description came from is decided by the server
from the caller's key, not claimed by the caller: `human` needs a token
carrying the `annotate:human` scope, and everything else is `ai`.

The staging file carries its layout version. A file written by an older build is
refused with instructions rather than migrated: an inventory is derived data
that a rescan reproduces, so the only thing genuinely lost is what was
annotated.

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
