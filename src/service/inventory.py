from __future__ import annotations

import threading
import uuid
from collections.abc import Sequence
from typing import Any, ClassVar

from pydantic import BaseModel

from src.core.contracts import ProfileMode, ProfileResult
from src.core.log import log
from src.service.pool import AdapterProvider
from src.service.staging import StagingStore, schema_hash


class ScanAlreadyRunningError(Exception):
    """A scan of this database is already in flight."""


class UnknownJobError(Exception):
    """No scan was ever started under this job id."""


class ScanStatus(BaseModel):
    job_id: str
    database: str | None = None
    state: str  # running | done | failed | cancelled
    containers_done: int = 0
    containers_failed: int = 0
    containers_skipped: int = 0
    cursor: str | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class InventoryService:
    """
    Runs a catalog scan in the background, writing to the staging store: a large
    source outlasts any tool call, so `start` returns a job id to poll.
    """

    DEFAULT_PAGE_SIZE: ClassVar[int] = 100

    # Above this many distinct values, the twenty commonest are noise rather
    # than a description of the column, so the round trip is skipped. Only when
    # the modes were chosen per column; a caller who names `top_values` gets it.
    TOP_VALUES_MAX_DISTINCT: ClassVar[int] = 200

    def __init__(
        self,
        provider: AdapterProvider,
        store: StagingStore,
        *,
        page_size: int | None = None,
        default_profile_modes: Sequence[ProfileMode] | None = None,
    ) -> None:
        self._provider = provider
        self._store = store
        self._page_size = page_size or self.DEFAULT_PAGE_SIZE
        # None and () differ: nothing configured means the adapter picks per
        # column, an empty sequence means gather nothing.
        self._default_profile_modes = (
            None if default_profile_modes is None else tuple(default_profile_modes)
        )
        self._lock = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._running_databases: set[str | None] = set()

    @property
    def store(self) -> StagingStore:
        """The staging store this service writes to, for readers."""
        return self._store

    # ---- control ----

    def start(
        self,
        database: str | None = None,
        *,
        profile_modes: Sequence[ProfileMode] | None = None,
        force: bool = False,
        resume: bool = True,
    ) -> str:
        """
        Begin a scan and return its job id.

        `resume` continues an unfinished run from its cursor; `force` rescans
        containers whose schema has not changed.

        `profile_modes` falls back to the server's default, and if that is unset
        too each column gets the statistics its type warrants. An empty sequence
        is respected as "gather no statistics".
        """
        modes = (
            self._default_profile_modes
            if profile_modes is None
            else tuple(profile_modes)
        )
        with self._lock:
            if database in self._running_databases:
                raise ScanAlreadyRunningError(
                    f"a scan of {database!r} is already running"
                )
            job_id = uuid.uuid4().hex
            cursor = None
            if resume:
                previous = self._store.resumable_scan(database)
                if previous and previous["cursor"]:
                    cursor = previous["cursor"]
                    log.info(
                        f"resuming inventory of {database!r} after {cursor!r} "
                        f"({previous['containers_done']} containers already done)"
                    )

            self._store.create_scan(job_id, database)
            cancel = threading.Event()
            self._cancels[job_id] = cancel
            self._running_databases.add(database)
            worker = threading.Thread(
                target=self._run,
                args=(job_id, database, cursor, modes, force),
                name=f"inventory-scan[{database or 'default'}]",
                daemon=True,
            )
            self._workers[job_id] = worker

        worker.start()
        log.info(f"inventory scan {job_id} started for {database or '<default>'}")
        return job_id

    def status(self, job_id: str) -> ScanStatus:
        row = self._store.get_scan(job_id)
        if row is None:
            raise UnknownJobError(job_id)
        return ScanStatus(
            **{k: v for k, v in row.items() if k in ScanStatus.model_fields}
        )

    def cancel(self, job_id: str) -> bool:
        """Stop after the container in flight; progress is kept."""
        cancel = self._cancels.get(job_id)
        if cancel is None:
            return False
        cancel.set()
        log.info(f"inventory scan {job_id} cancellation requested")
        return True

    def wait(self, job_id: str, timeout: float | None = None) -> ScanStatus:
        """Join the worker — for tests and shutdown, not for tool calls."""
        worker = self._workers.get(job_id)
        if worker is not None:
            worker.join(timeout)
        return self.status(job_id)

    def close(self, timeout: float = 30.0) -> None:
        for job_id in list(self._cancels):
            self.cancel(job_id)
        for worker in list(self._workers.values()):
            worker.join(timeout)

    # ---- the scan ----

    def _run(
        self,
        job_id: str,
        database: str | None,
        cursor: str | None,
        profile_modes: tuple[ProfileMode, ...] | None,
        force: bool,
    ) -> None:
        cancel = self._cancels[job_id]
        done = failed = skipped = 0
        try:
            adapter = self._provider.get(database)
            while True:
                page = adapter.list_containers(
                    database=database, limit=self._page_size, cursor=cursor
                )
                for info in page.containers:
                    if cancel.is_set():
                        self._store.finish_scan(job_id, "cancelled")
                        log.info(f"inventory scan {job_id} cancelled after {done}")
                        return

                    outcome = self._scan_container(
                        adapter, info, profile_modes=profile_modes, force=force
                    )
                    if outcome == "failed":
                        failed += 1
                    elif outcome == "skipped":
                        skipped += 1
                    else:
                        done += 1

                    # Per container: this is what makes resume work.
                    self._store.advance_scan(
                        job_id,
                        cursor=info.container_name,
                        done=done,
                        failed=failed,
                        skipped=skipped,
                    )

                if page.next_cursor is None:
                    break
                cursor = page.next_cursor

            self._store.finish_scan(job_id, "done")
            log.info(
                f"inventory scan {job_id} finished: {done} scanned, "
                f"{skipped} unchanged, {failed} failed"
            )
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed silently
            log.warning(f"inventory scan {job_id} failed: {type(exc).__name__}: {exc}")
            self._store.finish_scan(
                job_id, "failed", error=f"{type(exc).__name__}: {exc}"
            )
        finally:
            with self._lock:
                self._running_databases.discard(database)
                self._cancels.pop(job_id, None)

    def _scan_container(
        self,
        adapter: Any,
        info: Any,
        *,
        profile_modes: tuple[ProfileMode, ...] | None,
        force: bool,
    ) -> str:
        """
        Inventory one container: "done", "skipped" or "failed".

        One unreadable table is recorded and stepped over, not fatal.
        """
        try:
            columns = adapter.get_schema(info.container_name)
        except Exception as exc:  # noqa: BLE001 - per-container isolation
            log.warning(f"cannot read {info.container_name!r}: {exc}")
            self._store.upsert_container(
                info, hash_="", error=f"{type(exc).__name__}: {exc}"
            )
            return "failed"

        current = schema_hash(columns, native_description=info.native_description)
        if not force:
            stored = self._store.stored_schema_hash(
                info.database, info.schema_name, info.container_name
            )
            if stored == current:
                return "skipped"

        self._store.upsert_container(info, hash_=current)
        self._store.replace_columns(
            info.database, info.schema_name, info.container_name, columns
        )

        for column in columns:
            self._profile_column(adapter, info, column, profile_modes)
        return "done"

    def _profile_column(
        self,
        adapter: Any,
        info: Any,
        column: Any,
        profile_modes: tuple[ProfileMode, ...] | None,
    ) -> None:
        """Gather the statistics for one column, in order.

        Chosen per column when the caller named none, and `top_values` is then
        dropped once `distinct_count` says the column has too many to be worth
        listing — which is why the modes run in sequence rather than as a set."""
        chosen = (
            adapter.default_profile_modes(column)
            if profile_modes is None
            else profile_modes
        )
        distinct: int | None = None
        for mode in chosen:
            if (
                profile_modes is None
                and mode == ProfileMode.TOP_VALUES
                and distinct is not None
                and distinct > self.TOP_VALUES_MAX_DISTINCT
            ):
                continue
            result = self._profile_one(adapter, info, column.name, mode)
            if result is not None and mode == ProfileMode.DISTINCT_COUNT:
                distinct = result.distinct_count

    def _profile_one(
        self, adapter: Any, info: Any, column: str, mode: ProfileMode
    ) -> ProfileResult | None:
        try:
            result = adapter.profile_column(info.container_name, column, mode)
        except Exception as exc:  # noqa: BLE001 - one bad column is not fatal
            log.warning(
                f"cannot profile {info.container_name}.{column} ({mode}): {exc}"
            )
            return None
        self._store.record_profile(
            info.database, info.schema_name, info.container_name, column, mode, result
        )
        return result
