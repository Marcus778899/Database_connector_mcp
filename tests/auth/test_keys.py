from pathlib import Path

import pytest

from src.auth.keys import (
    KEY_SUFFIX,
    KeyDirectoryError,
    MalformedKeyIdError,
    PublicKeyDirectory,
    UnknownKeyError,
    valid_kid,
)


@pytest.fixture
def keys_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "keys"
    directory.mkdir()
    (directory / f"pm-explorer{KEY_SUFFIX}").write_text("PEM-A", encoding="utf-8")
    (directory / f"de_inventory{KEY_SUFFIX}").write_text("PEM-B", encoding="utf-8")
    return directory


# ---- what counts as a key id ----


@pytest.mark.parametrize(
    "kid", ["a", "pm-explorer", "de_inventory", "agent1", "A" * 64]
)
def test_a_plain_name_is_a_key_id(kid: str):
    assert valid_kid(kid) is True


@pytest.mark.parametrize(
    "kid",
    [
        "",
        "..",
        "../../etc/passwd",
        "sub/dir",
        "sub\\dir",
        "has space",
        "dotted.name",
        "A" * 65,
    ],
)
def test_anything_that_could_leave_the_directory_is_not(kid: str):
    """The kid comes out of a token header that has not been verified yet, so it
    is attacker-controlled input used to build a path."""
    assert valid_kid(kid) is False


def test_a_traversing_kid_is_refused_before_it_reaches_the_filesystem(keys_dir: Path):
    directory = PublicKeyDirectory(keys_dir)

    with pytest.raises(MalformedKeyIdError):
        directory.get("../../../etc/passwd")


# ---- reading ----


def test_a_key_on_file_is_returned(keys_dir: Path):
    assert PublicKeyDirectory(keys_dir).get("pm-explorer") == "PEM-A"


def test_a_key_not_on_file_is_unknown(keys_dir: Path):
    with pytest.raises(UnknownKeyError):
        PublicKeyDirectory(keys_dir).get("nobody")


def test_the_directory_lists_who_is_allowed_in(keys_dir: Path):
    assert PublicKeyDirectory(keys_dir).kids() == ["de_inventory", "pm-explorer"]


def test_a_file_that_is_not_a_key_id_is_not_listed(keys_dir: Path):
    (keys_dir / f"not a kid{KEY_SUFFIX}").write_text("PEM", encoding="utf-8")
    (keys_dir / "README.md").write_text("notes", encoding="utf-8")

    assert PublicKeyDirectory(keys_dir).kids() == ["de_inventory", "pm-explorer"]


def test_a_missing_directory_says_what_it_should_hold(tmp_path: Path):
    with pytest.raises(KeyDirectoryError, match="one <kid>"):
        PublicKeyDirectory(tmp_path / "absent")


# ---- caching and revocation ----


def test_a_key_is_not_re_read_within_the_ttl(keys_dir: Path):
    directory = PublicKeyDirectory(keys_dir, ttl=30)
    directory.get("pm-explorer")

    (keys_dir / f"pm-explorer{KEY_SUFFIX}").write_text("CHANGED", encoding="utf-8")

    assert directory.get("pm-explorer") == "PEM-A"


def test_deleting_a_key_revokes_it_once_the_ttl_is_up(keys_dir: Path):
    """`rm <kid>.pub` is the revocation mechanism, so it has to actually take
    effect without a restart."""
    directory = PublicKeyDirectory(keys_dir, ttl=0)
    directory.get("pm-explorer")

    (keys_dir / f"pm-explorer{KEY_SUFFIX}").unlink()

    with pytest.raises(UnknownKeyError):
        directory.get("pm-explorer")


def test_a_revoked_key_does_not_survive_in_the_cache(keys_dir: Path):
    directory = PublicKeyDirectory(keys_dir, ttl=0)
    directory.get("pm-explorer")
    (keys_dir / f"pm-explorer{KEY_SUFFIX}").unlink()
    with pytest.raises(UnknownKeyError):
        directory.get("pm-explorer")

    (keys_dir / f"pm-explorer{KEY_SUFFIX}").write_text("REISSUED", encoding="utf-8")

    assert directory.get("pm-explorer") == "REISSUED"


def test_forgetting_forces_a_re_read(keys_dir: Path):
    directory = PublicKeyDirectory(keys_dir, ttl=3600)
    directory.get("pm-explorer")
    (keys_dir / f"pm-explorer{KEY_SUFFIX}").write_text("ROTATED", encoding="utf-8")

    directory.forget()

    assert directory.get("pm-explorer") == "ROTATED"
