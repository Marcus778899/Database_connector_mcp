"""
The only test that proves the wiring: a real http server, a real Bearer header.

Everything else here can pass while `build_server` quietly hands FastMCP no
verifier at all, which is the one failure that matters — an unauthenticated
caller reading the database. So this module pays for a server on a socket.
"""

import asyncio
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from src.adapter.sqlite import SqliteAdapter
from src.auth.issue import generate_keypair, install_public_key, issue_token
from src.core.config import ServerConfig
from src.server import build_server

AUDIENCE = "etl-agent-mcp"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("source") / "shop.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
    conn.commit()
    conn.close()
    return path


@pytest.fixture(scope="module")
def signing_key(tmp_path_factory):
    keys_dir = tmp_path_factory.mktemp("keys")
    pair = generate_keypair("agent")
    install_public_key(pair, keys_dir)
    return pair, keys_dir


@pytest.fixture(scope="module")
def served(source: Path, signing_key) -> Iterator[str]:
    """One server for the module: starting it costs a second, and none of these
    tests change its configuration."""
    _, keys_dir = signing_key
    port = _free_port()
    config = ServerConfig(
        transport="http",
        host="127.0.0.1",
        port=port,
        require_auth=True,
        authorized_keys_dir=keys_dir,
        audit_log_path=keys_dir.parent / "audit.jsonl",
    )
    adapter = SqliteAdapter(source)
    mcp = build_server(config, adapter)

    thread = threading.Thread(
        target=lambda: mcp.run(transport="http", host="127.0.0.1", port=port),
        daemon=True,  # nothing here can stop uvicorn; the process outliving it will
        name="test-http-server",
    )
    thread.start()
    _wait_until_listening(port)
    yield f"http://127.0.0.1:{port}/mcp"


def _wait_until_listening(port: int, timeout: float = 20.0) -> None:
    """Poll rather than sleep a fixed amount: uvicorn starts in well under a
    second locally and takes longer on a loaded runner."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"no server on port {port} after {timeout}s")


def _token(pair, **overrides) -> str:
    args: dict[str, Any] = {
        "kid": "agent",
        "subject": "agent",
        "audience": AUDIENCE,
        "scopes": ["list_containers"],
        "lifetime": timedelta(hours=1),
    }
    args.update(overrides)
    return issue_token(pair.private_pem, **args)


def _call(url: str, token: str | None, tool: str = "list_containers") -> Any:
    async def run() -> Any:
        transport = StreamableHttpTransport(
            url, auth=BearerAuth(token) if token is not None else None
        )
        async with Client(transport) as client:
            return (await client.call_tool(tool, {})).data

    return asyncio.run(run())


def test_a_valid_token_gets_through(served: str, signing_key):
    page = _call(served, _token(signing_key[0]))

    assert [c.container_name for c in page.containers] == ["users"]


def test_no_token_is_turned_away_at_the_door(served: str):
    """Not a tool error — the request never reaches a tool."""
    with pytest.raises(Exception, match="401"):
        _call(served, None)


def test_a_token_for_another_audience_is_turned_away(served: str, signing_key):
    with pytest.raises(Exception, match="401"):
        _call(served, _token(signing_key[0], audience="some-other-server"))


def test_a_token_signed_by_a_stranger_is_turned_away(served: str):
    with pytest.raises(Exception, match="401"):
        _call(served, _token(generate_keypair("agent")))


def test_a_token_naming_an_unknown_key_is_turned_away(served: str, signing_key):
    with pytest.raises(Exception, match="401"):
        _call(served, _token(signing_key[0], kid="nobody"))


def test_something_that_is_not_a_token_at_all_is_turned_away(served: str):
    with pytest.raises(Exception, match="401"):
        _call(served, "not-a-jwt")


def test_a_valid_token_may_still_not_call_what_it_lacks_the_scope_for(
    served: str, signing_key
):
    """Authenticated is not authorised: this one gets in and is refused the tool."""
    token = _token(signing_key[0], scopes=["get_schema"])

    with pytest.raises(ToolError, match="may not call list_containers"):
        _call(served, token)


def test_the_audit_trail_records_who_rather_than_local(served: str, signing_key):
    """`key_id` was always "local" before there was anything to identify."""
    _call(served, _token(signing_key[0], subject="pm-alice"))

    trail = (signing_key[1].parent / "audit.jsonl").read_text(encoding="utf-8")
    assert '"key_id": "pm-alice"' in trail
    assert '"key_id": "local"' not in trail
