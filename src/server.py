from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

from src.core.config import ServerConfig
from src.core.contracts import (
    ColumnInfo,
    ContainerPage,
    ProfileMode,
    ProfileResult,
    SourceAdaptor,
)
from src.core.log import log
from src.service.audit import AuditLogger
from src.service.inventory import InventoryService, ScanStatus
from src.service.pool import AdapterPool, AdapterProvider, SingleAdapter
from src.service.staging import (
    SOURCE_AI,
    SOURCE_HUMAN,
    AnnotateResult,
    ColumnAnnotation,
    InventorySummary,
    StoredColumn,
    StoredContainerPage,
)

# No token means stdio: the client spawned us and holds our environment.
LOCAL_KEY_ID = "local"

# A key may only claim its descriptions are a person's if it carries this.
HUMAN_ANNOTATION_SCOPE = "annotate:human"


def build_server(
    config: ServerConfig,
    source: AdapterProvider | SourceAdaptor,
    *,
    audit: AuditLogger | None = None,
    inventory: InventoryService | None = None,
) -> FastMCP:
    """Wire the tools onto a source. Starts nothing; `main` picks the transport.
    The inventory tools appear only when a service is given."""
    if config.require_auth:
        raise NotImplementedError(
            "require_auth=True but no auth provider is implemented yet; see "
            "docs/authentication.md"
        )

    # a provider has `get`; a bare adapter does not
    provider = source if isinstance(source, AdapterProvider) else SingleAdapter(source)
    trail = audit or AuditLogger(config.audit_log_path)

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

    mcp: FastMCP = FastMCP(name=config.server_name, auth=None, lifespan=lifespan)
    _register_tools(mcp, config, provider, trail)
    if inventory is not None:
        _register_inventory_tools(mcp, inventory, trail)
    return mcp


def _identity(tool: str) -> str:
    """Caller's id, once its scopes are known to cover this tool."""
    token = get_access_token()
    if token is None:
        return LOCAL_KEY_ID
    if tool not in (token.scopes or []):
        raise ToolError(f"{token.subject or token.client_id} may not call {tool}")
    return token.subject or token.client_id or LOCAL_KEY_ID


def _annotation_source() -> str:
    """
    Who a description came from — decided here, never taken from the caller.

    An agent that could label its own guesses `human` would make the field
    worthless, so a description counts as a person's only when the key that
    carried it was granted that scope.
    """
    token = get_access_token()
    if token is not None and HUMAN_ANNOTATION_SCOPE in (token.scopes or []):
        return SOURCE_HUMAN
    return SOURCE_AI


def _register_tools(
    mcp: FastMCP,
    config: ServerConfig,
    provider: AdapterProvider,
    trail: AuditLogger,
) -> None:
    def _adapter(database: str | None) -> SourceAdaptor:
        try:
            return provider.get(database)
        except Exception as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    def list_databases() -> list[str]:
        """Databases this connection can inventory."""
        with trail.operation(
            key_id=_identity("list_databases"), tool="list_databases", params={}
        ) as ctx:
            result = provider.list_databases()
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
        key_id = _identity("list_containers")
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
            ctx.rendered_sql = adapter.pop_rendered_sql()
            ctx.rows_returned = len(page.containers)
            return page

    @mcp.tool
    def get_schema(container: str, database: str | None = None) -> list[ColumnInfo]:
        """Columns of one container, with key and nullability flags."""
        key_id = _identity("get_schema")
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
        container: str, limit: int = 3, database: str | None = None
    ) -> list[dict[str, Any]]:
        """A few rows, capped by the server's max_sample_limit."""
        key_id = _identity("get_sample")
        adapter = _adapter(database)
        capped = max(0, min(limit, config.max_sample_limit))
        with trail.operation(
            key_id=key_id,
            tool="get_sample",
            params={"container": container, "limit": capped, "database": database},
        ) as ctx:
            rows = _guard(adapter.get_sample)(container, limit=capped)
            ctx.rendered_sql = adapter.pop_rendered_sql()
            ctx.rows_returned = len(rows)
            return rows

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
        key_id = _identity("profile_column")
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
    mcp: FastMCP, inventory: InventoryService, trail: AuditLogger
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
        key_id = _identity("inventory_start")
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
        key_id = _identity("inventory_status")
        with trail.operation(
            key_id=key_id, tool="inventory_status", params={"job_id": job_id}
        ):
            return _guard(inventory.status)(job_id)

    @mcp.tool
    def inventory_cancel(job_id: str) -> bool:
        """Stop a scan after the container in flight. Progress is kept."""
        key_id = _identity("inventory_cancel")
        with trail.operation(
            key_id=key_id, tool="inventory_cancel", params={"job_id": job_id}
        ):
            return _guard(inventory.cancel)(job_id)

    @mcp.tool
    def inventory_summary(database: str | None = None) -> InventorySummary:
        """Counts over what has been inventoried. Ask for this before the rows."""
        key_id = _identity("inventory_summary")
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
        key_id = _identity("inventory_containers")
        params = {"database": database, "limit": limit, "cursor": cursor}
        with trail.operation(
            key_id=key_id, tool="inventory_containers", params=params
        ) as ctx:
            page = _guard(store.containers)(database, limit=limit, cursor=cursor)
            ctx.rows_returned = len(page.containers)
            return page

    @mcp.tool
    def inventory_columns(
        container: str, database: str, schema: str | None = None
    ) -> list[StoredColumn]:
        """Recorded columns of one container, with any profile gathered."""
        key_id = _identity("inventory_columns")
        params = {"container": container, "database": database, "schema": schema}
        with trail.operation(
            key_id=key_id, tool="inventory_columns", params=params
        ) as ctx:
            columns = _guard(store.columns)(database, container, schema)
            ctx.rows_returned = len(columns)
            return columns

    @mcp.tool
    def inventory_annotate(
        database: str,
        container: str,
        schema: str | None = None,
        container_description: str | None = None,
        columns: list[ColumnAnnotation] | None = None,
    ) -> AnnotateResult:
        """
        Describe what an inventoried table and its columns actually hold.

        The only tool here that writes, and it writes to the inventory alone —
        the source database is never touched. A rescan keeps what is written
        here; a field left out is left as it was, and a blank one clears it.
        Column names that are not in the inventory come back in
        `unknown_columns` instead of being ignored.
        """
        key_id = _identity("inventory_annotate")
        source = _annotation_source()
        params = {
            "database": database,
            "container": container,
            "schema": schema,
            "source": source,
        }
        with trail.operation(
            key_id=key_id, tool="inventory_annotate", params=params
        ) as ctx:
            result = _guard(store.annotate)(
                database,
                container,
                schema,
                container_description=container_description,
                columns=columns or (),
                source=source,
            )
            # what a write cost, the counterpart of rows_returned for a read
            ctx.extra["columns_written"] = result.columns_updated
            return result


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
