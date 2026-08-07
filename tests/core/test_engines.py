"""
The engine table is read by the Dockerfile as a script and by the runtime as a
module, so both halves are tested: the mapping itself, and the little CLI that
`RUN` shells out to.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.core import engines
from src.core.engines import (
    BUILD_PACKAGES,
    DISPLAY_NAMES,
    DRIVER_MODULES,
    EXTRAS,
    SYSTEM_PACKAGES,
    SourceEngine,
    UnknownEngineError,
    extras_for,
    image_tag_for,
    packages_for,
    parse_engines,
)

SCRIPT = Path(engines.__file__)


# ---- the table ----


def test_every_engine_has_an_answer_to_every_question():
    """A new engine that nobody added to `EXTRAS` would build an image with no
    driver in it and no error to say so."""
    assert set(EXTRAS) == set(SourceEngine)
    assert set(DISPLAY_NAMES) == set(SourceEngine)


def test_sqlite_needs_nothing_installed():
    """Its driver is the standard library, which is what makes it the default."""
    assert EXTRAS[SourceEngine.SQLITE] == ()
    assert SourceEngine.SQLITE not in DRIVER_MODULES
    assert SourceEngine.SQLITE not in SYSTEM_PACKAGES


def test_only_mssql_needs_an_os_package():
    """pyodbc binds to a driver that is not a python package. Every other
    engine's driver arrives through pip, and its image should not be paying for
    an apt step or Microsoft's repository."""
    assert set(SYSTEM_PACKAGES) == {SourceEngine.MSSQL}
    assert set(BUILD_PACKAGES) == {SourceEngine.MSSQL}


def test_the_server_extra_is_always_installed():
    """The container serves over HTTP and verifies tokens whatever it points
    at, so fastmcp, pyjwt and cryptography are not optional here."""
    for engine in SourceEngine:
        assert "server" in extras_for((engine,))


def test_extras_are_ordered_and_deduplicated():
    both = extras_for((SourceEngine.MYSQL, SourceEngine.MARIADB))

    assert both == ("server", "mysql")


def test_the_driver_module_is_the_one_that_is_actually_imported():
    """`missing_driver` answers by trying to find these, so a wrong name would
    report a working image as broken."""
    assert DRIVER_MODULES[SourceEngine.MSSQL] == "pyodbc"
    assert DRIVER_MODULES[SourceEngine.POSTGRES] == "psycopg"
    assert DRIVER_MODULES[SourceEngine.DATALAKE] == "pyarrow"


# ---- parsing ----


@pytest.mark.parametrize(
    "text, expected",
    [
        ("mssql", (SourceEngine.MSSQL,)),
        ("  mssql  ", (SourceEngine.MSSQL,)),
        ("MSSQL", (SourceEngine.MSSQL,)),
        ("mssql,postgres", (SourceEngine.MSSQL, SourceEngine.POSTGRES)),
        ("mssql postgres", (SourceEngine.MSSQL, SourceEngine.POSTGRES)),
        ("mssql,mssql", (SourceEngine.MSSQL,)),
    ],
)
def test_parse_engines(text: str, expected: tuple):
    assert parse_engines(text) == expected


@pytest.mark.parametrize("text", ["oracle", "", "  ", ","])
def test_an_engine_nobody_has_an_adapter_for_is_refused(text: str):
    with pytest.raises(UnknownEngineError):
        parse_engines(text)


def test_the_error_lists_what_is_supported():
    """It is read from a failed `docker build`, where there is nothing else to
    go on."""
    with pytest.raises(UnknownEngineError, match="mssql"):
        parse_engines("oracle")


# ---- the tag ----


def test_the_tag_carries_the_engine():
    """`docker compose up` without --build reuses whatever image already has
    the name, so a tag that did not change with the engine would serve one
    engine's configuration out of another's drivers."""
    assert image_tag_for((SourceEngine.MSSQL,)) == "mssql"
    assert (
        image_tag_for((SourceEngine.MSSQL, SourceEngine.POSTGRES)) == "mssql-postgres"
    )


# ---- the CLI the Dockerfile uses ----


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def test_extras_come_out_as_uv_flags():
    result = run("extras", "mssql")

    assert result.returncode == 0
    assert result.stdout.strip() == "--extra server --extra mssql"


def test_apt_comes_out_space_separated():
    """`RUN` splits on whitespace and has no other parser."""
    result = run("apt", "mssql")

    assert result.returncode == 0
    assert result.stdout.split() == ["unixodbc", "msodbcsql18"]


def test_an_engine_needing_no_packages_prints_nothing():
    result = run("apt", "sqlite")

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_ms_repo_answers_with_the_exit_code():
    """It gates an `if` in a shell, so the answer is a status, not a word."""
    assert run("ms-repo", "mssql").returncode == 0
    assert run("ms-repo", "postgres").returncode == 1


def test_the_script_fails_loudly_on_an_unknown_engine():
    result = run("extras", "oracle")

    assert result.returncode == 2
    assert "unknown engine" in result.stderr


def test_the_script_runs_without_the_project_installed(tmp_path: Path):
    """
    The Dockerfile copies this one file into the builder before `uv sync`, so
    it runs with nothing but the standard library available. Copying it out of
    the package and running it there is the only honest way to check that.
    """
    alone = tmp_path / "engines.py"
    alone.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(alone), "extras", "postgres"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "--extra server --extra postgres"


# ---- what the image can actually serve ----


def test_a_driver_that_is_installed_is_not_reported_missing():
    # pyarrow is a dev dependency of this project, so it is here.
    assert engines.missing_driver(SourceEngine.DATALAKE) is None


def test_sqlite_is_always_servable():
    assert engines.missing_driver(SourceEngine.SQLITE) is None


def test_a_driver_that_is_absent_is_named(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(DRIVER_MODULES, SourceEngine.SQLITE, "not_a_real_module")

    assert engines.missing_driver(SourceEngine.SQLITE) == "not_a_real_module"


def test_packages_for_deduplicates_across_engines():
    assert packages_for((SourceEngine.MSSQL, SourceEngine.MSSQL), SYSTEM_PACKAGES) == (
        "unixodbc",
        "msodbcsql18",
    )
