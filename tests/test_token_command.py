"""
`mcp-connector token …`, driven through `main` so that what is tested is what
an operator actually types.
"""

import asyncio
from pathlib import Path

import pytest

import main as entry
from src.auth.keys import KEY_SUFFIX, PublicKeyDirectory
from src.auth.verifier import SignedTokenVerifier

AUDIENCE = "etl-agent-mcp"


def _run(argv: list[str]) -> int:
    """Issuing goes through the same entry point as serving, one word apart."""
    return entry.main(["token", *argv])


def test_keygen_files_the_public_half_and_keeps_the_private_one_out(
    tmp_path: Path, capsys
):
    keys_dir = tmp_path / "keys"

    assert (
        _run(
            [
                "keygen",
                "--kid",
                "agent",
                "--keys-dir",
                str(keys_dir),
                "--out",
                str(tmp_path / "agent.pem"),
            ]
        )
        == 0
    )

    assert (keys_dir / f"agent{KEY_SUFFIX}").exists()
    assert (tmp_path / "agent.pem").exists()
    assert list(keys_dir.iterdir()) == [keys_dir / f"agent{KEY_SUFFIX}"]
    printed = capsys.readouterr().out
    assert "keep this off the server" in printed
    assert "revoke with" in printed


def test_a_token_issued_by_the_cli_verifies_against_the_key_it_filed(
    tmp_path: Path, capsys
):
    """The round trip the whole design rests on: the operator runs these two
    commands and the server accepts what comes out."""
    keys_dir = tmp_path / "keys"
    private = tmp_path / "agent.pem"
    _run(
        ["keygen", "--kid", "agent", "--keys-dir", str(keys_dir), "--out", str(private)]
    )
    capsys.readouterr()

    assert (
        _run(
            [
                "issue",
                "--key",
                str(private),
                "--kid",
                "agent",
                "--subject",
                "pm-alice",
                "--scope",
                "get_schema",
                "--lifetime",
                "12h",
            ]
        )
        == 0
    )

    token = capsys.readouterr().out.strip()
    verifier = SignedTokenVerifier(PublicKeyDirectory(keys_dir), audience=AUDIENCE)
    accepted = asyncio.run(verifier.verify_token(token))
    assert accepted is not None
    assert accepted.subject == "pm-alice"
    assert accepted.scopes == ["get_schema"]


def test_the_subject_defaults_to_the_key_id(tmp_path: Path, capsys):
    keys_dir = tmp_path / "keys"
    private = tmp_path / "agent.pem"
    _run(
        [
            "keygen",
            "--kid",
            "de-inventory",
            "--keys-dir",
            str(keys_dir),
            "--out",
            str(private),
        ]
    )
    capsys.readouterr()

    _run(
        [
            "issue",
            "--key",
            str(private),
            "--kid",
            "de-inventory",
            "--scope",
            "inventory_start",
        ]
    )

    token = capsys.readouterr().out.strip()
    verifier = SignedTokenVerifier(PublicKeyDirectory(keys_dir), audience=AUDIENCE)
    accepted = asyncio.run(verifier.verify_token(token))
    assert accepted is not None and accepted.subject == "de-inventory"


def test_a_token_with_no_scope_says_so(tmp_path: Path, capsys):
    """It is valid and can call nothing, which is worth hearing about at the
    moment of issue rather than at the first tool call."""
    keys_dir = tmp_path / "keys"
    private = tmp_path / "agent.pem"
    _run(
        ["keygen", "--kid", "agent", "--keys-dir", str(keys_dir), "--out", str(private)]
    )
    capsys.readouterr()

    _run(["issue", "--key", str(private), "--kid", "agent"])

    assert "may call nothing" in capsys.readouterr().err


def test_a_bad_lifetime_is_an_error_not_a_traceback(tmp_path: Path, capsys):
    keys_dir = tmp_path / "keys"
    private = tmp_path / "agent.pem"
    _run(
        ["keygen", "--kid", "agent", "--keys-dir", str(keys_dir), "--out", str(private)]
    )
    capsys.readouterr()

    assert (
        _run(
            ["issue", "--key", str(private), "--kid", "agent", "--lifetime", "forever"]
        )
        == 2
    )
    assert "error:" in capsys.readouterr().err


