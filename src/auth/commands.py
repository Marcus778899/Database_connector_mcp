"""
The `token` subcommand: what an operator runs to create a key pair and sign a
token with it.

Argument parsing only — the signing itself is `issue.py`, which is worth
testing without a command line around it. Nothing here runs while the server
is serving; `main` dispatches to one or the other, never both.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.auth.issue import (
    IssueError,
    generate_keypair,
    install_public_key,
    issue_token,
    parse_duration,
    public_key_of,
    save_private_key,
)
from src.auth.keys import KEY_SUFFIX, MalformedKeyIdError
from src.core.config import DEFAULT_AUDIENCE

COMMAND = "token"


def add_token_command(commands: argparse._SubParsersAction) -> None:
    """Hang `token keygen` and `token issue` off the top-level parser."""
    token = commands.add_parser(
        COMMAND,
        help="create the keys an agent authenticates with, and sign its tokens",
        description=(
            "Signing happens here rather than in the server: mcp.json can carry a "
            "static header but cannot compute a signature, and the server should "
            "never hold what it would take to mint a token."
        ),
    )
    actions = token.add_subparsers(dest="token_command", required=True)

    keygen = actions.add_parser(
        "keygen", help="one agent's key pair, filing the public half for the server"
    )
    keygen.add_argument(
        "--kid",
        required=True,
        help="the agent's name, and the filename its public key takes",
    )
    keygen.add_argument(
        "--keys-dir", required=True, help="the server's authorized keys directory"
    )
    keygen.add_argument(
        "--out",
        required=True,
        help="where to write the signing key. Keep it off the server.",
    )
    keygen.add_argument(
        "--if-missing",
        action="store_true",
        help="succeed quietly when the signing key is already there, rather than "
        "refusing to overwrite it. For a container that starts more than once: "
        "generating a new pair would invalidate every token already issued.",
    )

    issue = actions.add_parser("issue", help="sign one token")
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
    issue.add_argument(
        "--out", help="write the token here instead of to stdout, and say nothing else"
    )


def run_token_command(args: argparse.Namespace) -> int:
    """Dispatch `token …`. Returns the process's exit code."""
    try:
        if args.token_command == "keygen":
            return _keygen(args)
        return _issue(args)
    except (IssueError, MalformedKeyIdError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _keygen(args: argparse.Namespace) -> int:
    keys_dir = Path(args.keys_dir)
    private_path = Path(args.out)

    if args.if_missing and private_path.exists():
        # The public half may live on a different volume from the private one,
        # so an existing signing key does not guarantee the server can see it.
        public_path = keys_dir / f"{args.kid}{KEY_SUFFIX}"
        if not public_path.exists():
            public_path.parent.mkdir(parents=True, exist_ok=True)
            public_path.write_text(
                public_key_of(private_path.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            print(f"public key    {public_path}   restored from {private_path}")
        else:
            print(f"signing key   {private_path}   already there, keeping it")
        return 0

    pair = generate_keypair(args.kid)
    # private key first: a public key on file with no matching signing key would
    # be an entry in the allowlist that nobody can use and nobody remembers
    private_path = save_private_key(pair, private_path, keys_dir=keys_dir)
    public_path = install_public_key(pair, keys_dir)
    print(f"signing key   {private_path}   keep this off the server")
    print(f"public key    {public_path}   the server reads this")
    print(f"revoke with   rm {public_path}")
    return 0


def _issue(args: argparse.Namespace) -> int:
    token = issue_token(
        Path(args.key).read_text(encoding="utf-8"),
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
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(token, encoding="utf-8")
        print(f"token written to {target}", file=sys.stderr)
    else:
        # stdout and nothing else, so `... > token.jwt` gives a usable file
        print(token)
    return 0
