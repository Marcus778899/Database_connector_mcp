from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Sequence

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.auth.keys import KEY_SUFFIX, MalformedKeyIdError, valid_kid
from src.core.log import log

# Ed25519: one curve, no parameters to choose badly, and a public key short
# enough to paste. The verifier accepts RS256 and ES256 too, for keys issued
# elsewhere; nothing here needs to produce them.
ALGORITHM = "EdDSA"

_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class IssueError(Exception):
    """The token or key pair cannot be produced as asked."""


@dataclass(frozen=True)
class KeyPair:
    """A new identity. The private half is never written anywhere by itself."""

    kid: str
    private_pem: str
    public_pem: str


def parse_duration(text: str) -> timedelta:
    """`30d`, `12h`, `90m` — a lifetime is easier to get right than a date."""
    match = _DURATION_RE.match(text.strip())
    if match is None:
        raise IssueError(f"expected a lifetime like 30d, 12h or 90m, got {text!r}")
    amount, unit = match.groups()
    seconds = int(amount) * _DURATION_UNITS[unit]
    if seconds <= 0:
        raise IssueError("a token that has already expired is no use to anyone")
    return timedelta(seconds=seconds)


def check_kid(kid: str) -> str:
    """The name has to survive being read back out of an untrusted header."""
    if not valid_kid(kid):
        raise MalformedKeyIdError(
            f"a key id may only hold letters, digits, '_' and '-': {kid!r}"
        )
    return kid


def generate_keypair(kid: str) -> KeyPair:
    """A fresh Ed25519 pair for one agent."""
    check_kid(kid)
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return KeyPair(kid=kid, private_pem=private_pem, public_pem=public_pem)


def public_key_of(private_pem: str) -> str:
    """
    Derive the public half from a signing key.

    For the restart case: the signing key survives on its volume while the
    directory the server reads does not, and regenerating the pair instead
    would invalidate every token already issued.
    """
    try:
        private = serialization.load_pem_private_key(
            private_pem.encode("utf-8"), password=None
        )
    except ValueError as exc:
        raise IssueError(f"not a usable signing key: {exc}") from exc
    return (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )


def install_public_key(pair: KeyPair, keys_dir: str | Path) -> Path:
    """Put the public half where the server will look for it."""
    directory = Path(keys_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{pair.kid}{KEY_SUFFIX}"
    target.write_text(pair.public_pem, encoding="utf-8")
    return target


def save_private_key(pair: KeyPair, path: str | Path, *, keys_dir: Path | None) -> Path:
    """
    Write the signing key, refusing the two ways it gets lost or leaked:
    overwriting an existing one, and filing it next to the public keys the
    server reads.
    """
    target = Path(path)
    if target.exists():
        raise IssueError(
            f"{target} already exists; a signing key is the one thing here that "
            "cannot be regenerated, so this will not overwrite it"
        )
    if keys_dir is not None and target.resolve().parent == keys_dir.resolve():
        raise IssueError(
            f"{target} is inside the authorized keys directory, which is the one "
            "place a private key must never be. The server needs the public half "
            "only."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pair.private_pem, encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError as exc:
        # Windows ACLs are not this model, so this is expected there rather than
        # wrong. Said out loud all the same: whoever just wrote a signing key
        # should know the filesystem is not the thing protecting it.
        log.warning(
            f"could not restrict permissions on {target} ({exc}); protect the "
            "signing key by other means"
        )
    return target


def issue_token(
    private_pem: str,
    *,
    kid: str,
    subject: str,
    audience: str,
    scopes: Sequence[str],
    lifetime: timedelta,
    issued_at: datetime | None = None,
) -> str:
    """
    Sign one token, offline.

    `mcp.json` can carry a static header but cannot compute a signature, so the
    signing happens here and the agent only ever holds the result. The private
    key never reaches the server.
    """
    check_kid(kid)
    if not subject:
        raise IssueError("a token needs a subject: it becomes the audit trail's caller")
    now = issued_at or datetime.now(UTC)
    payload = {
        "sub": subject,
        "aud": audience,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "scopes": list(scopes),
    }
    return jwt.encode(payload, private_pem, algorithm=ALGORITHM, headers={"kid": kid})
