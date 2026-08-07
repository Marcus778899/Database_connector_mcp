"""
The `token` and `role` subcommands: what an operator runs to create a key pair,
sign a token with it, and read back what a role actually grants.

Argument parsing only — the signing itself is `issue.py` and the roles file is
`src.core.roles`, both worth testing without a command line around them.
Nothing here runs while the server is serving; `main` dispatches to one or the
other, never both.
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
from src.core.roles import ENV_ROLES_FILE, Role, RoleError, find_roles_file, load_roles

COMMAND = "token"
ROLE_COMMAND = "role"

# argparse cannot say whether `--lifetime 30d` was typed or defaulted, and a
# role's own lifetime should not be overridden by a default nobody chose.
DEFAULT_LIFETIME = "30d"


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
        "--role",
        help="grant what this role grants, from the roles file. Flags below are "
        "added on top of it, and never take anything away — see `role show`.",
    )
    issue.add_argument(
        "--roles-file",
        help=f"where the roles are defined. env {ENV_ROLES_FILE}",
    )
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
    issue.add_argument(
        "--lifetime",
        default=DEFAULT_LIFETIME,
        help=f"30d, 12h, 90m (default {DEFAULT_LIFETIME}, or the role's own)",
    )
    issue.add_argument(
        "--database",
        action="append",
        default=[],
        help="a database this token may read; repeatable. None means all of them.",
    )
    issue.add_argument(
        "--allow-container",
        action="append",
        default=[],
        help="a glob of containers this token may read, e.g. 'dim_*'; "
        "repeatable. None means all but the denied.",
    )
    issue.add_argument(
        "--deny-container",
        action="append",
        default=[],
        help="a glob this token may never read, e.g. '*_pii'; repeatable. "
        "Deny beats allow.",
    )
    issue.add_argument(
        "--allow-raw-sample",
        action="store_true",
        help="let this token ask get_sample for unmasked rows. Off by default: "
        "sampled rows go into an agent's context and stay there.",
    )
    issue.add_argument(
        "--annotate-as-human",
        action="store_true",
        help="descriptions written with this token are recorded as a person's "
        "rather than an agent's guesses.",
    )
    issue.add_argument(
        "--out", help="write the token here instead of to stdout, and say nothing else"
    )


def add_role_command(commands: argparse._SubParsersAction) -> None:
    """
    `role list` and `role show`.

    Reading a token back is possible but nobody does it, so a role that grants
    more than its author thought stays unnoticed until it matters. These print
    the resolved answer — inheritance applied, defaults filled in.
    """
    role = commands.add_parser(
        ROLE_COMMAND,
        help="what the named sets of grants in the roles file actually grant",
        description=(
            "Roles are what `token issue --role` cuts a token from. `show` prints "
            "one with its inheritance resolved, which is the form worth reviewing "
            "— a role that extends another does not read as what it grants."
        ),
    )
    role.add_argument(
        "--roles-file", help=f"where the roles are defined. env {ENV_ROLES_FILE}"
    )
    actions = role.add_subparsers(dest="role_command", required=True)
    actions.add_parser("list", help="every role, with what it can call")
    show = actions.add_parser("show", help="one role, in full")
    show.add_argument("name", help="the role to describe")


def run_token_command(args: argparse.Namespace) -> int:
    """Dispatch `token …`. Returns the process's exit code."""
    try:
        if args.token_command == "keygen":
            return _keygen(args)
        return _issue(args)
    except (IssueError, MalformedKeyIdError, RoleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def run_role_command(args: argparse.Namespace) -> int:
    """Dispatch `role …`. Returns the process's exit code."""
    try:
        path = find_roles_file(args.roles_file)
        roles = load_roles(path)
    except RoleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.role_command == "list":
        print(f"# {path}")
        for role in roles.values():
            print(f"\n{role.name}  ({len(role.tools)} tools)")
            if role.description:
                print(f"  {role.description}")
        return 0

    role = roles.get(args.name)
    if role is None:
        known = ", ".join(roles) or "none"
        print(
            f"error: no role {args.name!r} in {path}. Known: {known}", file=sys.stderr
        )
        return 2
    _print_role(role)
    return 0


def _print_role(role: Role) -> None:
    print(f"{role.name}")
    if role.description:
        print(f"  {role.description}")
    print(f"\n  may call ({len(role.tools)}):")
    for tool in role.tools:
        print(f"    {tool}")
    print("\n  databases:  " + (", ".join(role.databases) or "all of them"))
    print(
        "  allow:      " + (", ".join(role.allow_containers) or "everything not denied")
    )
    print("  deny:       " + (", ".join(role.deny_containers) or "nothing"))
    # Spelled out rather than printed as True/False: these two are the settings
    # a reviewer is actually looking for, and "yes" next to a name reads faster
    # than a boolean.
    print(
        "  raw rows:   "
        + ("yes — get_sample(mask=False) is allowed" if role.allow_raw_sample else "no")
    )
    print(
        "  writes as:  "
        + (
            "a person"
            if role.annotate_as_human
            else "an agent (descriptions marked as guesses)"
        )
    )
    print("  lifetime:   " + (role.lifetime or "the issuer's default"))


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


def _grants(args: argparse.Namespace) -> tuple[dict[str, object], str]:
    """
    What goes in the token, and the lifetime to sign it for.

    A role supplies the starting point; the flags add to it. Only adding, never
    removing: `--role pm --allow-raw-sample` is a widening, which is a thing an
    operator does deliberately, whereas a flag that quietly narrowed a role
    would produce a token that does not match the role's own SKILL.md.
    """
    role: Role | None = None
    if args.role:
        roles = load_roles(args.roles_file)
        role = roles.get(args.role)
        if role is None:
            known = ", ".join(roles) or "none"
            raise IssueError(f"no role {args.role!r} in the roles file. Known: {known}")

    grants: dict[str, object] = (
        role.issue_kwargs()
        if role
        else {
            "scopes": [],
            "databases": [],
            "allow_containers": [],
            "deny_containers": [],
            "allow_raw_sample": False,
            "annotate_as_human": False,
        }
    )
    for key, extra in (
        ("scopes", args.scope),
        ("databases", args.database),
        ("allow_containers", args.allow_container),
        ("deny_containers", args.deny_container),
    ):
        merged = list(grants[key])  # type: ignore[arg-type]
        merged.extend(item for item in extra if item not in merged)
        grants[key] = merged
    grants["allow_raw_sample"] = (
        bool(grants["allow_raw_sample"]) or args.allow_raw_sample
    )
    grants["annotate_as_human"] = (
        bool(grants["annotate_as_human"]) or args.annotate_as_human
    )

    # `--lifetime` has a default, so it cannot be told apart from an explicit
    # one; the role's own lifetime therefore wins unless the flag was moved off
    # that default.
    lifetime = args.lifetime
    if role and role.lifetime and lifetime == DEFAULT_LIFETIME:
        lifetime = role.lifetime
    return grants, lifetime


def _issue(args: argparse.Namespace) -> int:
    grants, lifetime = _grants(args)
    token = issue_token(
        Path(args.key).read_text(encoding="utf-8"),
        kid=args.kid,
        subject=args.subject or args.kid,
        audience=args.audience,
        lifetime=parse_duration(lifetime),
        **grants,  # type: ignore[arg-type]
    )
    if not grants["scopes"]:
        print(
            "warning: no --scope and no --role given, so this token may call nothing",
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
