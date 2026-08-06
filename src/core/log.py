from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable
from traceback import format_exc
from typing import Any, TypeVar

from loggerhelper import Logger

F = TypeVar("F", bound=Callable[..., Any])


class _LazyLogger:
    """
    The shared logger, built on first use rather than at import — `Logger()`
    creates `log/<date>/` as it is constructed, and `@log.error` has to be
    usable as a decorator. One instance only, or every line is logged twice.

    Delete this once loggerhelper builds its handlers on first emit; it then
    collapses to `log = Logger(...)`.
    """

    def __init__(self, log_dir: str | None = None, level: str | None = None) -> None:
        self._log_dir = log_dir
        self._level = level
        self._logger: Logger | None = None
        self._lock = threading.Lock()

    def _resolve(self) -> Logger:
        if self._logger is None:
            with self._lock:
                if self._logger is None:
                    self._logger = self._build()
        return self._logger

    def _build(self) -> Logger:
        level = self._level or os.environ.get("LOG_LEVEL", "INFO")
        log_dir = self._log_dir or os.environ.get("LOG_DIR")
        # loggerhelper annotates log_dir as `str` but defaults it to None, so
        # omit the argument rather than pass None.
        if log_dir is None:
            return Logger(level=level)
        return Logger(log_dir=log_dir, level=level)

    # ---- levels ----

    def info(self, message: str) -> None:
        """Worth seeing in a normal run."""
        self._resolve().info(message)

    def warning(self, message: str) -> None:
        """Handled, but someone should know."""
        self._resolve().warn(message)

    def critical(self, message: str) -> None:
        """The service cannot do its job."""
        self._resolve().critical(message)

    # ---- decorator ----

    def error(self, func: F) -> F:
        """
        Log the traceback and **swallow** the exception, returning None. Only
        where continuing is right: a background loop, a teardown, an entry
        point. Elsewhere use `log_errors`.
        """
        wrapped: Callable[..., Any] | None = None

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            nonlocal wrapped
            if wrapped is None:
                wrapped = self._resolve().error(func)
            return wrapped(*args, **kwargs)

        return wrapper  # type: ignore[return-value]


log = _LazyLogger()


def log_errors(func: F) -> F:
    """
    Log the traceback, then re-raise: for anything whose failure the caller must
    still see. Warning level, since the request failed and not the service.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            log.warning(f"{func.__qualname__} raised {type(exc).__name__}: {exc}")
            log.warning(format_exc())
            raise

    return wrapper  # type: ignore[return-value]
