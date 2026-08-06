"""
Key and token issuing, run by whoever operates the server.

Deliberately a separate command from `mcp-connector`: the server verifies
tokens and must never be able to mint one. Nothing here runs inside it.

    mcp-connector-token keygen  --kid pm-explorer --keys-dir ./keys --out ./pm.pem
    mcp-connector-token issue   --key ./pm.pem --kid pm-explorer \\
        --scope list_containers --scope get_schema --lifetime 30d
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from src.auth.issue import (
    IssueError,
    generate_keypair,
    install_public_key,
    issue_token,
    parse_duration,
    save_private_key,
)
from src.auth.keys import MalformedKeyIdError
from src.core.config import DEFAULT_AUDIENCE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-connector-token",
        description="Issue the keys and tokens an agent authenticates with.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    keygen = commands.add_parser(
        "keygen",
        help="create one agent's key pair, filing the public half for the server",
    )
    keygen.add_argument(
        "--kid",
        required=True,
        help="the agent's name, and the filename its public key takes",
    )
    keygen.add_argument(
        "--keys-dir", required=True, help="the server's authorized_keys_dir"
    )
    keygen.add_argument(
        "--out",
        required=True,
        help="where to write the signing key. Keep it off the server.",
    )

    issue = commands.add_parser("issue", help="sign one token")
    issue.add_argument("--key", required=True, help="the signing key from keygen")
    issue.add_argument("--kid", required=True, help="which public key verifies it")
    issue.add_argument("--subject", help="who the token speaks for; defaults to --kid")
    issue.add_argument(
        "--audience",
        default=DEFAULT_AUDIENCE,
        help=f"must match the server's --audience (default {DEFAULT_AUDIENCE})",
    )
    issue.add_argument(
        "--scope",
        action="append",
        default=[],
        help="a tool this token may call; repeatable. No scopes means no tools.",
    )
    issue.add_argument("--lifetime", default="30d", help="30d, 12h, 90m (default 30d)")
    return parser


def _keygen(args: argparse.Namespace) -> int:
    keys_dir = Path(args.keys_dir)
    pair = generate_keypair(args.kid)
    # private key first: a public key on file with no matching signing key would
    # be an entry in the allowlist that nobody can use and nobody remembers
    private_path = save_private_key(pair, args.out, keys_dir=keys_dir)
    public_path = install_public_key(pair, keys_dir)
    print(f"signing key   {private_path}   keep this off the server")
    print(f"public key    {public_path}   the server reads this")
    print(f"revoke with   rm {public_path}")
    return 0


def _issue(args: argparse.Namespace) -> int:
    private_pem = Path(args.key).read_text(encoding="utf-8")
    token = issue_token(
        private_pem,
        kid=args.kid,
        subject=args.subject or args.kid,
        audience=args.audience,
        scopes=args.scope,
        lifetime=parse_duration(args.lifetime),
    )
    if not args.scope:
        print(
            "warning: no --scope given, so this token may call nothing",
            file=sys.stderr,
        )
    print(token)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _keygen(args) if args.command == "keygen" else _issue(args)
    except (IssueError, MalformedKeyIdError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
