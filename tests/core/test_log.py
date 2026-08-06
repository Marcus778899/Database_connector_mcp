import threading
from datetime import datetime
from pathlib import Path

import pytest

from src.core.log import _LazyLogger, log, log_errors


def _log_file(log_dir: Path, level: str) -> Path:
    return log_dir / datetime.now().strftime("%Y-%m-%d") / f"{level}.log"


@pytest.fixture
def logger(tmp_path: Path) -> _LazyLogger:
    return _LazyLogger(log_dir=str(tmp_path / "log"))


# ---- laziness ----


def test_building_the_wrapper_touches_nothing(tmp_path: Path):
    log_dir = tmp_path / "log"

    _LazyLogger(log_dir=str(log_dir))

    assert not log_dir.exists()


def test_the_first_message_creates_the_log_directory(tmp_path: Path):
    log_dir = tmp_path / "log"
    logger = _LazyLogger(log_dir=str(log_dir))

    logger.info("hello")

    assert _log_file(log_dir, "INFO").exists()


def test_only_one_underlying_logger_is_ever_built(logger: _LazyLogger):
    logger.info("first")
    first = logger._logger

    logger.info("second")

    assert logger._logger is first


def test_concurrent_first_use_still_builds_one_logger(logger: _LazyLogger):
    seen: list[object] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        logger.info("concurrent")
        seen.append(logger._logger)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(item) for item in seen}) == 1


# ---- levels ----


@pytest.mark.parametrize(
    ("method", "level"),
    [("info", "INFO"), ("warning", "WARNING"), ("critical", "CRITICAL")],
)
def test_each_level_lands_in_its_own_file(tmp_path: Path, method: str, level: str):
    log_dir = tmp_path / "log"
    logger = _LazyLogger(log_dir=str(log_dir))

    getattr(logger, method)(f"a {level} message")

    assert f"a {level} message" in _log_file(log_dir, level).read_text()


def test_warning_is_exposed_under_the_standard_name(logger: _LazyLogger):
    logger.warning("careful")

    assert hasattr(logger, "warning")
    assert not hasattr(logger, "warn")


def test_levels_do_not_bleed_into_each_other(tmp_path: Path):
    log_dir = tmp_path / "log"
    logger = _LazyLogger(log_dir=str(log_dir))

    logger.critical("only critical")

    assert not _log_file(log_dir, "INFO").read_text()
    assert "only critical" in _log_file(log_dir, "CRITICAL").read_text()


def test_log_level_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    log_dir = tmp_path / "log"
    logger = _LazyLogger(log_dir=str(log_dir))

    logger.info("dropped")
    logger.warning("kept")

    assert not _log_file(log_dir, "INFO").read_text()
    assert "kept" in _log_file(log_dir, "WARNING").read_text()


def test_log_dir_comes_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "from_env"))

    _LazyLogger().info("hello")

    assert _log_file(tmp_path / "from_env", "INFO").exists()


# ---- log.error: swallows ----


def test_error_decorator_swallows_and_returns_none(logger: _LazyLogger):
    @logger.error
    def explode() -> str:
        raise RuntimeError("boom")

    assert explode() is None


def test_error_decorator_records_the_traceback(tmp_path: Path):
    log_dir = tmp_path / "log"
    logger = _LazyLogger(log_dir=str(log_dir))

    @logger.error
    def explode() -> None:
        raise RuntimeError("boom")

    explode()

    written = _log_file(log_dir, "ERROR").read_text()
    assert "explode" in written
    assert "boom" in written
    assert "Traceback" in written


def test_error_decorator_passes_arguments_and_results_through(logger: _LazyLogger):
    @logger.error
    def add(a: int, b: int = 0) -> int:
        return a + b

    assert add(1, b=2) == 3


def test_error_decorator_keeps_the_wrapped_identity(logger: _LazyLogger):
    @logger.error
    def documented() -> None:
        """my docstring"""

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "my docstring"


def test_decorating_does_not_resolve_the_logger(tmp_path: Path):
    log_dir = tmp_path / "log"
    logger = _LazyLogger(log_dir=str(log_dir))

    @logger.error
    def never_called() -> None:
        pass

    assert not log_dir.exists()


# ---- log_errors: re-raises ----


def test_log_errors_reraises(logger: _LazyLogger):
    @log_errors
    def explode() -> None:
        raise KeyError("missing")

    with pytest.raises(KeyError, match="missing"):
        explode()


def test_log_errors_returns_normally_on_success():
    @log_errors
    def double(value: int) -> int:
        return value * 2

    assert double(21) == 42


def test_log_errors_is_the_one_to_use_where_errors_are_contractual():
    """Picking the wrong one turns a precise exception into a stray None."""

    @log.error
    def swallowing() -> None:
        raise ValueError("contract violation")

    @log_errors
    def propagating() -> None:
        raise ValueError("contract violation")

    assert swallowing() is None
    with pytest.raises(ValueError):
        propagating()
