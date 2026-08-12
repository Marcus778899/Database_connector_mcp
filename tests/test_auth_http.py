"""
The only test that proves the wiring: a real http server, a real Bearer header.

Everything else here can pass while `build_server` quietly hands FastMCP no
verifier at all, which is the one failure that matters — an unauthenticated
caller reading the database. So this module pays for a server on a socket.
"""

import asyncio
import json
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from src.adapter.sqlite import SqliteAdapter
from src.auth.issue import generate_keypair, install_public_key, issue_token
from src.core.config import ServerConfig
from src.server import build_server
from src.service.inventory import InventoryService
from src.service.pool import SingleAdapter
from src.service.staging import StagingStore

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


# ---- downloading an export ----
#
# The route FastMCP does not wrap in RequireAuthMiddleware, so every refusal
# below is one this project's own handler has to make. Same reason this module
# pays for a socket: nothing else proves what the middleware actually covers.


@pytest.fixture(scope="module")
def exporting(
    source: Path, signing_key, tmp_path_factory
) -> Iterator[tuple[str, Path]]:
    """A second server, this one with somewhere to export to."""
    _, keys_dir = signing_key
    root = tmp_path_factory.mktemp("exporting")
    exports = root / "exports"
    exports.mkdir()
    port = _free_port()
    config = ServerConfig(
        transport="http",
        host="127.0.0.1",
        port=port,
        require_auth=True,
        authorized_keys_dir=keys_dir,
        audit_log_path=root / "audit.jsonl",
        staging_db_path=root / "staging.db",
        export_dir=exports,
        public_url=f"http://127.0.0.1:{port}",
    )
    adapter = SqliteAdapter(source)
    store = StagingStore(root / "staging.db")
    service = InventoryService(SingleAdapter(adapter), store)
    mcp = build_server(config, adapter, inventory=service)

    threading.Thread(
        target=lambda: mcp.run(transport="http", host="127.0.0.1", port=port),
        daemon=True,
        name="test-http-export-server",
    ).start()
    _wait_until_listening(port)
    yield f"http://127.0.0.1:{port}", exports


def _download(base: str, name: str, token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # Sent unnormalised: httpx would otherwise collapse `..` client-side and
    # the refusal being tested would never reach the server.
    with httpx.Client() as client:
        return client.send(
            httpx.Request("GET", f"{base}/export/{name}", headers=headers)
        )


@pytest.fixture(scope="module")
def exported(exporting, signing_key) -> str:
    """One real export to fetch, made the way an agent would make it."""
    base, _ = exporting
    token = _token(
        signing_key[0],
        scopes=["inventory_start", "inventory_status", "inventory_export"],
    )

    async def run() -> Any:
        transport = StreamableHttpTransport(f"{base}/mcp", auth=BearerAuth(token))
        async with Client(transport) as client:
            job = (await client.call_tool("inventory_start", {})).data
            for _ in range(200):
                status = (
                    await client.call_tool("inventory_status", {"job_id": job})
                ).data
                if status.state != "running":
                    break
                await asyncio.sleep(0.05)
            return (await client.call_tool("inventory_export", {})).data

    return asyncio.run(run()).download_url


def test_an_export_says_where_to_fetch_it(exported: str, exporting):
    base, _ = exporting

    assert exported == f"{base}/export/inventory.md"


def test_the_right_token_gets_the_file(exported: str, exporting, signing_key):
    base, exports = exporting
    response = _download(
        base, "inventory.md", _token(signing_key[0], scopes=["inventory_export"])
    )

    assert response.status_code == 200
    assert response.text == (exports / "inventory.md").read_text(encoding="utf-8")
    # a download, not something a browser renders: these files carry a client's
    # table and column names
    assert "attachment" in response.headers["content-disposition"]


def test_no_token_is_turned_away_from_the_download_too(exported: str, exporting):
    """The route the framework does not guard; this is our own 401."""
    base, _ = exporting
    response = _download(base, "inventory.md", None)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_token_without_the_export_scope_may_not_download(
    exported: str, exporting, signing_key
):
    """Authenticated is not authorised, the same as for a tool call."""
    base, _ = exporting
    response = _download(
        base, "inventory.md", _token(signing_key[0], scopes=["get_schema"])
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("..%2F..%2F..%2Fetc%2Fpasswd", id="climbing out"),
        pytest.param("nope.md", id="not there"),
    ],
)
def test_what_is_not_there_and_what_is_out_of_bounds_answer_alike(
    exported: str, exporting, signing_key, name: str
):
    """Telling those two apart hands the caller a way to map the filesystem one
    request at a time. The difference goes to the log instead."""
    base, _ = exporting
    response = _download(
        base, name, _token(signing_key[0], scopes=["inventory_export"])
    )

    assert response.status_code == 404
    assert response.text == "no such export\n"


def test_a_download_is_on_the_record(exported: str, exporting, signing_key):
    """The one call that takes the whole catalog off the server. If anything
    here is audited, it is this."""
    base, exports = exporting
    _download(
        base,
        "inventory.md",
        _token(signing_key[0], subject="de-bob", scopes=["inventory_export"]),
    )

    trail = [
        json.loads(line)
        for line in (exports.parent / "audit.jsonl").read_text().splitlines()
    ]
    record = next(
        r for r in reversed(trail) if r["tool"] == "inventory_export_download"
    )
    assert record["key_id"] == "de-bob"
    assert record["bytes_sent"] == (exports / "inventory.md").stat().st_size
