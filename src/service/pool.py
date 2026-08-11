from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from loggerhelper import log

from src.core.config import ConnectionInfo, SourceEngine
from src.core.contracts import SourceAdaptor
from src.service.factory import create_adapter, load_adapter_class, resolve_engine


class UnknownDatabaseError(Exception):
    """The requested database cannot be served by this provider."""


@runtime_checkable
class AdapterProvider(Protocol):
    """How the tool layer gets an adapter, so callers need not know whether they
    hold one adapter or a pool."""

    def get(self, database: str | None = None) -> SourceAdaptor: ...

    def list_databases(self) -> list[str]: ...

    def close(self) -> None: ...


class SingleAdapter:
    """Provider wrapping one already-built adapter."""

    def __init__(self, adapter: SourceAdaptor, *, database: str | None = None) -> None:
        self._adapter = adapter
        self._database = database

    def get(self, database: str | None = None) -> SourceAdaptor:
        if database is None or database == self._database:
            return self._adapter
        if self._database is not None:
            raise UnknownDatabaseError(
                f"this provider only serves {self._database!r}, got {database!r}"
            )
        # Undeclared: accept whatever the adapter itself reports, or a caller
        # could not pass back a name it just read from list_databases.
        served = self._adapter.list_databases()
        if database in served:
            return self._adapter
        raise UnknownDatabaseError(
            f"this provider only serves {served}, got {database!r}"
        )

    def list_databases(self) -> list[str]:
        return self._adapter.list_databases()

    def close(self) -> None:
        self._adapter.close()


@dataclass
class _Entry:
    adapter: SourceAdaptor
    last_used: float = 0.0


