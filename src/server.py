from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from loggerhelper import log
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

from src.auth import verifier_from_config
from src.auth.permissions import Permissions
from src.core.config import ServerConfig
from src.core.contracts import (
    ColumnInfo,
    ContainerPage,
    ProfileMode,
    ProfileResult,
    Sensitivity,
    SourceAdaptor,
)
from src.service import sensitivity
from src.service.audit import AuditLogger
from src.service.export import (
    ExportError,
    ExportFormat,
    ExportResult,
    export_inventory,
    under_root,
)
from src.service.inventory import InventoryService, ScanStatus
from src.service.pool import AdapterPool, AdapterProvider, SingleAdapter
from src.service.staging import (
    DEFAULT_CHANGE_LIMIT,
    DEFAULT_COLUMN_PAGE,
    DEFAULT_SEARCH_LIMIT,
    SOURCE_AI,
    SOURCE_HUMAN,
    AnnotateResult,
    ColumnAnnotation,
    ContainerAnnotation,
    InventorySummary,
    Relationship,
    SchemaChange,
    SearchHit,
    StoredColumnPage,
    StoredContainerPage,
)

# No token means stdio: the client spawned us and holds our environment.
LOCAL_KEY_ID = "local"

# Tool name -> the caller's id, or a refusal. `_identity` with the server's
# authentication requirement already bound in.
Identify = Callable[[str], tuple[str, Permissions]]

# A key may only claim its descriptions are a person's if it carries this.
HUMAN_ANNOTATION_SCOPE = "annotate:human"

# Where the export directory is served from over http. Not `/`-rooted anywhere
# near the MCP endpoint, and never a directory listing: one file at a time, by
# the name an export already handed back.
DOWNLOAD_PREFIX = "/export"

# Only what an export can produce. Anything else is a file this server did not
# write, and it is not this route's job to guess how to render it.
_DOWNLOAD_TYPES: dict[str, str] = {
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".yml": "application/yaml; charset=utf-8",
}


def build_server(
    config: ServerConfig,
    source: AdapterProvider | SourceAdaptor,
    *,
    audit: AuditLogger | None = None,
    inventory: InventoryService | None = None,
) -> FastMCP:
    """Wire the tools onto a source. Starts nothing; `main` picks the transport.
    The inventory tools appear only when a service is given."""
    # Built here rather than passed in, so that a server configured to require
    # authentication cannot be constructed without it. The config validator has
    # already ruled out require_auth over stdio.
    auth = verifier_from_config(config) if config.require_auth else None

    # a provider has `get`; a bare adapter does not
    provider = source if isinstance(source, AdapterProvider) else SingleAdapter(source)
    trail = audit or AuditLogger(
        config.audit_log_path,
        max_bytes=config.audit_max_mb * 1024 * 1024,
        backups=config.audit_backups,
    )

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncGenerator[None]:
        if isinstance(provider, AdapterPool):
            provider.start_reaper()
        log.info(f"{config.server_name} ready ({config.transport})")
        try:
            yield
        finally:
            if inventory is not None:
                inventory.close()
            provider.close()
            log.info(f"{config.server_name} stopped")

    # Bound once: every tool asks the same question, and whether an unauthenticated
    # call is acceptable is a property of the server, not of the tool.
    identify = partial(_caller, require_auth=config.require_auth)

    mcp: FastMCP = FastMCP(name=config.server_name, auth=auth, lifespan=lifespan)
    _register_tools(mcp, config, provider, trail, identify, inventory)
    if inventory is not None:
        _register_inventory_tools(mcp, config, inventory, trail, identify)
        _register_export_route(mcp, config, trail)
    return mcp


def _identity(tool: str, *, require_auth: bool) -> str:
    """
    Caller's id, once its scopes are known to cover this tool.

    No token means stdio, where the caller already has this process's
    environment and so already has the database credential — there is nothing
    left for a check to protect. A server that requires authentication refuses
    instead: the transport should have turned that call away long before here,
    and if it did not, answering it would be the one bug that matters.

    `require_auth` has no default on purpose. It is always bound in `partial`
    at registration, and a security check that can be skipped by forgetting a
    keyword is one that eventually will be.
    """
    return _caller(tool, require_auth=require_auth)[0]


