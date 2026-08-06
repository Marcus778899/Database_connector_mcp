import importlib
from pathlib import Path

import pytest

from src.core.config import ConnectionInfo, SourceEngine
from src.core.contracts import SourceAdaptor
from src.service import factory
from src.service.factory import (
    AdapterFactory,
    AdapterNotAvailableError,
    UnknownEngineError,
    create_adapter,
    load_adapter_class,
    resolve_engine,
)

# ---- registry hygiene ----


def test_every_engine_has_an_adapter_registered():
    assert set(factory._ADAPTER_REGISTRY) == set(SourceEngine)


def test_registry_module_paths_match_the_package_layout():
    for module_path, _ in factory._ADAPTER_REGISTRY.values():
        assert module_path.startswith("src.adapter.")


def test_engine_extras_are_real_optional_dependencies():
    declared = _declared_extras()
    assert set(factory._ENGINE_EXTRA.values()) <= declared


def _declared_extras() -> set[str]:
    import tomllib

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as handle:
        return set(tomllib.load(handle)["project"]["optional-dependencies"])


# ---- resolve_engine ----


def test_resolve_engine_accepts_enum_and_string():
    assert resolve_engine(SourceEngine.DATALAKE) is SourceEngine.DATALAKE
    assert resolve_engine("datalake") is SourceEngine.DATALAKE


def test_resolve_engine_rejects_unknown_names_with_one_error_type():
    with pytest.raises(UnknownEngineError, match="unknown engine 'oracle'") as excinfo:
        resolve_engine("oracle")
    # the message lists what is supported, so the caller can fix it
    assert "datalake" in str(excinfo.value)


def test_create_adapter_rejects_unknown_engine():
    with pytest.raises(UnknownEngineError):
        create_adapter("oracle", ConnectionInfo(path="/tmp"))


# ---- load_adapter_class ----


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        (SourceEngine.SQLITE, "SqliteAdapter"),
        (SourceEngine.DATALAKE, "DatalakeAdapter"),
    ],
)
def test_load_implemented_adapter(engine: SourceEngine, expected: str):
    assert load_adapter_class(engine).__name__ == expected


@pytest.mark.parametrize("engine", [SourceEngine.SQLITE, SourceEngine.DATALAKE])
def test_adapter_class_satisfies_the_factory_protocol(engine: SourceEngine):
    assert isinstance(load_adapter_class(engine), AdapterFactory)


@pytest.mark.parametrize(
    "engine",
    [
        SourceEngine.POSTGRES,
        SourceEngine.MYSQL,
        SourceEngine.MSSQL,
        SourceEngine.MONGODB,
    ],
)
def test_unimplemented_adapter_says_so_instead_of_blaming_the_driver(
    engine: SourceEngine,
):
    """A missing adapter module must not be reported as a missing driver."""
    with pytest.raises(AdapterNotAvailableError) as excinfo:
        load_adapter_class(engine)

    message = str(excinfo.value)
    assert "not implemented yet" in message
    assert "uv sync" not in message


def test_missing_driver_points_at_the_right_extra(monkeypatch: pytest.MonkeyPatch):
    def fake_import(name: str):
        raise ImportError(
            f"No module named 'psycopg' (importing {name})", name="psycopg"
        )

    monkeypatch.setattr(importlib, "import_module", fake_import)

    with pytest.raises(AdapterNotAvailableError) as excinfo:
        load_adapter_class(SourceEngine.POSTGRES)

    message = str(excinfo.value)
    assert "uv sync --extra postgres" in message
    assert "psycopg" in message
    assert "not implemented" not in message


def test_import_error_inside_the_adapter_is_not_mistaken_for_a_missing_driver(
    monkeypatch: pytest.MonkeyPatch,
):
    """A typo inside an adapter module is not "not implemented"."""

    def fake_import(name: str):
        raise ImportError("No module named 'sre_typo'", name="sre_typo")

    monkeypatch.setattr(importlib, "import_module", fake_import)

    with pytest.raises(AdapterNotAvailableError) as excinfo:
        load_adapter_class(SourceEngine.DATALAKE)

    assert "not implemented" not in str(excinfo.value)


def test_missing_class_in_an_existing_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(
        factory._ADAPTER_REGISTRY,
        SourceEngine.DATALAKE,
        ("src.adapter.datalake", "TypoAdapter"),
    )

    with pytest.raises(AdapterNotAvailableError, match="defines no TypoAdapter"):
        load_adapter_class(SourceEngine.DATALAKE)


def test_unregistered_engine(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delitem(factory._ADAPTER_REGISTRY, SourceEngine.DATALAKE)

    with pytest.raises(UnknownEngineError, match="no adapter registered"):
        load_adapter_class(SourceEngine.DATALAKE)


# ---- create_adapter ----


def test_create_adapter_builds_a_working_adapter(tmp_path: Path):
    adapter = create_adapter(SourceEngine.DATALAKE, ConnectionInfo(path=str(tmp_path)))

    assert isinstance(adapter, SourceAdaptor)
    assert adapter.list_databases() == ["datalake"]
    assert adapter.list_containers().containers == []


def test_create_adapter_forwards_its_arguments(tmp_path: Path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    (tmp_path / "t").mkdir()
    pq.write_table(pa.table({"a": [1, 2, 3]}), tmp_path / "t" / "part-0.parquet")

    adapter = create_adapter(
        SourceEngine.DATALAKE,
        ConnectionInfo(path=str(tmp_path)),
        database="lake2",
        max_sample_limit=1,
    )

    assert adapter.list_databases() == ["lake2"]
    assert len(adapter.get_sample("t", limit=99)) == 1


def test_create_adapter_lets_connection_errors_through(tmp_path: Path):
    """A bad connection is the caller's problem, not an availability one."""
    with pytest.raises(ValueError, match="requires a <REF>_URI"):
        create_adapter(SourceEngine.DATALAKE, ConnectionInfo(host="localhost"))
