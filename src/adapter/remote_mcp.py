from __future__ import annotations

import asyncio
import dataclasses
import threading
from collections.abc import Coroutine
from typing import Any, ClassVar

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.exceptions import FastMCPError
from loggerhelper import log
from mcp.shared.exceptions import McpError

from src.adapter.base import AdapterBase
from src.core.config import ConnectionInfo
from src.core.contracts import (
    ColumnInfo,
    ContainerPage,
    ProfileMode,
    ProfileResult,
)


# HTTP 408, the code the mcp session reports when a request outlives its timeout.
_REQUEST_TIMEOUT = 408


class RemoteCallError(Exception):
    """A remote tool call failed. MCP has no structured error taxonomy, so a
    remote `UnknownContainerError` arrives as an opaque error."""


class _LoopRunner:
    """
    Owns an event loop on its own thread. FastMCP calls sync tools from a
    threadpool, and `run_until_complete` on a shared loop raises as soon as two
    overlap; one owning thread keeps a persistent client safe.
    """

    def __init__(self, name: str) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any], timeout: float | None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def close(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()


class RemoteMcpAdapter(AdapterBase):
    """Another MCP server presented as a local source, so one endpoint can front
    databases owned by different teams."""

    DEFAULT_TIMEOUT: ClassVar[float] = 30.0

    def __init__(
        self,
        client: Client,
        *,
        database: str = "",
        max_sample_limit: int | None = None,
        timeout: float | None = None,
    ) -> None:
        super().__init__(database=database, max_sample_limit=max_sample_limit)
        self._client = client
        self._timeout = self.DEFAULT_TIMEOUT if timeout is None else timeout
        self._runner = _LoopRunner(f"remote-mcp[{database or 'default'}]")
        self._lock = threading.Lock()
        self._connected = False
        self._closed = False

    @classmethod
    def from_connection(
        cls,
        conn_info: ConnectionInfo,
        *,
        database: str | None = None,
        max_sample_limit: int | None = None,
    ) -> RemoteMcpAdapter:
        target = conn_info.uri or conn_info.path
        if not target:
            raise ValueError(
                "The mcp connection requires a <REF>_URI (http://…) or <REF>_PATH "
                "(a server script, for stdio)."
            )
        auth = BearerAuth(conn_info.token) if conn_info.token else None
        return cls(
            Client(target, auth=auth),
            database=database or conn_info.database or "",
            max_sample_limit=max_sample_limit,
        )

    @classmethod
    def from_url(
        cls, url: str, *, database: str = "", token: str | None = None
    ) -> RemoteMcpAdapter:
        auth = BearerAuth(token) if token else None
        return cls(Client(url, auth=auth), database=database)

    # ---- lifecycle ----

    def _ensure(self) -> None:
        if self._closed:
            raise RemoteCallError("this adapter is closed")
        if self._connected:
            return
        with self._lock:
            if not self._connected:
                self._runner.submit(self._client.__aenter__(), self._timeout)
                self._connected = True
                log.info(f"connected to remote mcp for {self._database or '<default>'}")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._connected:
                self._runner.submit(
                    self._client.__aexit__(None, None, None), self._timeout
                )
                self._connected = False
        self._runner.close()

    def ping(self) -> bool:
        """A real round-trip, so the pool rebuilds a remote that went away."""
        if self._closed:
            return False
        try:
            self._ensure()
            return bool(self._runner.submit(self._client.ping(), self._timeout))
        except Exception:  # noqa: BLE001 - any failure means unusable
            return False

    def __enter__(self) -> RemoteMcpAdapter:
        self._ensure()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- calling ----

    def _call(self, tool: str, args: dict[str, Any]) -> Any:
        self._ensure()
        self._record_sql(f"call {tool}({', '.join(sorted(args))})")
        payload = {k: v for k, v in args.items() if v is not None}
        try:
            result = self._runner.submit(
                self._client.call_tool(tool, payload, timeout=self._timeout),
                self._timeout,
            )
        except TimeoutError as exc:
            raise self._timed_out(tool) from exc
        except McpError as exc:
            # a timeout lands here, not on TimeoutError: mcp translates it first
            if getattr(exc.error, "code", None) == _REQUEST_TIMEOUT:
                raise self._timed_out(tool) from exc
            raise RemoteCallError(f"remote {tool} failed: {exc}") from exc
        except FastMCPError as exc:
            raise RemoteCallError(f"remote {tool} failed: {exc}") from exc

        if result.data is None:
            raise RemoteCallError(f"remote {tool} returned no structured data")
        return result.data

    def _timed_out(self, tool: str) -> RemoteCallError:
        return RemoteCallError(f"remote {tool} timed out after {self._timeout}s")

    # 4 tools (READ ONLY)

    def list_databases(self) -> list[str]:
        return [str(name) for name in self._call("list_databases", {})]

    def list_containers(
        self,
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage:
        data = self._call(
            "list_containers",
            {
                "database": database or self._database or None,
                "schema": schema,
                "limit": self._cap_page_size(limit),
                "cursor": cursor,
            },
        )
        return ContainerPage.model_validate(_as_dict(data))

    def get_schema(self, container: str) -> list[ColumnInfo]:
        data = self._call("get_schema", {"container": container})
        return [ColumnInfo.model_validate(_as_dict(row)) for row in data]

    def get_sample(
        self, container: str, limit: int = AdapterBase._DEFAULT_SAMPLE_LIMIT
    ) -> list[dict[str, Any]]:
        data = self._call(
            "get_sample", {"container": container, "limit": self._cap_limit(limit)}
        )
        return [dict(_as_dict(row)) for row in data]

    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult:
        data = self._call(
            "profile_column",
            {"container": container, "column": column, "mode": str(mode)},
        )
        return ProfileResult.model_validate(_as_dict(data))


def _as_dict(value: Any) -> Any:
    """`CallToolResult.data` is a dataclass generated from the remote's output
    schema, not a dict. `asdict` recurses, covering nested models."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    dump = getattr(value, "model_dump", None)
    return dump() if callable(dump) else value
