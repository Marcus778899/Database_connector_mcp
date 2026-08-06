from __future__ import annotations

from typing import Any, ClassVar

import jwt
from fastmcp.server.auth.auth import AccessToken, TokenVerifier

from src.auth.keys import AuthKeyError, PublicKeyDirectory
from src.core.log import log

# Asymmetric only, and named explicitly. Accepting an HMAC algorithm here is the
# classic JWT forgery: the attacker signs with HS256 using the *public* key as
# the shared secret, and a verifier that trusts the header's `alg` accepts it.
# The public keys are public, so that would be no barrier at all.
ALGORITHMS: tuple[str, ...] = ("EdDSA", "ES256", "RS256")

# Claims without which a token means nothing here. `exp` is required because
# expiry is the main way a token is revoked; `sub` because it is what the audit
# trail records as the caller.
REQUIRED_CLAIMS: tuple[str, ...] = ("exp", "aud", "sub")


class SignedTokenVerifier(TokenVerifier):
    """
    Verifies a Bearer JWT against the public key its `kid` names.

    Deliberately says nothing back about why a token failed — an error that
    distinguished "unknown key" from "bad signature" from "expired" would let
    someone map the key directory by trying tokens. The reason goes to the log,
    where the operator can see it and the caller cannot.
    """

    # Tolerated clock difference between whoever signed the token and this
    # server. Without it a correctly issued token can be rejected for seconds.
    DEFAULT_LEEWAY: ClassVar[float] = 60.0

    def __init__(
        self,
        keys: PublicKeyDirectory,
        *,
        audience: str,
        leeway: float | None = None,
        algorithms: tuple[str, ...] = ALGORITHMS,
    ) -> None:
        super().__init__()
        self._keys = keys
        self._audience = audience
        self._leeway = self.DEFAULT_LEEWAY if leeway is None else leeway
        self._algorithms = list(algorithms)

    async def verify_token(self, token: str) -> AccessToken | None:
        """The token's claims if it is genuinely ours, otherwise None (401)."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            log.warning(f"auth: unreadable token header ({type(exc).__name__}: {exc})")
            return None
        if not kid:
            log.warning("auth: token carries no kid, so no key can be chosen for it")
            return None

        try:
            public_key = self._keys.get(kid)
        except AuthKeyError as exc:
            log.warning(f"auth: no usable key for kid {kid!r} ({type(exc).__name__})")
            return None

        try:
            claims = jwt.decode(
                token,
                public_key,
                algorithms=self._algorithms,
                audience=self._audience,
                leeway=self._leeway,
                options={"require": list(REQUIRED_CLAIMS)},
            )
        except jwt.PyJWTError as exc:
            log.warning(
                f"auth: rejected token from kid {kid!r} "
                f"({type(exc).__name__}: {exc})"
            )
            return None

        subject = str(claims["sub"])
        log.info(f"auth: {subject} accepted (kid {kid})")
        return AccessToken(
            token=token,
            client_id=subject,
            subject=subject,
            scopes=_scopes(claims),
            expires_at=int(claims["exp"]),
            claims=claims,
        )


def _scopes(claims: dict[str, Any]) -> list[str]:
    """
    What the key is allowed to call.

    `scopes` as a list is what this project's own issuer writes; `scope` as a
    space-separated string is what OAuth uses, and accepting it costs one line
    against the day a token comes from somewhere else. Anything else is treated
    as no scopes at all rather than guessed at.
    """
    raw = claims.get("scopes", claims.get("scope"))
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []
