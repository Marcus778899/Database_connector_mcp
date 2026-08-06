"""
Guards the dependency direction between `src` packages.

The rule is what makes the layout meaningful: `core` holds the contracts every
other layer agrees on, so it must not know about any implementation. Without a
test, that direction quietly rots the first time something is convenient.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# unit -> the src packages it may import (itself is always allowed). A unit is a
# package directory or a top-level module such as src/server.py.
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "utils": set(),
    "adapter": {"core", "utils"},
    "auth": {"core", "utils"},
    # service composes everything, including importing adapters by module path
    "service": {"core", "utils", "adapter", "auth"},
    # server.py is the outward-facing protocol surface, so it may use any of them
    "server": {"core", "utils", "adapter", "auth", "service"},
}


def _units() -> dict[str, list[Path]]:
    """Each unit and the files it is made of."""
    units: dict[str, list[Path]] = {}
    for entry in SRC.iterdir():
        if entry.name.startswith("_"):
            continue
        if entry.is_dir():
            units[entry.name] = sorted(entry.rglob("*.py"))
        elif entry.suffix == ".py":
            units[entry.stem] = [entry]
    return units


def _src_imports(path: Path) -> set[str]:
    """The `src.<package>` names a module imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) >= 2 and parts[0] == "src":
                    found.add(parts[1])
            continue
        else:
            continue
        if len(parts) >= 2 and parts[0] == "src":
            found.add(parts[1])
    return found


def test_every_unit_has_a_rule():
    assert set(_units()) == set(ALLOWED)


@pytest.mark.parametrize("unit", sorted(ALLOWED))
def test_a_unit_only_imports_what_its_layer_allows(unit: str):
    allowed = ALLOWED[unit] | {unit}
    violations: list[str] = []

    for path in _units()[unit]:
        for imported in sorted(_src_imports(path) - allowed):
            violations.append(f"{path.relative_to(SRC.parent)} imports src.{imported}")

    assert not violations, "\n".join(violations)


def test_core_stays_free_of_implementations():
    """Spelled out separately because this is the rule that matters most: the
    factory used to live in core and reached into src.adapter via importlib,
    inverting the dependency behind a dynamic import."""
    for path in sorted((SRC / "core").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "src.adapter" not in source, f"{path.name} references src.adapter"
        assert "src.service" not in source, f"{path.name} references src.service"