def _caller(tool: str, *, require_auth: bool) -> tuple[str, Permissions]:
    """
    Who is calling and what they are allowed, or a refusal.

    One lookup for both, because every check after this one — which database,
    which container, raw rows or masked — asks the same token the same
    question, and reading it twice invites the two answers to drift.
    """
    token = get_access_token()
    if token is None:
        if require_auth:
            raise ToolError(f"{tool} requires an authenticated caller")
        return LOCAL_KEY_ID, Permissions.local()

    # `claims` is optional on the SDK's own token type, so it is read rather
    # than assumed: another auth provider may hand one over without it.
    rights = Permissions.from_claims(
        list(token.scopes or []), getattr(token, "claims", None) or {}
    )
    name = token.subject or token.client_id or LOCAL_KEY_ID
    if not rights.may_call(tool):
        raise ToolError(f"{name} may not call {tool}")
    return name, rights


def _require_access(
    name: str, rights: Permissions, *, database: str | None, container: str | None
) -> None:
    """
    Whether this key may look at this thing at all.

    Refused by name rather than silently emptied: an agent told a table is not
    there will go looking for it elsewhere, and an agent told it may not look
    will ask someone for access.
    """
    if not rights.may_use_database(database):
        raise ToolError(f"{name} may not read database {database!r}")
    if container is not None and not rights.may_read(container):
        raise ToolError(f"{name} may not read {container!r}")


def _annotation_source(rights: Permissions | None = None) -> str:
    """
    Who a description came from — decided here, never taken from the caller.

    An agent that could label its own guesses `human` would make the field
    worthless, so a description counts as a person's only when the key that
    carried it was granted that.
    """
    if rights is None:
        token = get_access_token()
        if token is None:
            return SOURCE_AI
        rights = Permissions.from_claims(
            list(token.scopes or []), getattr(token, "claims", None) or {}
        )
    return SOURCE_HUMAN if rights.annotate_as_human else SOURCE_AI


def _known_sensitivity(
    inventory: InventoryService | None,
    provider: AdapterProvider,
    database: str | None,
    container: str,
) -> dict[str, Sensitivity]:
    """
    What the inventory already decided about this container's columns, which
    beats deciding again from a handful of rows — it may have been corrected by
    a person.

    Empty when there is no inventory, which is not a gap: masking then judges
    the sample in front of it, so a server with no staging database still does
    not put raw email addresses into a context.
    """
    if inventory is None:
        return {}
    try:
        # The tools take `database` optionally and the inventory is keyed by
        # the real name, so an omitted one has to be resolved rather than
        # quietly missing every recorded verdict.
        names = provider.list_databases() if database is None else [database]
        if not names:
            return {}
        return inventory.store.sensitivity_of(names[0], container)
    except Exception as exc:  # noqa: BLE001 - never fail a read over this
        log.warning(f"cannot read recorded sensitivity for {container!r}: {exc}")
        return {}


