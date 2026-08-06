import asyncio
import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest

from src.auth.issue import generate_keypair, install_public_key, issue_token
from src.auth.keys import PublicKeyDirectory
from src.auth.verifier import SignedTokenVerifier

AUDIENCE = "etl-agent-mcp"


@pytest.fixture
def keys(tmp_path: Path):
    pair = generate_keypair("agent")
    install_public_key(pair, tmp_path / "keys")
    return pair, PublicKeyDirectory(tmp_path / "keys", ttl=0)


@pytest.fixture
def verifier(keys) -> SignedTokenVerifier:
    return SignedTokenVerifier(keys[1], audience=AUDIENCE)


def _token(pair, **overrides) -> str:
    args = {
        "kid": "agent",
        "subject": "agent",
        "audience": AUDIENCE,
        "scopes": ["get_schema"],
        "lifetime": timedelta(hours=1),
    }
    args.update(overrides)
    return issue_token(pair.private_pem, **args)


def _verify(verifier: SignedTokenVerifier, token: str):
    return asyncio.run(verifier.verify_token(token))


def _b64(raw: dict) -> str:
    packed = json.dumps(raw, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(packed).rstrip(b"=").decode("ascii")


def _hs256(header: dict, payload: dict, *, secret: bytes) -> str:
    """A JWT signed with HMAC, built without a library's opinions."""
    signing_input = f"{_b64(header)}.{_b64(payload)}".encode("ascii")
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    tail = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{signing_input.decode('ascii')}.{tail}"


# ---- what a good token gets you ----


def test_a_token_this_server_issued_is_accepted(verifier, keys):
    accepted = _verify(verifier, _token(keys[0]))

    assert accepted is not None
    assert accepted.subject == "agent"
    assert accepted.client_id == "agent"
    assert accepted.scopes == ["get_schema"]


def test_the_subject_is_what_the_audit_trail_will_record(verifier, keys):
    accepted = _verify(verifier, _token(keys[0], subject="pm-alice"))

    assert accepted is not None
    assert accepted.subject == "pm-alice"


def test_the_claims_come_back_whole_for_whatever_needs_them_later(verifier, keys):
    """Phase 4 adds claims to this token; they have to survive the verifier."""
    accepted = _verify(verifier, _token(keys[0]))

    assert accepted is not None
    assert accepted.claims["aud"] == AUDIENCE
    assert accepted.claims["exp"] > 0


# ---- what it does not ----


def test_an_expired_token_is_refused(verifier, keys):
    """Expiry is the main revocation mechanism, so it has to bite."""
    stale = _token(
        keys[0],
        lifetime=timedelta(minutes=5),
        issued_at=datetime.now(UTC) - timedelta(days=1),
    )

    assert _verify(verifier, stale) is None


def test_a_token_for_somewhere_else_is_refused(verifier, keys):
    """Otherwise a token signed for another service gets replayed at this one."""
    assert _verify(verifier, _token(keys[0], audience="some-other-server")) is None


def test_a_token_naming_a_key_we_do_not_have_is_refused(verifier, keys):
    assert _verify(verifier, _token(keys[0], kid="stranger")) is None


def test_a_token_from_another_key_pair_is_refused(verifier, tmp_path: Path):
    """The signature is the whole point: knowing a kid is not enough."""
    impostor = generate_keypair("agent")

    assert _verify(verifier, _token(impostor)) is None


def test_a_tampered_payload_is_refused(verifier, keys):
    header, payload, signature = _token(keys[0]).split(".")
    other = _token(keys[0], subject="somebody-else").split(".")[1]

    assert _verify(verifier, f"{header}.{other}.{signature}") is None


def test_a_token_with_no_kid_is_refused(verifier, keys):
    """Nothing tells us which key to check it against, and guessing is not a
    security decision."""
    naked = jwt.encode(
        {
            "sub": "agent",
            "aud": AUDIENCE,
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        keys[0].private_pem,
        algorithm="EdDSA",
    )

    assert _verify(verifier, naked) is None


def test_a_symmetric_signature_over_the_public_key_is_refused(verifier, keys):
    """
    The classic JWT forgery: sign with HS256 using the *public* key as the
    shared secret. The public keys are public, so a verifier that took the
    header's `alg` at its word would let anyone in.

    Assembled by hand because pyjwt refuses to produce it — which is a guard on
    this repo's issuer, not on an attacker's.
    """
    payload = {
        "sub": "attacker",
        "aud": AUDIENCE,
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "scopes": ["get_sample"],
    }
    forged = _hs256(
        {"alg": "HS256", "typ": "JWT", "kid": "agent"},
        payload,
        secret=keys[0].public_pem.encode("utf-8"),
    )

    assert _verify(verifier, forged) is None


@pytest.mark.parametrize("missing", ["exp", "aud", "sub"])
def test_a_token_missing_a_required_claim_is_refused(verifier, keys, missing: str):
    payload = {
        "sub": "agent",
        "aud": AUDIENCE,
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
    }
    payload.pop(missing)
    incomplete = jwt.encode(
        payload, keys[0].private_pem, algorithm="EdDSA", headers={"kid": "agent"}
    )

    assert _verify(verifier, incomplete) is None


def test_garbage_is_refused_without_raising(verifier):
    """A malformed header must be a 401, not a 500."""
    assert _verify(verifier, "not-a-token") is None
    assert _verify(verifier, "") is None


def test_a_deleted_key_stops_working(verifier, keys, tmp_path: Path):
    token = _token(keys[0])
    assert _verify(verifier, token) is not None

    (tmp_path / "keys" / "agent.pub").unlink()

    assert _verify(verifier, token) is None


# ---- tolerances ----


def test_a_clock_a_little_behind_is_tolerated(keys):
    """A token that expired seconds ago was almost certainly issued against a
    clock a shade different from ours."""
    verifier = SignedTokenVerifier(keys[1], audience=AUDIENCE, leeway=60)
    just_expired = _token(
        keys[0],
        lifetime=timedelta(seconds=30),
        issued_at=datetime.now(UTC) - timedelta(seconds=45),
    )

    assert _verify(verifier, just_expired) is not None


def test_the_leeway_is_not_a_grace_period(keys):
    verifier = SignedTokenVerifier(keys[1], audience=AUDIENCE, leeway=60)
    long_gone = _token(
        keys[0],
        lifetime=timedelta(seconds=30),
        issued_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    assert _verify(verifier, long_gone) is None


# ---- scopes ----


def test_scopes_arrive_as_a_list(verifier, keys):
    accepted = _verify(verifier, _token(keys[0], scopes=["a", "b"]))

    assert accepted is not None and accepted.scopes == ["a", "b"]


def test_an_oauth_style_scope_string_is_understood(verifier, keys):
    """Not what this project's issuer writes, but what a token from anywhere
    else would carry."""
    token = jwt.encode(
        {
            "sub": "agent",
            "aud": AUDIENCE,
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "scope": "get_schema get_sample",
        },
        keys[0].private_pem,
        algorithm="EdDSA",
        headers={"kid": "agent"},
    )

    accepted = _verify(verifier, token)
    assert accepted is not None and accepted.scopes == ["get_schema", "get_sample"]


def test_a_token_with_no_scopes_may_call_nothing(verifier, keys):
    """Accepted as an identity, but the tools check the scopes themselves."""
    accepted = _verify(verifier, _token(keys[0], scopes=[]))

    assert accepted is not None and accepted.scopes == []
