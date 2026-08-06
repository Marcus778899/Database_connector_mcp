"""
Who is allowed to call this server.

The shape is forced by `mcp.json`: it can carry a static header but cannot
compute a signature, so the signing happens offline and the server only ever
verifies (`verifier.py`) against a directory of public keys (`keys.py`). The
server therefore holds nothing it could mint a token with.

`issue.py` is the signing half, and it lives here rather than in the entry
point because it is worth testing on its own; the command that drives it is
`issue_token.py`, in the repository root beside `main.py`.
"""

from __future__ import annotations

from src.auth.keys import (
    AuthKeyError,
    KeyDirectoryError,
    MalformedKeyIdError,
    PublicKeyDirectory,
    UnknownKeyError,
    valid_kid,
)
from src.auth.verifier import SignedTokenVerifier
from src.core.config import ServerConfig


class AuthConfigurationError(Exception):
    """Authentication was asked for but cannot be set up as configured."""


def verifier_from_config(config: ServerConfig) -> SignedTokenVerifier:
    """Build the verifier a server with `require_auth` needs."""
    if config.authorized_keys_dir is None:
        raise AuthConfigurationError(
            "require_auth is on but no authorized keys directory is set: pass "
            "--authorized-keys-dir (or MCP_AUTHORIZED_KEYS_DIR) naming a directory "
            "of <kid>.pub files. Create one with `mcp-connector-token keygen`."
        )
    try:
        keys = PublicKeyDirectory(config.authorized_keys_dir)
    except KeyDirectoryError as exc:
        raise AuthConfigurationError(str(exc)) from exc
    return SignedTokenVerifier(keys, audience=config.audience)


__all__ = [
    "AuthConfigurationError",
    "AuthKeyError",
    "KeyDirectoryError",
    "MalformedKeyIdError",
    "PublicKeyDirectory",
    "SignedTokenVerifier",
    "UnknownKeyError",
    "valid_kid",
    "verifier_from_config",
]
