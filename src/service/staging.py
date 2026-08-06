from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.contracts import ColumnInfo, ContainerInfo, ProfileMode, ProfileResult
from src.core.log import log

# NULL in a primary key lets duplicates in (SQLite treats NULLs as distinct).
NO_SCHEMA = ""

MARKER = "database-mcp-connector/staging"
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS staging_meta (
    marker     TEXT PRIMARY KEY,
    version    INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    job_id           TEXT PRIMARY KEY,
    database         TEXT,
    state            TEXT NOT NULL,
    cursor           TEXT,
    containers_done  INTEGER NOT NULL DEFAULT 0,
    containers_failed INTEGER NOT NULL DEFAULT 0,
    containers_skipped INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT
);

CREATE TABLE IF NOT EXISTS containers (
    database        TEXT NOT NULL,
    schema_name     TEXT NOT NULL,
    container_name  TEXT NOT NULL,
    container_type  TEXT NOT NULL,
    estimated_count INTEGER,
    schema_hash     TEXT NOT NULL,
    error           TEXT,
    scanned_at      TEXT NOT NULL,
    PRIMARY KEY (database, schema_name, container_name)
);

CREATE TABLE IF NOT EXISTS columns (
    database       TEXT NOT NULL,
    schema_name    TEXT NOT NULL,
    container_name TEXT NOT NULL,
    column_name    TEXT NOT NULL,
    ordinal        INTEGER NOT NULL,
    native_type    TEXT NOT NULL,
    nullable       INTEGER NOT NULL,
    is_pk          INTEGER NOT NULL,
    is_fk          INTEGER NOT NULL,
    profile        TEXT,
    scanned_at     TEXT NOT NULL,
    PRIMARY KEY (database, schema_name, container_name, column_name)
);

CREATE INDEX IF NOT EXISTS columns_by_container
    ON columns (database, schema_name, container_name);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _reject_source_overlap(staging: Path, source: str | Path | None) -> None:
    """The staging file must not be the database being inventoried."""
    if source is None:
        return
    source_path = Path(source)
    if not source_path.name:
        return
    if staging.expanduser().resolve() == source_path.expanduser().resolve():
        raise StagingPathConflictError(
            f"staging path {staging} is the source database; point STAGING_DB "
            "somewhere else"
        )