def test_keygen_refuses_to_clobber_a_signing_key(tmp_path: Path, capsys):
    keys_dir = tmp_path / "keys"
    private = tmp_path / "agent.pem"
    _run(
        ["keygen", "--kid", "agent", "--keys-dir", str(keys_dir), "--out", str(private)]
    )
    before = private.read_text(encoding="utf-8")
    capsys.readouterr()

    assert (
        _run(
            [
                "keygen",
                "--kid",
                "agent",
                "--keys-dir",
                str(keys_dir),
                "--out",
                str(private),
            ]
        )
        == 2
    )

    assert private.read_text(encoding="utf-8") == before
    assert "error:" in capsys.readouterr().err


def test_token_on_its_own_is_not_a_command(capsys):
    with pytest.raises(SystemExit):
        _run([])


# ---- what a container's entrypoint does ----


def test_if_missing_keeps_the_key_a_restart_would_otherwise_replace(
    tmp_path: Path, capsys
):
    """An entrypoint runs keygen on every start. Generating a new pair would
    invalidate every token already handed out."""
    keys_dir = tmp_path / "keys"
    private = tmp_path / "agent.pem"
    _run(
        ["keygen", "--kid", "agent", "--keys-dir", str(keys_dir), "--out", str(private)]
    )
    first = private.read_text(encoding="utf-8")
    public = (keys_dir / f"agent{KEY_SUFFIX}").read_text(encoding="utf-8")
    capsys.readouterr()

    assert (
        _run(
            [
                "keygen",
                "--kid",
                "agent",
                "--keys-dir",
                str(keys_dir),
                "--out",
                str(private),
                "--if-missing",
            ]
        )
        == 0
    )

    assert private.read_text(encoding="utf-8") == first
    assert (keys_dir / f"agent{KEY_SUFFIX}").read_text(encoding="utf-8") == public
    assert "already there" in capsys.readouterr().out


def test_if_missing_generates_the_pair_the_first_time(tmp_path: Path, capsys):
    keys_dir = tmp_path / "keys"

    assert (
        _run(
            [
                "keygen",
                "--kid",
                "agent",
                "--keys-dir",
                str(keys_dir),
                "--out",
                str(tmp_path / "agent.pem"),
                "--if-missing",
            ]
        )
        == 0
    )

    assert (keys_dir / f"agent{KEY_SUFFIX}").exists()


def test_if_missing_puts_the_public_half_back_when_only_it_is_gone(
    tmp_path: Path, capsys
):
    """The signing key is on a volume that survives; the keys directory need
    not be. A server with no public key on file turns everyone away."""
    keys_dir = tmp_path / "keys"
    private = tmp_path / "agent.pem"
    _run(
        ["keygen", "--kid", "agent", "--keys-dir", str(keys_dir), "--out", str(private)]
    )
    before = (keys_dir / f"agent{KEY_SUFFIX}").read_text(encoding="utf-8")
    (keys_dir / f"agent{KEY_SUFFIX}").unlink()
    capsys.readouterr()

    assert (
        _run(
            [
                "keygen",
                "--kid",
                "agent",
                "--keys-dir",
                str(keys_dir),
                "--out",
                str(private),
                "--if-missing",
            ]
        )
        == 0
    )

    assert (keys_dir / f"agent{KEY_SUFFIX}").read_text(encoding="utf-8") == before
    assert "restored from" in capsys.readouterr().out


def test_a_token_can_be_written_to_a_file_for_the_entrypoint_to_hand_on(
    tmp_path: Path, capsys
):
    keys_dir = tmp_path / "keys"
    private = tmp_path / "agent.pem"
    _run(
        ["keygen", "--kid", "agent", "--keys-dir", str(keys_dir), "--out", str(private)]
    )
    capsys.readouterr()

    _run(
        [
            "issue",
            "--key",
            str(private),
            "--kid",
            "agent",
            "--scope",
            "get_schema",
            "--out",
            str(tmp_path / "agent.jwt"),
        ]
    )

    token = (tmp_path / "agent.jwt").read_text(encoding="utf-8")
    verifier = SignedTokenVerifier(PublicKeyDirectory(keys_dir), audience=AUDIENCE)
    assert asyncio.run(verifier.verify_token(token)) is not None
    assert capsys.readouterr().out == "", "the file is the output, not stdout"