def _register_tools(
    mcp: FastMCP,
    config: ServerConfig,
    provider: AdapterProvider,
    trail: AuditLogger,
    identify: Identify,
    inventory: InventoryService | None,
) -> None:
    def _adapter(database: str | None) -> SourceAdaptor:
        try:
            return provider.get(database)
        except Exception as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    def list_databases() -> list[str]:
        """Databases this connection can inventory."""
        key_id, rights = identify("list_databases")
        with trail.operation(key_id=key_id, tool="list_databases", params={}) as ctx:
            result = [
                name
                for name in provider.list_databases()
                if rights.may_use_database(name)
            ]
            ctx.rows_returned = len(result)
            return result

    @mcp.tool
    def list_containers(
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage:
        """
        One page of tables / views / collections. Pass `next_cursor` back as
        `cursor` to continue; it is None on the last page.
        """
        key_id, rights = identify("list_containers")
        _require_access(key_id, rights, database=database, container=None)
        adapter = _adapter(database)
        params = {
            "database": database,
            "schema": schema,
            "limit": limit,
            "cursor": cursor,
        }
        with trail.operation(
            key_id=key_id, tool="list_containers", params=params
        ) as ctx:
            page = _guard(adapter.list_containers)(
                database=database, schema=schema, limit=limit, cursor=cursor
            )
            # Filtered, not refused: a page is a listing, and the names a key
            # may not read are not its business either. The cursor still comes
            # from the unfiltered page, so paging does not stall on a run of
            # containers this key cannot see.
            page.containers = [
                info for info in page.containers if rights.may_read(info.container_name)
            ]
            ctx.rendered_sql = adapter.pop_rendered_sql()
            ctx.rows_returned = len(page.containers)
            return page

    @mcp.tool
    def get_schema(container: str, database: str | None = None) -> list[ColumnInfo]:
        """Columns of one container, with key and nullability flags."""
        key_id, rights = identify("get_schema")
        _require_access(key_id, rights, database=database, container=container)
        adapter = _adapter(database)
        with trail.operation(
            key_id=key_id,
            tool="get_schema",
            params={"container": container, "database": database},
        ) as ctx:
            columns = _guard(adapter.get_schema)(container)
            ctx.rendered_sql = adapter.pop_rendered_sql()
            ctx.rows_returned = len(columns)
            return columns

    @mcp.tool
    def get_sample(
        container: str,
        limit: int = 3,
        database: str | None = None,
        mask: bool = True,
    ) -> list[dict[str, Any]]:
        """
        A few rows, capped by the server's max_sample_limit.

        Personal data is masked by default — `a***@***.com` — because these
        rows go into an agent's context and from there into everything that
        context touches. `mask=False` returns them as they are, and a key
        needs to have been granted that.
        """
        key_id, rights = identify("get_sample")
        _require_access(key_id, rights, database=database, container=container)
        if not mask and not rights.allow_raw_sample:
            raise ToolError(
                f"{key_id} may not read unmasked rows; call without mask=False"
            )
        adapter = _adapter(database)
        capped = max(0, min(limit, config.max_sample_limit))
        with trail.operation(
            key_id=key_id,
            tool="get_sample",
            params={
                "container": container,
                "limit": capped,
                "database": database,
                "mask": mask,
            },
        ) as ctx:
            rows = _guard(adapter.get_sample)(container, limit=capped)
            ctx.rendered_sql = adapter.pop_rendered_sql()
            ctx.rows_returned = len(rows)
            if not mask:
                # Worth being able to answer "who has seen raw rows, and of
                # what" without reading the whole trail for it.
                ctx.extra["unmasked"] = True
                return rows
            hidden = sensitivity.sensitive_columns(
                rows, _known_sensitivity(inventory, provider, database, container)
            )
            ctx.extra["masked_columns"] = sorted(hidden)
            return sensitivity.mask_rows(rows, hidden)

    @mcp.tool
    def profile_column(
        container: str,
        column: str,
        mode: ProfileMode,
        database: str | None = None,
    ) -> ProfileResult:
        """
        One statistic about one column. `approximate` is True when the source
        stopped short of a full scan.
        """
        key_id, rights = identify("profile_column")
        _require_access(key_id, rights, database=database, container=container)
        adapter = _adapter(database)
        with trail.operation(
            key_id=key_id,
            tool="profile_column",
            params={
                "container": container,
                "column": column,
                "mode": str(mode),
                "database": database,
            },
        ) as ctx:
            result = _guard(adapter.profile_column)(container, column, mode)
            ctx.rendered_sql = adapter.pop_rendered_sql()
            return result


def _register_inventory_tools(
    mcp: FastMCP,
    config: ServerConfig,
    inventory: InventoryService,
    trail: AuditLogger,
    identify: Identify,
) -> None:
    store = inventory.store

    @mcp.tool
    def inventory_start(
        database: str | None = None,
        profile_modes: list[ProfileMode] | None = None,
        force: bool = False,
        resume: bool = True,
    ) -> str:
        """
        Start a background catalog scan and return its job id; poll
        `inventory_status`. Unchanged containers are skipped unless `force`, and
        an unfinished run continues from its cursor unless `resume` is False.
        """
        key_id, rights = identify("inventory_start")
        _require_access(key_id, rights, database=database, container=None)
        params = {"database": database, "force": force, "resume": resume}
        with trail.operation(
            key_id=key_id, tool="inventory_start", params=params
        ) as ctx:
            job_id = _guard(inventory.start)(
                database, profile_modes=profile_modes, force=force, resume=resume
            )
            ctx.extra["job_id"] = job_id
            return job_id

    @mcp.tool
    def inventory_status(job_id: str) -> ScanStatus:
        """How far a scan has got, and why it stopped."""
        key_id, rights = identify("inventory_status")
        with trail.operation(
            key_id=key_id, tool="inventory_status", params={"job_id": job_id}
        ):
            return _guard(inventory.status)(job_id)

    @mcp.tool
    def inventory_cancel(job_id: str) -> bool:
        """Stop a scan after the container in flight. Progress is kept."""
        key_id, rights = identify("inventory_cancel")
        with trail.operation(
            key_id=key_id, tool="inventory_cancel", params={"job_id": job_id}
        ):
            return _guard(inventory.cancel)(job_id)

    @mcp.tool
    def inventory_summary(database: str | None = None) -> InventorySummary:
        """Counts over what has been inventoried. Ask for this before the rows."""
        key_id, rights = identify("inventory_summary")
        _require_access(key_id, rights, database=database, container=None)
        with trail.operation(
            key_id=key_id, tool="inventory_summary", params={"database": database}
        ):
            return _guard(store.summary)(database)

    @mcp.tool
    def inventory_containers(
        database: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> StoredContainerPage:
        """One page of inventoried containers, newest scan state included."""
        key_id, rights = identify("inventory_containers")
        _require_access(key_id, rights, database=database, container=None)
        params = {"database": database, "limit": limit, "cursor": cursor}
        with trail.operation(
            key_id=key_id, tool="inventory_containers", params=params
        ) as ctx:
            page = _guard(store.containers)(database, limit=limit, cursor=cursor)
            page.containers = [
                row for row in page.containers if rights.may_read(row.container_name)
            ]
            ctx.rows_returned = len(page.containers)
            return page

    @mcp.tool
    def inventory_columns(
        database: str,
        container: str | None = None,
        schema: str | None = None,
        limit: int = DEFAULT_COLUMN_PAGE,
        cursor: str | None = None,
        include_profile: bool = True,
        only_missing_description: bool = False,
    ) -> StoredColumnPage:
        """
        One page of recorded columns, in ordinal order.

        Pass `next_cursor` back as `cursor` to continue. On a wide table the
        profiles are most of the weight, so `include_profile=False` is the way
        to read its shape without them.

        Without a `container` the page spans the whole database — that, with
        `only_missing_description=True` and `include_profile=False`, is how to
        describe a catalog: read a page of what is still undescribed, write it
        back with `inventory_annotate`, continue from the cursor. One table at
        a time costs a round trip per table and reads statistics that
        describing them does not use.

        A cursor belongs to the shape of page that produced it; one from a
        single-container page cannot continue a database-wide one.
        """
        key_id, rights = identify("inventory_columns")
        _require_access(key_id, rights, database=database, container=container)
        params = {
            "container": container,
            "database": database,
            "schema": schema,
            "limit": limit,
            "cursor": cursor,
            "include_profile": include_profile,
            "only_missing_description": only_missing_description,
        }
        with trail.operation(
            key_id=key_id, tool="inventory_columns", params=params
        ) as ctx:
            page = _guard(store.columns)(
                database,
                container,
                schema,
                limit=limit,
                cursor=cursor,
                include_profile=include_profile,
                only_missing_description=only_missing_description,
            )
            # Named container: `_require_access` already settled it. Without
            # one the page spans containers this key may not read, so it is
            # filtered here for the same reason the listings are — and the
            # cursor still comes from the unfiltered page, so a run of denied
            # containers does not stall the paging.
            if container is None:
                page.columns = [
                    column
                    for column in page.columns
                    if rights.may_read(column.container_name)
                ]
            ctx.rows_returned = len(page.columns)
            return page

    @mcp.tool
    def inventory_relationships(database: str | None = None) -> list[Relationship]:
        """
        Every foreign key in the inventory, as edges.

        "How do these two tables join" is the first question anyone asks of a
        database they did not build. Enough to draw an ER diagram from.
        """
        key_id, rights = identify("inventory_relationships")
        _require_access(key_id, rights, database=database, container=None)
        with trail.operation(
            key_id=key_id,
            tool="inventory_relationships",
            params={"database": database},
        ) as ctx:
            edges = _guard(store.relationships)(database)
            ctx.rows_returned = len(edges)
            return edges

    @mcp.tool
    def inventory_changes(
        database: str | None = None,
        since: str | None = None,
        limit: int = DEFAULT_CHANGE_LIMIT,
    ) -> list[SchemaChange]:
        """
        What the upstream schema did between scans, newest first.

        A container appearing or disappearing, and the columns added, removed
        or retyped within one. `since` is an ISO timestamp.
        """
        key_id, rights = identify("inventory_changes")
        _require_access(key_id, rights, database=database, container=None)
        params = {"database": database, "since": since, "limit": limit}
        with trail.operation(
            key_id=key_id, tool="inventory_changes", params=params
        ) as ctx:
            changes = _guard(store.changes)(database, since=since, limit=limit)
            ctx.rows_returned = len(changes)
            return changes

    @mcp.tool
    def inventory_search(
        keyword: str,
        database: str | None = None,
        kind: Literal["all", "container", "column"] = "all",
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[SearchHit]:
        """
        Containers and columns whose name or description mentions `keyword`.

        Where to start when the catalog is too big to read: ask this, then
        `inventory_columns` for the one container that looked right. Hits carry
        no statistics, so a hundred of them still cost little.
        """
        key_id, rights = identify("inventory_search")
        _require_access(key_id, rights, database=database, container=None)
        params = {
            "keyword": keyword,
            "database": database,
            "kind": kind,
            "limit": limit,
        }
        with trail.operation(
            key_id=key_id, tool="inventory_search", params=params
        ) as ctx:
            hits = [
                hit
                for hit in _guard(store.search)(
                    keyword, database, kind=kind, limit=limit
                )
                if rights.may_read(hit.container_name)
            ]
            ctx.rows_returned = len(hits)
            return hits

    @mcp.tool
    def inventory_annotate(
        database: str,
        container: str | None = None,
        schema: str | None = None,
        container_description: str | None = None,
        columns: list[ColumnAnnotation] | None = None,
        containers: list[ContainerAnnotation] | None = None,
    ) -> AnnotateResult:
        """
        Describe what inventoried tables and their columns actually hold.

        The only tool here that writes, and it writes to the inventory alone —
        the source database is never touched. A rescan keeps what is written
        here; a field left out is left as it was, and a blank one clears it.
        Column names that are not in the inventory come back in
        `unknown_columns` instead of being ignored.

        One table at a time with `container`, or a whole batch with
        `containers` — the counterpart of reading a page of columns that spans
        tables. A batch is one transaction, and a name that was never
        inventoried comes back in `unknown_containers` rather than costing the
        rest of the batch its writes.
        """
        key_id, rights = identify("inventory_annotate")
        # Named separately in the message: an agent that sent both usually
        # means the batch, and one that sent neither has built an empty call
        # it will otherwise report as a write.
        if (container is None) == (containers is None):
            raise ToolError(
                "inventory_annotate takes either `container` (one table) or "
                "`containers` (a batch), not both and not neither"
            )
        source = _annotation_source(rights)
        batch = containers or [
            ContainerAnnotation(
                container=str(container),
                schema_name=schema,
                container_description=container_description,
                columns=columns or [],
            )
        ]
        # Every name in the batch, not just the first: this is the check that
        # keeps a key out of the tables it may not read, and a batch is exactly
        # the shape that would smuggle one past a check that looked once.
        for item in batch:
            _require_access(key_id, rights, database=database, container=item.container)
        params = {
            "database": database,
            "container": container,
            "containers": [item.container for item in batch] if containers else None,
            "schema": schema,
            "source": source,
        }
        with trail.operation(
            key_id=key_id, tool="inventory_annotate", params=params
        ) as ctx:
            if containers is None:
                result = _guard(store.annotate)(
                    database,
                    str(container),
                    schema,
                    container_description=container_description,
                    columns=columns or (),
                    source=source,
                )
            else:
                result = _guard(store.annotate_many)(database, batch, source=source)
            # what a write cost, the counterpart of rows_returned for a read
            ctx.extra["columns_written"] = result.columns_updated
            ctx.extra["containers_written"] = len(batch) - len(
                result.unknown_containers
            )
            return result

    # Last, and only with somewhere to write: an export has nowhere to go
    # otherwise, so the tool is not served at all rather than served and failing
    # on every call.
    if config.export_dir is None:
        log.info("no export directory configured; inventory_export stays off")
        return

    @mcp.tool
    def inventory_export(
        format: ExportFormat = "markdown",
        database: str | None = None,
        path: str | None = None,
    ) -> ExportResult:
        """
        Write the whole inventory to a file and return **only where it went**.

        This is how a full sweep is delivered: the catalog itself never travels
        through a tool result. `markdown` is a data dictionary to read, `csv` a
        row per column, `dbt_yaml` a `schema.yml` for a dbt project. `path` is
        relative to the server's export directory and cannot leave it.

        Over http the result also carries where to fetch the file from, which
        is what the person who asked for it actually needs — the file is on the
        server, and they are not.
        """
        key_id, rights = identify("inventory_export")
        params = {"format": format, "database": database, "path": path}
        with trail.operation(
            key_id=key_id, tool="inventory_export", params=params
        ) as ctx:
            result = _guard(export_inventory)(
                store,
                config.export_dir,
                format=format,
                database=database,
                path=path,
                permits=rights.may_read,
            )
            _attach_download(result, config)
            ctx.rows_returned = result.columns
            ctx.extra["bytes_written"] = result.bytes_written
            return result


def _attach_download(result: ExportResult, config: ServerConfig) -> None:
    """
    Say where the file can be fetched from, when there is anywhere to fetch it.

    Filled here rather than in `export_inventory`, which has no business
    knowing what path this server is mounted at — it writes files, and would
    have to be told about http to answer this.
    """
    if config.export_dir is None or config.transport == "stdio":
        return
    try:
        relative = Path(result.path).relative_to(Path(config.export_dir).resolve())
    except ValueError:  # written outside the directory: nothing to serve
        return
    result.download_path = f"{DOWNLOAD_PREFIX}/{relative.as_posix()}"
    if config.public_url:
        result.download_url = f"{config.public_url.rstrip('/')}{result.download_path}"


def _register_export_route(
    mcp: FastMCP, config: ServerConfig, trail: AuditLogger
) -> None:
    """
    Serve the export directory over the same port, to the same tokens.

    An export writes a file the person who asked for it cannot reach: the
    server is on someone else's machine, and `docker cp` needs a shell there.
    So the file is served — but only the file, only under the export directory,
    and only to a key that could have produced it in the first place.

    Not an MCP tool, deliberately. A tool result travels into the caller's
    context, and the whole point of an export is that the catalog does not.
    """
    export_dir = config.export_dir
    if export_dir is None or config.transport == "stdio":
        return

    @mcp.custom_route(f"{DOWNLOAD_PREFIX}/{{name:path}}", methods=["GET"])
    async def download_export(request: Request) -> Response:
        # FastMCP wraps only the MCP endpoints in RequireAuthMiddleware; the
        # app-level middleware authenticates a Bearer if one is there but lets
        # a request with none through to here. So this route does its own
        # refusing, and must: it is reachable by anyone who can reach the port.
        user = request.scope.get("user")
        token = getattr(user, "access_token", None)
        if token is None:
            if config.require_auth:
                return PlainTextResponse(
                    "this export requires an authenticated caller\n",
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            # No auth required means stdio's bargain over http on purpose
            # (`allow_insecure_http`): there is nothing here to protect that
            # the tools were protecting either.
            key_id, rights = LOCAL_KEY_ID, Permissions.local()
        else:
            rights = Permissions.from_claims(
                list(token.scopes or []), getattr(token, "claims", None) or {}
            )
            key_id = token.subject or token.client_id or LOCAL_KEY_ID

        if not rights.may_call("inventory_export"):
            return PlainTextResponse(
                f"{key_id} may not download exports\n", status_code=403
            )

        name = request.path_params["name"]
        with trail.operation(
            key_id=key_id, tool="inventory_export_download", params={"path": name}
        ) as ctx:
            try:
                target = under_root(export_dir, name)
                readable = target.is_file()
            except ExportError as exc:
                # Logged, not answered. Telling a caller apart "outside the
                # directory" from "not there" hands them a way to map the
                # filesystem one request at a time, and the same reasoning
                # keeps the token verifier quiet about why it refused.
                log.warning(f"refused export download of {name!r}: {exc}")
                readable = False
                target = None
            if target is None or not readable:
                ctx.extra["refused"] = True
                return PlainTextResponse("no such export\n", status_code=404)

            # Recorded before the response leaves: FileResponse streams after
            # this block has closed, so anything measured later is measured
            # after the audit record is already written.
            ctx.extra["bytes_sent"] = target.stat().st_size
            return FileResponse(
                target,
                # `filename` is what makes this a download rather than
                # something a browser renders. These files carry a client's
                # table and column names; they should land on a disk, not in a
                # tab.
                filename=target.name,
                media_type=_DOWNLOAD_TYPES.get(
                    target.suffix, "application/octet-stream"
                ),
            )


def _guard(func: Any) -> Any:
    """An adapter failure becomes a readable tool error, not a traceback."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return wrapper