def schema_hash(columns: Sequence[ColumnInfo]) -> str:
    """Fingerprint of a container's shape, for deciding what a rescan can skip."""
    payload = [
        [c.name, c.ordinal, c.native_type, c.nullable, c.is_pk, c.is_fk]
        for c in columns
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StagingError(Exception):
    """The staging path cannot be used."""


class NotAStagingStoreError(StagingError):
    """The file exists but was not created by this store."""


class StagingPathConflictError(StagingError):
    """The staging path is the source database being inventoried."""


class StagingStore:
    """
    Where an inventory run accumulates, so scanning is decoupled from reading it:
    a run that dies resumes, unchanged containers are skipped, and the agent can
    ask for a summary instead of every row.

    One connection guarded by a lock, since the scan worker has its own thread.
    """

    def __init__(
        self, path: str | Path, *, source_path: str | Path | None = None
    ) -> None:
        self.path = Path(path)
        _reject_source_overlap(self.path, source_path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._claim_or_reject()
            # WAL: a reader is not blocked by the scan writing.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO staging_meta (marker, version, created_at) "
                "VALUES (?,?,?)",
                (MARKER, SCHEMA_VERSION, _now()),
            )
            self._conn.commit()
        log.info(f"staging store ready at {self.path}")

    def _claim_or_reject(self) -> None:
        """Refuse a file holding someone else's data: anything already populated
        must carry our marker."""
        try:
            tables = {
                row[0]
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        except sqlite3.DatabaseError as exc:
            raise NotAStagingStoreError(
                f"{self.path} is not a SQLite database"
            ) from exc

        if not tables:
            return
        if "staging_meta" not in tables:
            raise NotAStagingStoreError(
                f"{self.path} holds tables {sorted(tables)[:5]} but no staging marker; "
                "refusing to write to what looks like a source database"
            )
        row = self._conn.execute(
            "SELECT version FROM staging_meta WHERE marker=?", (MARKER,)
        ).fetchone()
        if row is None:
            raise NotAStagingStoreError(f"{self.path} carries a different marker")
        if row["version"] > SCHEMA_VERSION:
            raise NotAStagingStoreError(
                f"{self.path} was written by a newer version ({row['version']} > "
                f"{SCHEMA_VERSION})"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> StagingStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- helpers ----

    def _write(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _rows(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _row(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # ---- scans ----

    def create_scan(self, job_id: str, database: str | None) -> None:
        self._write(
            "INSERT INTO scans (job_id, database, state, started_at) VALUES (?,?,?,?)",
            (job_id, database, "running", _now()),
        )

    def advance_scan(
        self,
        job_id: str,
        *,
        cursor: str,
        done: int,
        failed: int,
        skipped: int,
    ) -> None:
        self._write(
            "UPDATE scans SET cursor=?, containers_done=?, containers_failed=?, "
            "containers_skipped=? WHERE job_id=?",
            (cursor, done, failed, skipped, job_id),
        )

    def finish_scan(self, job_id: str, state: str, error: str | None = None) -> None:
        self._write(
            "UPDATE scans SET state=?, error=?, finished_at=? WHERE job_id=?",
            (state, error, _now(), job_id),
        )

    def get_scan(self, job_id: str) -> dict[str, Any] | None:
        row = self._row("SELECT * FROM scans WHERE job_id=?", (job_id,))
        return dict(row) if row else None

    def resumable_scan(self, database: str | None) -> dict[str, Any] | None:
        """The most recent run for this database that never finished."""
        row = self._row(
            "SELECT * FROM scans WHERE database IS ? AND state IN ('running','failed') "
            "ORDER BY started_at DESC LIMIT 1",
            (database,),
        )
        return dict(row) if row else None

    # ---- inventory ----

    def stored_schema_hash(
        self, database: str, schema: str | None, container: str
    ) -> str | None:
        row = self._row(
            "SELECT schema_hash FROM containers WHERE database=? AND schema_name=? "
            "AND container_name=? AND error IS NULL",
            (database, schema or NO_SCHEMA, container),
        )
        return row["schema_hash"] if row else None

    def upsert_container(
        self, info: ContainerInfo, *, hash_: str, error: str | None = None
    ) -> None:
        self._write(
            """
            INSERT INTO containers (database, schema_name, container_name,
                container_type, estimated_count, schema_hash, error, scanned_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT (database, schema_name, container_name) DO UPDATE SET
                container_type=excluded.container_type,
                estimated_count=excluded.estimated_count,
                schema_hash=excluded.schema_hash,
                error=excluded.error,
                scanned_at=excluded.scanned_at
            """,
            (
                info.database,
                info.schema_name or NO_SCHEMA,
                info.container_name,
                str(info.container_type),
                info.estimated_count,
                hash_,
                error,
                _now(),
            ),
        )

    def replace_columns(
        self,
        database: str,
        schema: str | None,
        container: str,
        columns: Iterable[ColumnInfo],
    ) -> None:
        """Rewrite a container's columns: old rows go first so a dropped column
        disappears, in one transaction."""
        schema_key = schema or NO_SCHEMA
        scanned_at = _now()
        with self._lock:
            self._conn.execute(
                "DELETE FROM columns WHERE database=? AND schema_name=? "
                "AND container_name=?",
                (database, schema_key, container),
            )
            self._conn.executemany(
                """
                INSERT INTO columns (database, schema_name, container_name,
                    column_name, ordinal, native_type, nullable, is_pk, is_fk,
                    scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        database,
                        schema_key,
                        container,
                        column.name,
                        column.ordinal,
                        column.native_type,
                        int(column.nullable),
                        int(column.is_pk),
                        int(column.is_fk),
                        scanned_at,
                    )
                    for column in columns
                ],
            )
            self._conn.commit()

    def record_profile(
        self,
        database: str,
        schema: str | None,
        container: str,
        column: str,
        mode: ProfileMode,
        result: ProfileResult,
    ) -> None:
        """Merge one mode's result into the column's stored profile."""
        schema_key = schema or NO_SCHEMA
        with self._lock:
            row = self._conn.execute(
                "SELECT profile FROM columns WHERE database=? AND schema_name=? "
                "AND container_name=? AND column_name=?",
                (database, schema_key, container, column),
            ).fetchone()
            if row is None:
                return
            profile = json.loads(row["profile"]) if row["profile"] else {}
            profile[str(mode)] = result.model_dump(exclude_defaults=True)
            self._conn.execute(
                "UPDATE columns SET profile=? WHERE database=? AND schema_name=? "
                "AND container_name=? AND column_name=?",
                (
                    json.dumps(profile, ensure_ascii=False),
                    database,
                    schema_key,
                    container,
                    column,
                ),
            )
            self._conn.commit()

    # ---- reading ----

    def summary(self, database: str | None = None) -> dict[str, Any]:
        """Counts rather than rows, so looking at a 10k-table catalog costs a few
        hundred tokens."""
        where, params = ("WHERE database=?", (database,)) if database else ("", ())
        containers = self._row(
            f"SELECT COUNT(*) AS n, SUM(error IS NOT NULL) AS failed, "  # noqa: S608
            f"SUM(estimated_count) AS rows_total FROM containers {where}",
            params,
        )
        columns = self._row(
            f"SELECT COUNT(*) AS n, SUM(profile IS NOT NULL) AS profiled "  # noqa: S608
            f"FROM columns {where}",
            params,
        )
        assert containers is not None and columns is not None  # noqa: S101
        return {
            "database": database,
            "containers": containers["n"],
            "containers_failed": containers["failed"] or 0,
            "estimated_rows": containers["rows_total"],
            "columns": columns["n"],
            "columns_profiled": columns["profiled"] or 0,
        }

    def containers(
        self,
        database: str | None = None,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Keyset on the name, same as the live catalog."""
        clauses, params = [], []
        if database:
            clauses.append("database=?")
            params.append(database)
        if cursor:
            clauses.append("container_name>?")
            params.append(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return [
            dict(row)
            for row in self._rows(
                f"SELECT * FROM containers {where} "  # noqa: S608
                "ORDER BY container_name LIMIT ?",
                params,
            )
        ]

    def columns(
        self, database: str, container: str, schema: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM columns WHERE database=? AND schema_name=? "
            "AND container_name=? ORDER BY ordinal",
            (database, schema or NO_SCHEMA, container),
        )
        result = []
        for row in rows:
            item = dict(row)
            item["nullable"] = bool(item["nullable"])
            item["is_pk"] = bool(item["is_pk"])
            item["is_fk"] = bool(item["is_fk"])
            item["profile"] = json.loads(item["profile"]) if item["profile"] else None
            result.append(item)
        return result
