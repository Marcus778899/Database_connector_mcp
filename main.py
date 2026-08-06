"""
Entry point. Turns arguments and the environment into a `ServerConfig`, wires a
source onto the tools, and runs the transport.

Every setting has a flag and an `MCP_*` environment variable; the flag wins.
Connection details are not among them — those live under the `connection_ref`
prefix and are resolved by `src.core.config.resolve_connection`.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from fastmcp import FastMCP
from pydantic import ValidationError

from src.auth import AuthConfigurationError
from src.auth.commands import COMMAND as TOKEN_COMMAND
from src.auth.commands import add_token_command, run_token_command
from src.core.config import (
    MissingConnectionEnvError,
    ServerConfig,
    SourceEngine,
    Transport,
    resolve_connection,
)
from src.core.contracts import ProfileMode
from src.core.log import log
from src.server import build_server
from src.service.inventory import InventoryService
from src.service.pool import AdapterPool
from src.service.staging import StagingError, StagingStore

_TRANSPORTS: tuple[str, ...] = ("stdio", "http", "streamable-http", "sse")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


class ConfigurationError(Exception):
    """The server cannot be built from what was given."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-connector",
        description="Serve a database's catalog over MCP.",
    )
    parser.add_argument("--server-name", help="env MCP_SERVER_NAME")
    parser.add_argument(
        "--engine",
        choices=[engine.value for engine in SourceEngine],
        help="env MCP_ENGINE",
    )
    parser.add_argument(
        "--connection-ref",
        help="prefix of the <REF>_HOST / <REF>_URI / <REF>_PATH … variables "
        "holding the connection details. env MCP_CONNECTION_REF",
    )
    parser.add_argument("--database", help="env MCP_DATABASE")
    parser.add_argument("--transport", choices=_TRANSPORTS, help="env MCP_TRANSPORT")
    parser.add_argument("--host", help="env MCP_HOST")
    parser.add_argument("--port", help="env MCP_PORT")
    parser.add_argument("--max-sample-limit", help="env MCP_MAX_SAMPLE_LIMIT")
    parser.add_argument(
        "--staging-db",
        help="where a scan accumulates. Without it the inventory tools are not "
        "served. env MCP_STAGING_DB",
    )
    parser.add_argument("--export-dir", help="env MCP_EXPORT_DIR")
    parser.add_argument("--audit-log", help="env MCP_AUDIT_LOG")
    parser.add_argument(
        "--audit-max-mb",
        help="rotate the audit trail past this size; 0 never rotates. "
        "env MCP_AUDIT_MAX_MB",
    )
    parser.add_argument(
        "--audit-backups",
        help="how many rotated trails to keep. env MCP_AUDIT_BACKUPS",
    )
    parser.add_argument(
        "--profile-mode",
        action="append",
        choices=[mode.value for mode in ProfileMode],
        help="statistics a scan gathers for every column; repeatable. Unset, "
        "each column gets what its type warrants. env MCP_PROFILE_MODES "
        "(comma separated)",
    )
    # Declared the positive way round: BooleanOptionalAction reads a leading
    # "--no-" as the negation, so a flag actually named --no-profile would set
    # itself to False.
    parser.add_argument(
        "--profile",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--no-profile gathers no statistics at all, whatever the column. "
        "env MCP_PROFILE",
    )
    parser.add_argument("--authorized-keys-dir", help="env MCP_AUTHORIZED_KEYS_DIR")
    parser.add_argument("--audience", help="env MCP_AUDIENCE")
    parser.add_argument(
        "--require-auth",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="env MCP_REQUIRE_AUTH",
    )
    parser.add_argument(
        "--allow-insecure-http",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="serve a network transport without auth on purpose. "
        "env MCP_ALLOW_INSECURE_HTTP",
    )
    # Optional, so serving stays the bare form: `mcp-connector --engine sqlite …`
    # is what every mcp.json in the wild already says.
    commands = parser.add_subparsers(dest="command")
    add_token_command(commands)
    return parser


def _flag(raw: str | None) -> bool | None:
    """Tri-state: None means "not set", so the model's default survives."""
    if raw is None or raw.strip() == "":
        return None
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ConfigurationError(f"expected a boolean, got {raw!r}")


