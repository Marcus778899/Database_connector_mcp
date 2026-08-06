from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import ClassVar

from src.core.log import log

# The public half only. Nothing here can sign, which is the point: this server
# can verify a token and cannot mint one, so taking the server does not get you
# the ability to issue yourself a new key.
KEY_SUFFIX = ".pub"

# A kid arrives inside an unverified token header, so it is attacker-controlled
# and gets spelled out rather than sanitised: no separators, no dots, nothing
# that could climb out of the directory.
_KID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_kid(kid: str) -> bool:
    """Whether a name is one this directory could hold. The issuer checks the
    same rule, so a key that cannot be looked up cannot be created either."""
    return _KID_RE.match(kid) is not None


class AuthKeyError(Exception):
    """Base for the ways a key lookup fails."""


class MalformedKeyIdError(AuthKeyError):
    """The kid is not a name this directory would ever hold."""


class UnknownKeyError(AuthKeyError):
    """No public key is on file under that kid."""


class KeyDirectoryError(AuthKeyError):
    """The directory itself cannot be used."""


class PublicKeyDirectory:
    """
    The set of agents allowed in, one PEM file each, named for its `kid`.

    Revocation is `rm <kid>.pub`: quicker than waiting for a token to expire and
    needing no restart, which is why the read is cached only briefly rather than
    for the life of the process.
    """

    DEFAULT_TTL: ClassVar[float] = 30.0

    def __init__(self, path: str | Path, *, ttl: float | None = None) -> None:
        self.path = Path(path)
        if not self.path.is_dir():
            raise KeyDirectoryError(
                f"{self.path} is not a directory; authorized_keys_dir must hold one "
                f"<kid>{KEY_SUFFIX} file per agent allowed to connect"
            )
        self._ttl = self.DEFAULT_TTL if ttl is None else ttl
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str]] = {}
        log.info(f"authorized keys read from {self.path} ({len(self.kids())} on file)")

    def kids(self) -> list[str]:
        """Every agent currently allowed in. For diagnostics, not for the hot path."""
        return sorted(
            entry.stem
            for entry in self.path.glob(f"*{KEY_SUFFIX}")
            if entry.is_file() and valid_kid(entry.stem)
        )

    def get(self, kid: str) -> str:
        """
        The PEM for one kid, re-read once the cached copy is `ttl` seconds old.

        The delay is the deliberate cost of not reading a file on every call: a
        deleted key keeps working for at most that long.
        """
        if not valid_kid(kid):
            raise MalformedKeyIdError(f"not a usable key id: {kid!r}")

        with self._lock:
            cached = self._cache.get(kid)
            if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
                return cached[1]

            target = self.path / f"{kid}{KEY_SUFFIX}"
            try:
                pem = target.read_text(encoding="utf-8")
            except FileNotFoundError as exc:
                # Forget it: a revoked key must not be served from the cache.
                self._cache.pop(kid, None)
                raise UnknownKeyError(kid) from exc
            except OSError as exc:
                raise KeyDirectoryError(f"cannot read {target}: {exc}") from exc

            self._cache[kid] = (time.monotonic(), pem)
            return pem

    def forget(self) -> None:
        """Drop the cache, so the next verification re-reads every key."""
        with self._lock:
            self._cache.clear()
