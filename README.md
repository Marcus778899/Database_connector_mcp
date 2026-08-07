# database-mcp-connector

An MCP server that makes a data source's catalog — **containers, columns, types
and descriptions** — readable by an AI agent, without handing anyone the
database credential. That credential lives in the server's environment and
nowhere else.

Adapters exist for sqlite, postgres, mysql/mariadb, mssql, mongodb, datalake
(parquet/csv/json) and another MCP server fronted as a source.

- [What you get](#what-you-get)
- [Deploy](#deploy) — [transports](#choosing-a-transport), [compose](#docker-compose), [stdio](#docker-over-stdio), [no docker](#without-docker)
- [Configure](#configure)
- [Connect an agent](#connect-an-agent)
- [Authentication](#authentication)
- [Using it well](#using-it-well)

## What you get

Everything is read-only against the source, and where the engine can enforce
that rather than be trusted about it, it does: sqlite opens the file `mode=ro`,
postgres and mysql set the session read-only. mssql, mongodb and the datalake
drivers have no such switch.

**Five tools answer live**, straight from the source:

| tool | what it gives |
|---|---|
| `list_databases` | databases this connection can reach |
| `list_containers` | one page of tables / views / collections, keyset paged |
| `get_schema` | a container's columns, with key and nullability flags |
| `get_sample` | a few rows, masked by default, capped by `--max-sample-limit` |
| `profile_column` | one statistic about one column |

**Ten more appear when `--staging-db` is set.** They answer from a scan already
recorded in a local sqlite file, so they never touch the source:

| tool | what it does |
|---|---|
| `inventory_start` | begin a background scan, return a job id |
| `inventory_status` | how far it got, and why it stopped |
| `inventory_cancel` | stop after the container in flight; progress is kept |
| `inventory_summary` | counts over what has been inventoried — **ask for this first** |
| `inventory_containers` | one page of inventoried containers |
| `inventory_columns` | one page of a container's columns |
| `inventory_search` | containers and columns matching a keyword |
| `inventory_relationships` | every foreign key, as edges to draw an ER diagram from |
| `inventory_changes` | what the upstream schema did between scans |
| `inventory_annotate` | describe a table or its columns |

**One more appears when `--export-dir` is set**: `inventory_export`, which
writes the whole catalog to a file and returns only its path.

A scan resumes from its cursor if it dies, and skips containers whose schema
fingerprint has not changed. A run that resumed from a cursor never reports
anything removed — it did not look at what came before it.

## Deploy

### Choosing a transport

This decides almost everything else about the deployment, so pick it first.

| transport | shape | authentication | deploy as |
|---|---|---|---|
| `stdio` | the client spawns the process and talks over its stdin/stdout | none, and `--require-auth` is **refused** | one process per client, started by the client |
| `http` / `streamable-http` | one endpoint at `/mcp` | signed bearer token | a long-running service |
| `sse` | legacy two-endpoint form at `/sse` | signed bearer token | only for clients that cannot do streamable-http |

`http` and `streamable-http` are the same transport under two names; use either.

Over **stdio** there is nothing for authentication to add: whoever can spawn the
process already holds its environment, and therefore the database credential.
That is why `--require-auth` is rejected there rather than ignored.

Over a **network transport** the threat is "whoever learns the URL can read the
database". The server will not serve one unauthenticated unless it is bound to
loopback, or you pass `--allow-insecure-http` to say you meant it.

One thing that bites on stdio: **stdout is the JSON-RPC channel**. Anything a
wrapper script prints there breaks the handshake before the client sees the
server. Everything this project's entrypoint prints goes to stderr for that
reason, and yours should too.

### Docker Compose

The default deployment: a network transport with authentication. Two services
out of one image, split so that the serving container never holds a signing key.

```bash
cp .env.example .env    # edit it — engine, connection, scopes
docker compose up -d
```

| service | runs | mounts | what it does |
|---|---|---|---|
| `provision` | once, then exits | `/keys` rw, `/private` rw, `./out` | makes the key pair, signs a token, generates the client artifacts |
| `server` | long | `/keys` **ro**, `/data` | verifies tokens and serves |

`server` waits on `service_completed_successfully`, so a failed provision keeps
the server down rather than serving an allowlist nobody is in.

The split is in the volumes, not the images: the signing key goes to a volume
`server` does not mount, so taking the serving container does not get you the
ability to issue yourself a token. Keep it that way.

`provision` is idempotent — it runs again on every `up` and keeps the key and
token it already made, since a fresh pair would invalidate every token already
handed out. `PROVISION_FORCE=1` signs a new one.

What you collect afterwards:

```
out/
  agent.jwt                          the credential — 0600, treat it as one
  agent/                             a Claude Code plugin
    .mcp.json                        the server entry, token as ${VAR}
    .claude-plugin/plugin.json
    skills/<server-name>/SKILL.md    generated for this token's scopes
    codex.toml                       the same server in Codex's spelling
    INSTALL.md
```

State lives on the `mcp-data` volume: the staging database, the audit trail and
the logs. Nothing is baked into the image — the staging schema builds itself on
first open, so there is nothing to set up in advance.

A file-backed engine needs its source mounted; `docker-compose.yml` carries a
commented example. Networked engines need nothing there.

### Docker, over stdio

No compose, no token, no port. The client starts the container per session:

```json
{
  "mcpServers": {
    "shop-catalog": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "MCP_ENGINE=postgres",
        "-e", "MCP_CONNECTION_REF=shop",
        "-e", "SHOP_HOST", "-e", "SHOP_USER", "-e", "SHOP_PASSWORD", "-e", "SHOP_DB",
        "database-mcp-connector:slim", "serve"
      ]
    }
  }
}
```

`-e NAME` without a value is docker's pass-through form: the variable comes from
the environment the client was started in, so no secret is written into this
file. `provision` generates this shape for you when `MCP_TRANSPORT=stdio`.

### Build targets

```bash
docker build --target slim -t database-mcp-connector:slim .   # the default
docker build --target full -t database-mcp-connector:full .
```

| target | engines | why the split |
|---|---|---|
| `slim` | sqlite, postgres, mysql/mariadb, mongodb, mcp | pure python wheels, no OS packages |
| `full` | the above plus mssql, datalake | pyodbc needs unixODBC and Microsoft's own driver installed at the OS level |

Two targets rather than one image per engine: which engine a container serves is
a run-time choice, so an image only has to carry the drivers, not the decision.

**No connection detail is ever a build argument.** A build argument survives in
`docker history`. Hosts, users and passwords are run-time environment only.

The image also exposes the CLI directly, for anything the two roles do not
cover — `docker run --rm <image> token issue …`, or any other command.

### Without Docker

```bash
uv sync --extra server
export SHOP_PATH=./var/shop.db
uv run mcp-connector --engine sqlite --connection-ref shop --staging-db ./var/staging.db
```

`--extra server` makes the MCP layer importable. Each engine's driver is its own
extra: `postgres`, `mysql` (serves mariadb too), `mssql`, `mongo`, `datalake`,
`mcp`. sqlite needs none. For mssql the ODBC driver itself is not a python
package and has to be installed separately.

## Configure

### The connection

Connection details are **not** flags. They live in environment variables under a
prefix you choose, and `--connection-ref` names the prefix:

```bash
export SHOP_HOST=db.internal SHOP_USER=readonly SHOP_PASSWORD=… SHOP_DB=shop
uv run mcp-connector --engine postgres --connection-ref shop
```

Recognised suffixes are `_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_DB` /
`_DATABASE`, `_URI`, `_PATH` and `_TOKEN`. A repo-local `.env` is loaded
automatically; under compose, `.env` is also what fills in the `${...}` in
`docker-compose.yml`. See [.env.example](.env.example).

| engine | needs | instead of, or as well |
|---|---|---|
| `sqlite` | `_PATH` (a `.db` file) | `_URI` |
| `datalake` | `_URI` (`s3://…`, `gs://…`) or `_PATH` | |
| `postgres` | `_HOST`, `_USER`, `_PASSWORD`, `_DB` | `_URI` — a whole libpq conninfo, and the only way to reach a unix socket |
| `mysql` / `mariadb` | `_HOST`, `_USER`, `_PASSWORD`, `_DB` | `_URI` (`mysql://user:pass@host:port/db`) |
| `mssql` | `_HOST`, `_USER`, `_PASSWORD`, `_DB` | `_URI` — a whole ODBC connection string |
| `mongodb` | `_URI` (`mongodb://…`) or `_HOST`, plus `_DB` | the url's path counts as `_DB` |
| `mcp` | `_URI` (`http://…`) or `_PATH`, plus `_TOKEN` | |

`_DB` is what `--database` overrides. For postgres, mysql, mssql and mongodb it
selects one database on a server that has several; asking for another opens a
second connection, which the pool keeps beside the first.

The mssql connection string is built with `Encrypt=yes` and the certificate
checked. A self-signed certificate needs `TrustServerCertificate=yes`, and the
way to say so is a whole `<REF>_URI` — a decision to take on purpose, not a
default to inherit.

### Everything else

Every other setting has a flag and an `MCP_*` variable. The flag wins.

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
| `--authorized-keys-dir` | `MCP_AUTHORIZED_KEYS_DIR` | unset |
| `--audience` | `MCP_AUDIENCE` | `etl-agent-mcp` |
| `--allow-insecure-http` | `MCP_ALLOW_INSECURE_HTTP` | `false` |

`--staging-db` is off by default because without somewhere to accumulate a scan
there is nowhere to put what it gathers, so the inventory tools are not served
at all rather than served and broken. It must not point at the database being
inventoried; the store refuses that.

`--profile-mode` applies the modes you name to every column. Left unset, each
column gets what its type warrants — a range for a number or a date, the
commonest values for a categorical one, and nothing beyond a null ratio for a
type that means nothing to us.

## Connect an agent

`provision` generates the client side for you: an `.mcp.json` with the server
entry, and a `SKILL.md` telling the agent how to use it. Both are generated
against the actual deployment — the tool list is read from the server's own
source, and what the agent is told it may call comes from the token that was
just signed, so the two cannot disagree. Copy `out/<kid>/` into
`~/.claude/plugins/`, or point a marketplace at it.

By hand, for a network transport:

```json
{
  "mcpServers": {
    "shop-catalog": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer ${SHOP_CATALOG_TOKEN}" }
    }
  }
}
```

Keep the token a `${VAR}` rather than pasting it in. This file ends up in a
project, and a bearer token in a project is a credential in every copy of it.

## Authentication

Only for the network transports.

`mcp.json` can carry a static header but cannot compute a signature, so signing
happens outside the serving process. The server holds **public keys only** — it
can verify a token and cannot mint one, so taking the server does not get you
the ability to issue yourself a new key.

This is signing, not encryption. The token is a JWT: its payload is plain
readable text, and what the signature buys is that it cannot be forged.

```bash
# once per agent — the public half is filed where the server reads it
uv run mcp-connector token keygen --kid pm-explorer --keys-dir ./keys --out ./pm-explorer.pem

# per token
uv run mcp-connector token issue --key ./pm-explorer.pem --kid pm-explorer \
    --scope list_containers --scope get_schema --lifetime 30d

# serve
uv run mcp-connector --engine sqlite --connection-ref shop \
    --transport streamable-http --host 0.0.0.0 \
    --require-auth --authorized-keys-dir ./keys
```

The agent holds only the resulting token. The private key never reaches the
server, and the public key never reaches the agent.

| claim | what it does |
|---|---|
| `kid` (header) | names the public key in `--authorized-keys-dir` that verifies it |
| `sub` | who the caller is, and what the audit trail records |
| `aud` | must equal `--audience`, so a token signed for elsewhere is refused |
| `exp` | required; expiry is the main way a token stops working |
| `scopes` | the tools it may call, by name. No scopes, no tools. |

**Revoking** is `rm ./keys/<kid>.pub` — no restart, effective within 30 seconds,
which is how often the directory is re-read.

`exp` is checked with 60 seconds of leeway, against clock skew. It matters only
if you issue very short lifetimes: a `--lifetime 5m` token is good for six.

Signatures are asymmetric only (EdDSA, ES256, RS256); an HMAC algorithm is never
accepted, since the keys here are public.

### What a key may see

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
asked for directly — an agent told a table is not there goes looking for it, one
told it may not look asks for access — and left out of listings, searches and
exports, so the catalog's shape does not leak to someone who cannot read it.

Authentication is not authorisation: a valid token still gets a tool error for a
tool outside its scopes, and the refusal is recorded in the audit trail like any
other call.

## Using it well

### Reading a catalog that will not fit

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

A path given to it is relative to `--export-dir` and cannot leave it; `../` and
symlinks are resolved before the check, because the caller is an agent relaying
a path someone gave it.

### Personal data

`get_sample` puts real rows into an agent's context, and from there into every
log, transcript and history that context touches. So **rows are masked by
default**:

```
{"id": 1, "email": "a***@***.com", "note": "hello", "api_key": "***"}
```

The shape survives where it is useful; a secret keeps nothing, because there is
no shape worth showing.

Which columns count is decided by the scan, from the column name first and then
from a small sample of values. Only the verdict is stored, never the values it
was reached from. It is a guess, and `inventory_annotate` overrides it for good
— a later scan never overrules a verdict a person wrote:

```json
{"column": "internal_ref", "sensitivity": "pii"}
```

`mask=False` returns the rows as they are. Over stdio that is allowed. Over an
authenticated transport the key needs `--allow-raw-sample`, and the audit trail
records that it happened.

### Descriptions

The inventory keeps two kinds of description, in separate columns:

- **`native_description`** — the source's own comment. A scan overwrites it, and
  it is part of the fingerprint, so a comment edited upstream triggers a
  rescan. sqlite has no comments at all.
- **`description`** — written through `inventory_annotate`. **A scan never
  touches it**, and it stays out of the fingerprint so that writing one cannot
  trigger its own rescan.

`inventory_annotate` is the only tool that writes, and it writes to the
inventory alone — the source database is never touched. Who a description came
from is decided by the server from the caller's key, not claimed by the caller:
`human` needs a token carrying that grant, everything else is `ai`.

The staging file carries its layout version and one written by an older build is
refused rather than migrated. An inventory is derived data that a rescan
reproduces, so the only thing genuinely lost is what was annotated.

### What a container is called

A container is one string, and it has to identify one container. On postgres and
mssql it therefore carries the schema — `public.users`, `dbo.orders`. A bare
`users` still works where exactly one schema has one; where several do, the call
is refused with the list rather than answered from whichever came first.

sqlite, mysql and mongodb have no schema layer below the database, so their
containers are named plainly. mysql's `SCHEMA` is a synonym for `DATABASE`, and
passing one that is not the connected database is refused rather than ignored.

### Collections have no schema

mongodb has no declared schema, so `get_schema` **infers** one from the first
hundred documents of a collection. It describes the sample, not the collection:

- a field seen holding more than one type is reported as all of them, `int|string`
- a field missing from a document counts as null in that document
- only top-level fields are reported; a nested document is `object`, an array is
  `array`
- profiling a field no sampled document carried is refused, rather than answered
  with a null ratio of 1.0 that reads as a fact about the collection

## Develop

```bash
uv sync --all-extras --dev
uv run pytest
uvx ruff@0.4.4 format . && uvx ruff@0.4.4 check .
uvx pyright
```

Two rules are enforced by tests rather than by convention:
`tests/test_layering.py` fails if `core` imports an implementation, and
`tests/test_context_budget.py` builds a 500-table catalog and asserts what each
tool costs.