def _modes(raw: str | None) -> list[ProfileMode] | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return [ProfileMode(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise ConfigurationError(f"unknown profile mode: {exc}") from exc


def config_from_args(
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> ServerConfig:
    """
    Flag, then environment, then the model's own default.

    Values stay as strings where pydantic can coerce them, so the validation
    rules live in one place.
    """
    env = os.environ if env is None else env
    values: dict[str, Any] = {}

    def pick(field: str, flag: Any, env_key: str) -> None:
        raw = flag if flag is not None else env.get(env_key)
        if raw is None or raw == "":
            return
        values[field] = raw

    pick("server_name", args.server_name, "MCP_SERVER_NAME")
    pick("engine", args.engine, "MCP_ENGINE")
    pick("connection_ref", args.connection_ref, "MCP_CONNECTION_REF")
    pick("database", args.database, "MCP_DATABASE")
    pick("max_sample_limit", args.max_sample_limit, "MCP_MAX_SAMPLE_LIMIT")
    pick("transport", args.transport, "MCP_TRANSPORT")
    pick("host", args.host, "MCP_HOST")
    pick("port", args.port, "MCP_PORT")
    pick("staging_db_path", args.staging_db, "MCP_STAGING_DB")
    pick("export_dir", args.export_dir, "MCP_EXPORT_DIR")
    pick("audit_log_path", args.audit_log, "MCP_AUDIT_LOG")
    pick("audit_max_mb", args.audit_max_mb, "MCP_AUDIT_MAX_MB")
    pick("audit_backups", args.audit_backups, "MCP_AUDIT_BACKUPS")
    pick("authorized_keys_dir", args.authorized_keys_dir, "MCP_AUTHORIZED_KEYS_DIR")
    pick("audience", args.audience, "MCP_AUDIENCE")

    # [] and None differ downstream: nothing gathered versus per-column choice.
    profile = (
        args.profile if args.profile is not None else _flag(env.get("MCP_PROFILE"))
    )
    modes = args.profile_mode or _modes(env.get("MCP_PROFILE_MODES"))
    if profile is False:
        values["profile_modes"] = []
    elif modes:
        values["profile_modes"] = modes

    for field, flag, env_key in (
        ("require_auth", args.require_auth, "MCP_REQUIRE_AUTH"),
        ("allow_insecure_http", args.allow_insecure_http, "MCP_ALLOW_INSECURE_HTTP"),
    ):
        resolved = flag if flag is not None else _flag(env.get(env_key))
        if resolved is not None:
            values[field] = resolved

    try:
        return ServerConfig(**values)
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc


def build(config: ServerConfig) -> FastMCP:
    """
    Wire the tools onto the configured source.

    Nothing starts listening here; `main` picks the transport. The server's
    lifespan owns the teardown of everything built below.
    """
    if not config.connection_ref:
        raise ConfigurationError(
            "no connection: pass --connection-ref (or MCP_CONNECTION_REF), naming "
            "the prefix of the <REF>_URI / <REF>_PATH / <REF>_HOST … variables "
            "that hold the connection details"
        )

    try:
        conn_info = resolve_connection(config.connection_ref)
    except MissingConnectionEnvError as exc:
        # A mistyped ref is the commonest setup error, and the message already
        # names the variables it looked for. It should not arrive wrapped in a
        # traceback.
        raise ConfigurationError(str(exc)) from exc

    provider = AdapterPool(
        config.engine,
        conn_info,
        max_sample_limit=config.max_sample_limit,
        default_database=config.database,
    )

    inventory = None
    if config.staging_db_path is not None:
        try:
            # source_path lets the store refuse to stage into the database it is
            # inventorying
            store = StagingStore(config.staging_db_path, source_path=conn_info.path)
        except StagingError as exc:
            # an unusable staging path is a configuration mistake, not a crash
            raise ConfigurationError(str(exc)) from exc
        inventory = InventoryService(
            provider, store, default_profile_modes=config.profile_modes
        )
    else:
        log.info("no staging database configured; the inventory tools stay off")

    try:
        return build_server(config, provider, inventory=inventory)
    except AuthConfigurationError as exc:
        # a missing or unusable key directory is a setup mistake, and the
        # message already says which one
        raise ConfigurationError(str(exc)) from exc


def _run_kwargs(config: ServerConfig) -> dict[str, Any]:
    """host/port belong to the http transports; stdio rejects them."""
    if config.transport == "stdio":
        return {}
    return {"host": config.host, "port": config.port}


def main(argv: Sequence[str] | None = None) -> int:
    from src.core.env import load_repo_dotenv

    # before the config is read, so a repo-local .env can supply it
    load_repo_dotenv()

    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) == TOKEN_COMMAND:
        # issuing only: nothing below this line runs, so no source is opened
        # and no port is bound
        return run_token_command(args)

    try:
        config = config_from_args(args)
        mcp = build(config)
    except (ConfigurationError, ValueError) as exc:
        # stderr, never stdout: on stdio that stream carries JSON-RPC
        print(f"error: {exc}", file=sys.stderr)
        return 2

    transport: Transport = config.transport
    mcp.run(transport=transport, **_run_kwargs(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
