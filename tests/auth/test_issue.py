from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest

from src.auth.issue import (
    ALGORITHM,
    IssueError,
    check_kid,
    generate_keypair,
    install_public_key,
    issue_token,
    parse_duration,
    save_private_key,
)
from src.auth.keys import KEY_SUFFIX, MalformedKeyIdError, PublicKeyDirectory


@pytest.fixture
def pair():
    return generate_keypair("agent")


# ---- lifetimes ----


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("90m", 5400), ("12h", 43200), ("30d", 2592000), ("2w", 1209600)],
)
def test_a_lifetime_is_easier_to_get_right_than_a_date(text: str, seconds: int):
    assert parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["", "30", "d30", "1y", "-5d", "0h", "tomorrow"])
def test_a_lifetime_that_is_not_one_is_refused(text: str):
    with pytest.raises(IssueError):
        parse_duration(text)


# ---- key pairs ----


def test_a_pair_is_generated_and_the_public_half_is_filed(pair, tmp_path: Path):
    target = install_public_key(pair, tmp_path / "keys")

    assert target.name == f"agent{KEY_SUFFIX}"
    assert PublicKeyDirectory(tmp_path / "keys").get("agent") == pair.public_pem


def test_the_private_half_is_a_signing_key_and_the_public_half_is_not(pair):
    assert "PRIVATE KEY" in pair.private_pem
    assert "PUBLIC KEY" in pair.public_pem
    assert "PRIVATE" not in pair.public_pem


def test_every_pair_is_different(tmp_path: Path):
    assert generate_keypair("a").private_pem != generate_keypair("a").private_pem


@pytest.mark.parametrize("kid", ["../escape", "has space", ""])
def test_a_key_id_the_server_could_not_look_up_is_refused(kid: str):
    with pytest.raises(MalformedKeyIdError):
        check_kid(kid)
    with pytest.raises(MalformedKeyIdError):
        generate_keypair(kid)


def test_a_signing_key_is_written_where_asked(pair, tmp_path: Path):
    path = save_private_key(pair, tmp_path / "agent.pem", keys_dir=tmp_path / "keys")

    assert path.read_text(encoding="utf-8") == pair.private_pem


def test_a_signing_key_is_never_overwritten(pair, tmp_path: Path):
    """It is the one thing here that cannot be regenerated: overwrite it and
    every token it signed becomes unverifiable."""
    path = tmp_path / "agent.pem"
    path.write_text("an existing key", encoding="utf-8")

    with pytest.raises(IssueError, match="cannot be regenerated"):
        save_private_key(pair, path, keys_dir=None)

    assert path.read_text(encoding="utf-8") == "an existing key"


def test_a_signing_key_may_not_be_filed_with_the_public_ones(pair, tmp_path: Path):
    """The directory the server reads is the one place it must never be."""
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()

    with pytest.raises(IssueError, match="must never be"):
        save_private_key(pair, keys_dir / "agent.pem", keys_dir=keys_dir)


# ---- tokens ----


def test_a_token_carries_what_the_server_will_check(pair):
    token = issue_token(
        pair.private_pem,
        kid="agent",
        subject="pm-alice",
        audience="etl-agent-mcp",
        scopes=["get_schema"],
        lifetime=timedelta(hours=1),
    )

    header = jwt.get_unverified_header(token)
    claims = jwt.decode(
        token, pair.public_pem, algorithms=[ALGORITHM], audience="etl-agent-mcp"
    )
    assert header["kid"] == "agent"
    assert header["alg"] == ALGORITHM
    assert claims["sub"] == "pm-alice"
    assert claims["scopes"] == ["get_schema"]
    assert claims["exp"] > claims["iat"]


def test_the_lifetime_lands_on_exp(pair):
    issued = datetime(2026, 1, 1, tzinfo=UTC)

    token = issue_token(
        pair.private_pem,
        kid="agent",
        subject="agent",
        audience="etl-agent-mcp",
        scopes=[],
        lifetime=timedelta(days=30),
        issued_at=issued,
    )

    # the arithmetic is the point, not whether a token dated 2026 is still live
    claims = jwt.decode(
        token,
        pair.public_pem,
        algorithms=[ALGORITHM],
        audience="etl-agent-mcp",
        options={"verify_exp": False},
    )
    assert claims["exp"] - claims["iat"] == 30 * 86400


def test_a_token_without_a_subject_is_refused(pair):
    """`sub` is what the audit trail records, so a token without one would make
    every entry anonymous."""
    with pytest.raises(IssueError, match="audit trail"):
        issue_token(
            pair.private_pem,
            kid="agent",
            subject="",
            audience="etl-agent-mcp",
            scopes=[],
            lifetime=timedelta(hours=1),
        )
