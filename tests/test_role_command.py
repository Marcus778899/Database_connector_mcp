"""
`mcp-connector role …` and `token issue --role`, driven through `main` so that
what is tested is what an operator actually types.

The property that matters throughout: what a role says it grants and what the
token ends up carrying are the same thing. A skill is generated from the token,
so anywhere those two drift is a document that describes access nobody has.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import main as entry
from src.auth.keys import PublicKeyDirectory
from src.auth.permissions import Permissions
from src.auth.verifier import SignedTokenVerifier
from src.core.roles import ENV_ROLES_FILE

AUDIENCE = "etl-agent-mcp"
SHIPPED = Path(__file__).resolve().parents[1] / "docker" / "roles.toml"

ROLES = """
[roles.reader]
description = "reads the catalog"
tools = ["get_schema", "inventory_search"]
containers = { deny = ["*_pii"] }
lifetime = "7d"

[roles.writer]
extends = "reader"
add_tools = ["inventory_annotate"]
annotate_as_human = true
"""


@pytest.fixture
def roles_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "roles.toml"
    path.write_text(ROLES, encoding="utf-8")
    monkeypatch.setenv(ENV_ROLES_FILE, str(path))
    return path


@pytest.fixture
def signing_key(tmp_path: Path, capsys) -> tuple[Path, Path]:
    """A key pair, and the directory a verifier would read."""
    keys_dir = tmp_path / "keys"
    private = tmp_path / "agent.pem"
    entry.main(
        [
            "token",
            "keygen",
            "--kid",
            "agent",
            "--keys-dir",
            str(keys_dir),
            "--out",
            str(private),
        ]
    )
    capsys.readouterr()
    return private, keys_dir


def issue(private: Path, *args: str) -> list[str]:
    return ["token", "issue", "--key", str(private), "--kid", "agent", *args]


def granted(token: str, keys_dir: Path) -> Permissions:
    """What the server would make of this token — the only answer that counts."""
    verifier = SignedTokenVerifier(PublicKeyDirectory(keys_dir), audience=AUDIENCE)
    accepted = asyncio.run(verifier.verify_token(token))
    assert accepted is not None, "the server would have refused this token"
    return Permissions.from_claims(
        list(accepted.scopes or []), getattr(accepted, "claims", None) or {}
    )


# ---- role list / show ----


def test_role_list_names_every_role(roles_file: Path, capsys):
    assert entry.main(["role", "list"]) == 0

    out = capsys.readouterr().out
    assert "reader" in out
    assert "writer" in out
    assert "reads the catalog" in out


def test_role_show_resolves_the_inheritance(roles_file: Path, capsys):
    """A role that extends another does not read as what it grants, which is
    the whole reason this command exists."""
    assert entry.main(["role", "show", "writer"]) == 0

    out = capsys.readouterr().out
    assert "get_schema" in out  # inherited
    assert "inventory_annotate" in out  # its own
    assert "*_pii" in out  # inherited restriction


def test_role_show_spells_out_the_two_settings_worth_reviewing(
    roles_file: Path, capsys
):
    entry.main(["role", "show", "reader"])
    out = capsys.readouterr().out

    assert "raw rows:   no" in out
    assert "an agent" in out


def test_role_show_for_a_role_that_does_not_exist_lists_the_ones_that_do(
    roles_file: Path, capsys
):
    assert entry.main(["role", "show", "nope"]) == 2

    err = capsys.readouterr().err
    assert "reader" in err and "writer" in err


def test_a_broken_roles_file_is_an_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    path = tmp_path / "roles.toml"
    path.write_text('[roles.a]\ntools = ["nope"]\n', encoding="utf-8")
    monkeypatch.setenv(ENV_ROLES_FILE, str(path))

    assert entry.main(["role", "list"]) == 2
    assert "no tool for" in capsys.readouterr().err


# ---- issuing from a role ----


def test_a_role_becomes_the_tokens_grants(
    roles_file: Path, signing_key: tuple[Path, Path], capsys
):
    private, keys_dir = signing_key

    assert entry.main(issue(private, "--role", "writer")) == 0

    rights = granted(capsys.readouterr().out.strip(), keys_dir)
    assert set(rights.tools) == {"get_schema", "inventory_search", "inventory_annotate"}
    assert rights.annotate_as_human is True
    assert not rights.may_read("customer_pii")
    assert rights.may_read("dim_product")


def test_the_roles_lifetime_is_used_when_none_was_asked_for(
    roles_file: Path, signing_key: tuple[Path, Path], capsys
):
    """`--lifetime` has a default, so it cannot be told apart from an explicit
    one. The role's own answer has to win over a default nobody chose."""
    private, keys_dir = signing_key
    entry.main(issue(private, "--role", "reader"))

    token = capsys.readouterr().out.strip()
    payload = _payload(token)
    assert payload["exp"] - payload["iat"] == 7 * 86400