class AdapterPool:
    """
    One adapter per database, created on demand, reused, released when idle.

    Three limits for three failure modes: `max_size` bounds how many
    connections accumulate, `idle_timeout` closes unused ones (swept on `get()`
    and by the reaper when there is no traffic), and `ping()` on borrow rebuilds
    a connection the server dropped first.

    Thread-safe because FastMCP runs sync tools in a threadpool. `ping()` holds
    the lock, so borrows serialise — fine for a round-trip.
    """

    DEFAULT_MAX_SIZE: ClassVar[int] = 8
    DEFAULT_IDLE_TIMEOUT: ClassVar[float] = 600.0  # 10 minutes
    DEFAULT_REAPER_INTERVAL: ClassVar[float] = 60.0

    def __init__(
        self,
        engine: SourceEngine | str,
        conn_info: ConnectionInfo,
        *,
        max_sample_limit: int | None = None,
        default_database: str | None = None,
        max_size: int | None = None,
        idle_timeout: float | None = DEFAULT_IDLE_TIMEOUT,
        reaper_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = resolve_engine(engine)
        self._conn_info = conn_info
        self._max_sample_limit = max_sample_limit
        self._default_database = default_database or conn_info.database

        self._max_size = self.DEFAULT_MAX_SIZE if max_size is None else max_size
        if self._max_size < 1:
            raise ValueError(f"max_size must be at least 1, got {self._max_size}")
        if idle_timeout is not None and idle_timeout <= 0:
            raise ValueError(f"idle_timeout must be positive, got {idle_timeout}")

        self._idle_timeout = idle_timeout
        self._reaper_interval = (
            min(
                self.DEFAULT_REAPER_INTERVAL,
                idle_timeout or self.DEFAULT_REAPER_INTERVAL,
            )
            if reaper_interval is None
            else reaper_interval
        )
        # monotonic, not wall clock: a clock adjustment must not expire the pool
        self._clock = clock

        self._adapters: OrderedDict[str, _Entry] = OrderedDict()
        self._supports_multiple_databases: bool | None = None
        self._lock = threading.Lock()

        # Separate lock: the reaper's sweep takes `_lock`, so start/stop must not
        # hold it while joining the thread.
        self._reaper_lock = threading.Lock()
        self._reaper: threading.Thread | None = None
        self._stop_reaping = threading.Event()

    # ---- database resolution ----

    def _resolve_database(self, database: str | None) -> str | None:
        db = database or self._default_database
        if db is None:
            return db
        if self._supports_multiple_databases is None:
            self._supports_multiple_databases = load_adapter_class(
                self._engine
            ).SUPPORTS_MULTIPLE_DATABASES
        if self._supports_multiple_databases:
            return db
        # For a single-database source, `database` labels the whole source. Caching
        # one adapter per requested name would hand out adapters that all read the
        # same data while reporting different database names.
        if db != self._default_database:
            raise UnknownDatabaseError(
                f"{self._engine} exposes a single database "
                f"({self._default_database!r}), got {database!r}"
            )
        return db

    # ---- borrowing ----

    def get(self, database: str | None = None) -> SourceAdaptor:
        db = self._resolve_database(database)
        key = db or ""  # sqlite/datalake have no database to select
        discarded: list[SourceAdaptor] = []

        with self._lock:
            discarded.extend(self._take_expired())

            entry = self._adapters.get(key)
            if entry is not None and not _is_alive(entry.adapter):
                log.warning(
                    f"{self._engine} adapter for {key!r} failed ping, rebuilding"
                )
                del self._adapters[key]
                discarded.append(entry.adapter)
                entry = None

            if entry is None:
                # Under the lock, or two threads racing on one key leak the loser.
                entry = _Entry(
                    create_adapter(
                        self._engine,
                        self._conn_info,
                        database=db,
                        max_sample_limit=self._max_sample_limit,
                    )
                )
                self._adapters[key] = entry
                log.info(f"opened {self._engine} adapter for {key or '<default>'}")
                discarded.extend(self._take_over_capacity())

            entry.last_used = self._clock()
            self._adapters.move_to_end(key)
            adapter = entry.adapter

        # Closing can block, so it happens outside the lock.
        for stale in discarded:
            _close_quietly(stale)
        return adapter

    def list_databases(self) -> list[str]:
        return self.get().list_databases()

    # ---- eviction ----

    def _take_expired(self) -> list[SourceAdaptor]:
        """Entries idle longer than `idle_timeout`. Ordered by recency, so the
        expired ones are a prefix and the scan stops at the first live one."""
        if self._idle_timeout is None:
            return []
        deadline = self._clock() - self._idle_timeout
        expired: list[SourceAdaptor] = []
        for key, entry in list(self._adapters.items()):
            if entry.last_used > deadline:
                break
            del self._adapters[key]
            log.info(
                f"closing idle {self._engine} adapter for {key or '<default>'} "
                f"(unused for {self._idle_timeout}s)"
            )
            expired.append(entry.adapter)
        return expired

    def _take_over_capacity(self) -> list[SourceAdaptor]:
        evicted: list[SourceAdaptor] = []
        while len(self._adapters) > self._max_size:
            key, entry = self._adapters.popitem(last=False)
            log.warning(
                f"pool at capacity (max_size={self._max_size}), closing least "
                f"recently used {self._engine} adapter for {key or '<default>'}"
            )
            evicted.append(entry.adapter)
        return evicted

    def reap(self) -> int:
        """Close everything idle right now; returns how many were closed."""
        with self._lock:
            expired = self._take_expired()
        for stale in expired:
            _close_quietly(stale)
        return len(expired)

    # ---- reaper thread ----

    def start_reaper(self) -> None:
        """
        Close idle adapters in the background — `get()` only sweeps when something
        calls in. Not automatic: a pool should not spawn a thread by existing.
        """
        if self._idle_timeout is None:
            return
        with self._reaper_lock:
            if self._reaper is not None:
                return
            self._stop_reaping.clear()
            self._reaper = threading.Thread(
                target=self._reap_loop,
                name=f"adapter-pool-reaper[{self._engine}]",
                daemon=True,
            )
            self._reaper.start()
            log.info(
                f"reaper started for {self._engine}: closing adapters idle for "
                f"{self._idle_timeout}s, checked every {self._reaper_interval}s"
            )

    def stop_reaper(self, timeout: float = 5.0) -> None:
        with self._reaper_lock:
            thread, self._reaper = self._reaper, None
        if thread is None:
            return
        self._stop_reaping.set()
        thread.join(timeout)
        log.info(f"reaper stopped for {self._engine}")

    def _reap_loop(self) -> None:
        # Event.wait, not sleep, so stop_reaper() need not wait out the interval.
        while not self._stop_reaping.wait(self._reaper_interval):
            self._reap_quietly()

    @log.catch(reraise=False)
    def _reap_quietly(self) -> None:
        """A failed sweep must not kill the thread; `reap()` keeps raising for
        callers who ask for it directly."""
        self.reap()

    @property
    def reaper_running(self) -> bool:
        thread = self._reaper
        return thread is not None and thread.is_alive()

    # ---- teardown ----

    def close(self) -> None:
        self.stop_reaper()
        with self._lock:
            adapters = [entry.adapter for entry in self._adapters.values()]
            self._adapters.clear()
        for adapter in adapters:
            _close_quietly(adapter)

    def __enter__(self) -> AdapterPool:
        self.start_reaper()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _is_alive(adapter: SourceAdaptor) -> bool:
    """A ping that raises means dead, not an error for the caller."""
    try:
        return bool(adapter.ping())
    except Exception:  # noqa: BLE001 - any failure means unusable
        return False


@log.catch(reraise=False)
def _close_quietly(adapter: SourceAdaptor) -> None:
    """One failing close must not leave the rest open — swallowing is the point,
    which is why `reraise=False` belongs here and nowhere it can hide a result."""
    adapter.close()
