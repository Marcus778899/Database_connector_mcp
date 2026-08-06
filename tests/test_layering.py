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

# package -> the src packages it may import (itself is always allowed)
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "utils": set(),
    "adapter": {"core", "utils"},
    "auth": {"core", "utils"},
    # service composes everything, including importing adapters by module path
    "service": {"core", "utils", "adapter", "auth"},
}


def _packages() -> list[str]:
    return sorted(
        p.name for p in SRC.iterdir() if p.is_dir() and not p.name.startswith("_")
    )


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


def test_every_package_has_a_rule():
    assert set(_packages()) == set(ALLOWED)


@pytest.mark.parametrize("package", sorted(ALLOWED))
def test_package_only_imports_what_its_layer_allows(package: str):
    allowed = ALLOWED[package] | {package}
    violations: list[str] = []

    for path in sorted((SRC / package).rglob("*.py")):
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