def test_an_explicit_lifetime_still_wins(
    roles_file: Path, signing_key: tuple[Path, Path], capsys
):
    private, _ = signing_key
    entry.main(issue(private, "--role", "reader", "--lifetime", "1h"))

    payload = _payload(capsys.readouterr().out.strip())
    assert payload["exp"] - payload["iat"] == 3600


def test_flags_add_to_a_role_rather_than_replacing_it(
    roles_file: Path, signing_key: tuple[Path, Path], capsys
):
    private, keys_dir = signing_key
    entry.main(issue(private, "--role", "reader", "--scope", "get_sample"))

    rights = granted(capsys.readouterr().out.strip(), keys_dir)
    assert set(rights.tools) == {"get_schema", "inventory_search", "get_sample"}


def test_a_flag_can_widen_a_role_on_purpose(
    roles_file: Path, signing_key: tuple[Path, Path], capsys
):
    """Widening is a thing an operator does deliberately — a short-lived token
    that may see raw rows, without editing the role every holder shares."""
    private, keys_dir = signing_key
    entry.main(issue(private, "--role", "reader", "--allow-raw-sample"))

    assert granted(capsys.readouterr().out.strip(), keys_dir).allow_raw_sample is True


def test_a_role_that_does_not_exist_is_an_error_not_a_traceback(
    roles_file: Path, signing_key: tuple[Path, Path], capsys
):
    private, _ = signing_key

    assert entry.main(issue(private, "--role", "nope")) == 2

    err = capsys.readouterr().err
    assert "no role" in err and "reader" in err


def test_issuing_without_a_role_or_a_scope_says_the_token_is_useless(
    roles_file: Path, signing_key: tuple[Path, Path], capsys
):
    private, _ = signing_key
    entry.main(issue(private))

    assert "may call nothing" in capsys.readouterr().err


# ---- the shipped roles, end to end ----


@pytest.mark.parametrize("role", ["pm", "de"])
def test_the_shipped_roles_produce_tokens_the_server_accepts(
    role: str, signing_key: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv(ENV_ROLES_FILE, str(SHIPPED))
    private, keys_dir = signing_key

    assert entry.main(issue(private, "--role", role)) == 0

    rights = granted(capsys.readouterr().out.strip(), keys_dir)
    assert rights.may_call("get_schema")
    assert rights.allow_raw_sample is False


def test_pm_cannot_start_a_scan_and_de_can(
    signing_key: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys
):
    """The one difference between the two roles that a customer would notice."""
    monkeypatch.setenv(ENV_ROLES_FILE, str(SHIPPED))
    private, keys_dir = signing_key

    entry.main(issue(private, "--role", "pm"))
    pm = granted(capsys.readouterr().out.strip(), keys_dir)
    entry.main(issue(private, "--role", "de"))
    de = granted(capsys.readouterr().out.strip(), keys_dir)

    assert not pm.may_call("inventory_start")
    assert not pm.may_call("inventory_annotate")
    assert de.may_call("inventory_start")
    assert de.may_call("inventory_annotate")


def _payload(token: str) -> dict:
    import base64

    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
