"""
The roles file is a permissions document, so the way it fails matters as much
as the way it works. Every test below that expects a `RoleError` is guarding
against the same thing: a file that looks like it grants one thing and quietly
grants another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.roles import (
    ENV_ROLES_FILE,
    Role,
    RoleError,
    find_roles_file,
    load_roles,
)

SHIPPED = Path(__file__).resolve().parents[2] / "docker" / "roles.toml"


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "roles.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ---- the file that ships ----


def test_the_shipped_roles_load():
    roles = load_roles(SHIPPED)

    assert set(roles) == {"pm", "de"}


def test_de_can_do_everything_pm_can():
    """DE extends PM rather than repeating it, and the point of that is this
    property. If it stops holding, the inheritance is lying."""
    roles = load_roles(SHIPPED)

    assert set(roles["pm"].tools) <= set(roles["de"].tools)


def test_de_runs_the_scans_and_pm_does_not():
    roles = load_roles(SHIPPED)

    assert "inventory_start" in roles["de"].tools
    assert "inventory_annotate" in roles["de"].tools
    assert "inventory_start" not in roles["pm"].tools
    assert "inventory_annotate" not in roles["pm"].tools


def test_pm_can_read_the_inventory_and_sample_from_it():
    """The role exists so a person can answer a customer's question about the
    data without writing SQL; these are the tools that takes."""
    pm = load_roles(SHIPPED)["pm"]

    for tool in ("inventory_search", "inventory_columns", "get_sample", "get_schema"):
        assert tool in pm.tools


def test_neither_shipped_role_sees_unmasked_rows():
    """Raw sampling is a deliberate, separate grant. A default that hands it to
    every role would make the masking pointless."""
    roles = load_roles(SHIPPED)

    assert not roles["pm"].allow_raw_sample
    assert not roles["de"].allow_raw_sample


def test_neither_shipped_role_writes_descriptions_as_a_person():
    """An agent's guesses are an agent's guesses even when a data engineer
    asked for them."""
    roles = load_roles(SHIPPED)

    assert not roles["pm"].annotate_as_human
    assert not roles["de"].annotate_as_human


# ---- inheritance ----


def test_add_tools_extends_the_parents_list(tmp_path: Path):
    roles = load_roles(
        write(
            tmp_path,
            """
            [roles.base]
            tools = ["get_schema"]

            [roles.child]
            extends = "base"
            add_tools = ["get_sample"]
            """,
        )
    )

    assert roles["child"].tools == ("get_schema", "get_sample")


def test_drop_tools_narrows_the_parents_list(tmp_path: Path):
    roles = load_roles(
        write(
            tmp_path,
            """
            [roles.base]
            tools = ["get_schema", "get_sample"]

            [roles.child]
            extends = "base"
            drop_tools = ["get_sample"]
            """,
        )
    )

    assert roles["child"].tools == ("get_schema",)


def test_dropping_something_the_parent_never_had_is_an_error(tmp_path: Path):
    """Otherwise a renamed tool leaves a role silently broader than it reads."""
    with pytest.raises(RoleError, match="does not inherit"):
        load_roles(
            write(
                tmp_path,
                """
                [roles.base]
                tools = ["get_schema"]

                [roles.child]
                extends = "base"
                drop_tools = ["get_sample"]
                """,
            )
        )


def test_tools_and_add_tools_together_are_refused(tmp_path: Path):
    with pytest.raises(RoleError, match="both tools and add_tools"):
        load_roles(
            write(
                tmp_path,
                """
                [roles.base]
                tools = ["get_schema"]

                [roles.child]
                extends = "base"
                tools = ["get_sample"]
                add_tools = ["profile_column"]
                """,
            )
        )


def test_scalars_are_inherited_and_overridable(tmp_path: Path):
    roles = load_roles(
        write(
            tmp_path,
            """
            [roles.base]
            tools = ["get_schema"]
            lifetime = "30d"
            databases = ["shop"]
            containers = { deny = ["*_pii"] }

            [roles.inherits]
            extends = "base"

            [roles.overrides]
            extends = "base"
            lifetime = "1h"
            allow_raw_sample = true
            """,
        )
    )

    assert roles["inherits"].lifetime == "30d"
    assert roles["inherits"].databases == ("shop",)
    assert roles["inherits"].deny_containers == ("*_pii",)
    assert roles["overrides"].lifetime == "1h"
    assert roles["overrides"].allow_raw_sample is True
    # Inherited, not reset, by a sibling setting
    assert roles["overrides"].deny_containers == ("*_pii",)


def test_a_circle_of_inheritance_is_reported(tmp_path: Path):
    with pytest.raises(RoleError, match="circle"):
        load_roles(
            write(
                tmp_path,
                """
                [roles.a]
                extends = "b"
                tools = ["get_schema"]

                [roles.b]
                extends = "a"
                tools = ["get_schema"]
                """,
            )
        )


def test_extending_a_role_that_does_not_exist_is_reported(tmp_path: Path):
    with pytest.raises(RoleError, match="not a role"):
        load_roles(
            write(
                tmp_path,
                """
                [roles.child]
                extends = "nobody"
                tools = ["get_schema"]
                """,
            )
        )


# ---- refusing what would silently do nothing ----


def test_a_tool_that_does_not_exist_is_refused(tmp_path: Path):
    """A scope matching no tool grants nothing, so it is always a typo — and an
    invisible one, since the token still signs."""
    with pytest.raises(RoleError, match="no tool for"):
        load_roles(
            write(tmp_path, '[roles.oops]\ntools = ["get_schemas"]\n'),
        )


def test_a_role_with_no_tools_is_refused(tmp_path: Path):
    with pytest.raises(RoleError, match="grants no tools"):
        load_roles(write(tmp_path, "[roles.empty]\ntools = []\n"))


def test_a_setting_nobody_recognises_is_refused(tmp_path: Path):
    """The worst failure available to a permissions file is ignoring a line."""
    with pytest.raises(RoleError, match="mean nothing here"):
        load_roles(
            write(
                tmp_path,
                '[roles.oops]\ntools = ["get_schema"]\nallow_raw_samples = true\n',
            )
        )


def test_a_container_key_nobody_recognises_is_refused(tmp_path: Path):
    with pytest.raises(RoleError, match="only.*allow and deny"):
        load_roles(
            write(
                tmp_path,
                '[roles.oops]\ntools = ["get_schema"]\n'
                'containers = { forbid = ["x"] }\n',
            )
        )


def test_a_file_with_no_roles_is_refused(tmp_path: Path):
    with pytest.raises(RoleError, match="defines no"):
        load_roles(write(tmp_path, "# nothing here\n"))


@pytest.mark.parametrize(
    "body, message",
    [
        ('[roles.a]\ntools = "get_schema"\n', "list of strings"),
        (
            '[roles.a]\ntools = ["get_schema"]\nallow_raw_sample = "yes"\n',
            "true or false",
        ),
        ('[roles.a]\ntools = ["get_schema"]\nextends = 3\n', "a role name"),
    ],
)
def test_the_wrong_type_is_reported_rather_than_coerced(
    tmp_path: Path, body: str, message: str
):
    with pytest.raises(RoleError, match=message):
        load_roles(write(tmp_path, body))


# ---- finding the file ----


def test_the_env_var_names_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = write(tmp_path, '[roles.a]\ntools = ["get_schema"]\n')
    monkeypatch.setenv(ENV_ROLES_FILE, str(path))

    assert find_roles_file() == path


def test_an_env_var_pointing_nowhere_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(ENV_ROLES_FILE, str(tmp_path / "absent.toml"))

    with pytest.raises(RoleError, match="not a file"):
        find_roles_file()


def test_the_checkout_is_found_without_being_told(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENV_ROLES_FILE, raising=False)

    assert find_roles_file() == SHIPPED


# ---- what a role becomes ----


def test_issue_kwargs_are_what_issue_token_takes():
    role = Role(
        name="x",
        tools=("get_schema",),
        databases=("shop",),
        deny_containers=("*_pii",),
        allow_raw_sample=True,
    )

    assert role.issue_kwargs() == {
        "scopes": ["get_schema"],
        "databases": ["shop"],
        "allow_containers": [],
        "deny_containers": ["*_pii"],
        "allow_raw_sample": True,
        "annotate_as_human": False,
    }
