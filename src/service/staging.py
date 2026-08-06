from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from src.core.contracts import (
    ColumnInfo,
    ContainerInfo,
    ProfileMode,
    ProfileResult,
    Sensitivity,
)
from src.core.log import log

# NULL in a primary key lets duplicates in (SQLite treats NULLs as distinct).
NO_SCHEMA = ""

MARKER = "database-mcp-connector/staging"
SCHEMA_VERSION = 3

# Page sizes for the two reads that can otherwise return a whole catalog.
DEFAULT_COLUMN_PAGE = 100
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_CHANGE_LIMIT = 100

# How much of a description a search hit carries. Enough to judge whether this
# is the column you meant; `inventory_columns` has the whole thing. Without a
# bound, one long description makes a whole result set expensive, and a hit is
# supposed to be cheap by construction rather than by luck.
SEARCH_DESCRIPTION_CHARS = 120

# Every column but the profile, which is most of a wide table's weight.
_COLUMNS_WITHOUT_PROFILE = (
    "database, schema_name, container_name, column_name, ordinal, native_type, "
    "nullable, is_pk, is_fk, native_description, description, description_source, "
    "description_updated_at, references_container, references_column, "
    "sensitivity, sensitivity_source, scanned_at"
)

# Who wrote a description. The server decides this, never the caller.
SOURCE_NATIVE = "native"
SOURCE_AI = "ai"
SOURCE_HUMAN = "human"

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

-- Two kinds of description, deliberately in separate columns:
-- `native_description` is read from the source and overwritten by every scan,
-- `description` is written by an agent or a person and a scan never touches it.
CREATE TABLE IF NOT EXISTS containers (
    database        TEXT NOT NULL,
    schema_name     TEXT NOT NULL,
    container_name  TEXT NOT NULL,
    container_type  TEXT NOT NULL,
    estimated_count INTEGER,
    schema_hash     TEXT NOT NULL,
    native_description     TEXT,
    description            TEXT,
    description_source     TEXT,   -- native | ai | human
    description_updated_at TEXT,
    last_modified_at       TEXT,
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
    native_description     TEXT,
    description            TEXT,
    description_source     TEXT,
    description_updated_at TEXT,
    references_container   TEXT,
    references_column      TEXT,
    sensitivity            TEXT,   -- none | pii | secret
    sensitivity_source     TEXT,
    profile        TEXT,
    scanned_at     TEXT NOT NULL,
    PRIMARY KEY (database, schema_name, container_name, column_name)
);

CREATE INDEX IF NOT EXISTS columns_by_container
    ON columns (database, schema_name, container_name);

-- For searching by column name across the whole catalog.
CREATE INDEX IF NOT EXISTS columns_by_name ON columns (column_name);

-- Append-only: `schema_hash` keeps only the latest state, but "what changed
-- upstream this week" is what a data engineer asks first, and the hash needed
-- to answer it was already being computed and thrown away.
CREATE TABLE IF NOT EXISTS schema_changes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT NOT NULL,
    database       TEXT NOT NULL,
    schema_name    TEXT NOT NULL,
    container_name TEXT NOT NULL,
    change_type    TEXT NOT NULL,  -- container_added | container_removed | schema_changed
    old_hash       TEXT,
    new_hash       TEXT,
    detail         TEXT,           -- JSON: the columns added, removed or retyped
    detected_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS changes_by_time ON schema_changes (detected_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _blank_to_none(text: str) -> str | None:
    """A description of whitespace is a description no one wants back."""
    return text.strip() or None


def _escape_like(text: str) -> str:
    """
    Neutralise LIKE's own wildcards.

    Searching for `user_id` must not match `userXid`: `_` is a single-character
    wildcard, and a caller looking for a column name is the commonest way to
    meet one.
    """
    for char in ("\\", "%", "_"):
        text = text.replace(char, f"\\{char}")
    return text


def _summarise(text: str | None) -> str | None:
    """A description cut to what a search result can afford to carry."""
    if text is None or len(text) <= SEARCH_DESCRIPTION_CHARS:
        return text
    return text[:SEARCH_DESCRIPTION_CHARS].rstrip() + "…"


def _ordinal_cursor(cursor: str) -> int:
    """
    A cursor is the last ordinal returned, and nothing else is one.

    Starting from the top instead would answer "the page after X" with page one
    — the same rows again, with a cursor that leads back to them, and no way
    for the caller to tell it is going in circles. A cursor is machine-made, so
    one that will not parse means something is wrong upstream and saying so is
    the only useful answer.
    """
    try:
        return int(cursor)
    except ValueError as exc:
        raise InvalidCursorError(
            f"{cursor!r} is not a cursor from a previous page; pass back the "
            "`next_cursor` you were given, or nothing to start from the first"
        ) from exc


def _reject_source_overlap(staging: Path, source: str | Path | None) -> None:
    """The staging file must not be the database being inventoried."""
    if source is None:
        return
    source_path = Path(source)
    if not source_path.name:
        return
    if staging.expanduser().resolve() == source_path.expanduser().resolve():
        raise StagingPathConflictError(
            f"staging path {staging} is the source database; point "
            "MCP_STAGING_DB somewhere else"
        )


def schema_hash(
    columns: Sequence[ColumnInfo], *, native_description: str | None = None
) -> str:
    """
    Fingerprint of a container's shape, for deciding what a rescan can skip.

    Everything a scan overwrites goes in, the source's own comments included:
    a comment edited upstream is exactly the sort of change the inventory is for,
    and a hash blind to it would skip the container. What an agent or a person
    writes stays out — a scan never overwrites that, and a hash sensitive to it
    would make annotating a container trigger its own rescan.
    """
    payload = {
        "container": native_description,
        "columns": [
            [
                c.name,
                c.ordinal,
                c.native_type,
                c.nullable,
                c.is_pk,
                c.is_fk,
                c.native_description,
                c.references_container,
                c.references_column,
            ]
            for c in columns
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StoredContainer(BaseModel):
    """A container as the last scan recorded it, plus anything written about it."""

    database: str
    schema_name: str
    container_name: str
    container_type: str
    estimated_count: int | None = None
    schema_hash: str
    native_description: str | None = None
    description: str | None = None
    description_source: str | None = None
    description_updated_at: str | None = None
    last_modified_at: str | None = None
    error: str | None = None
    scanned_at: str


class StoredColumn(BaseModel):
    """A column as the last scan recorded it, with any profile gathered."""

    database: str
    schema_name: str
    container_name: str
    column_name: str
    ordinal: int
    native_type: str
    nullable: bool
    is_pk: bool
    is_fk: bool
    native_description: str | None = None
    description: str | None = None
    description_source: str | None = None
    description_updated_at: str | None = None
    references_container: str | None = None
    references_column: str | None = None
    sensitivity: Sensitivity | None = None
    sensitivity_source: str | None = None
    profile: dict[str, Any] | None = None
    scanned_at: str


class ColumnAnnotation(BaseModel):
    """One column's written description. A field left None is left alone."""

    column: str
    description: str | None = None
    sensitivity: Sensitivity | None = None


class AnnotateResult(BaseModel):
    """`unknown_columns` is reported rather than ignored: a name that is not
    there usually means the agent is annotating the wrong container."""

    containers_updated: int = 0
    columns_updated: int = 0
    unknown_columns: list[str] = []


class StoredContainerPage(BaseModel):
    containers: list[StoredContainer]
    next_cursor: str | None = None


class StoredColumnPage(BaseModel):
    columns: list[StoredColumn]
    next_cursor: str | None = None


class SchemaChange(BaseModel):
    """One thing that moved upstream between two scans."""

    id: int
    job_id: str
    database: str
    schema_name: str
    container_name: str
    change_type: str
    old_hash: str | None = None
    new_hash: str | None = None
    # what actually differed: added / removed / retyped column names
    detail: dict[str, Any] | None = None
    detected_at: str


class Relationship(BaseModel):
    """One foreign key, as an edge an agent can draw."""

    database: str
    schema_name: str
    from_container: str
    from_column: str
    to_container: str
    to_column: str | None = None


class SearchHit(BaseModel):
    """
    One match, kept deliberately narrow.

    No profile: this is what an agent reads to decide where to look, and a
    hundred hits carrying their statistics is the context problem the search
    exists to avoid. `inventory_columns` is the next call, on one container.
    """

    database: str
    schema_name: str
    container_name: str
    # None for a container hit, the column's name for a column hit
    column_name: str | None = None
    native_type: str | None = None
    description: str | None = None
    # whether the keyword was found in the name or in a description
    match_in: str


class InventorySummary(BaseModel):
    database: str | None = None
    containers: int = 0
    containers_failed: int = 0
    estimated_rows: int | None = None
    columns: int = 0
    columns_profiled: int = 0


class StagingError(Exception):
    """The staging path cannot be used."""


class NotAStagingStoreError(StagingError):
    """The file exists but was not created by this store."""


class StagingPathConflictError(StagingError):
    """The staging path is the source database being inventoried."""


class OutdatedStagingSchemaError(StagingError):
    """The file was written by an older layout of this store."""


class UnknownStagedContainerError(Exception):
    """Nothing has been inventoried under that name."""


class InvalidCursorError(Exception):
    """The cursor did not come from a page this store handed out."""


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
        if row["version"] < SCHEMA_VERSION:
            # No migration chain on purpose: an inventory is derived data that a
            # rescan reproduces, so the honest instruction is cheaper to maintain
            # — and to trust — than a chain of ALTERs.
            raise OutdatedStagingSchemaError(
                f"{self.path} uses staging schema v{row['version']}, this build "
                f"writes v{SCHEMA_VERSION}. An inventory is rebuilt by rescanning: "
                f"delete {self.path} and run inventory_start again. Anything "
                "written with inventory_annotate is in that file and will be lost."
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
        """What the scan saw. `description` and its provenance are absent from
        the update list on purpose — they belong to `annotate`."""
        self._write(
            """
            INSERT INTO containers (database, schema_name, container_name,
                container_type, estimated_count, schema_hash, native_description,
                last_modified_at, error, scanned_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (database, schema_name, container_name) DO UPDATE SET
                container_type=excluded.container_type,
                estimated_count=excluded.estimated_count,
                schema_hash=excluded.schema_hash,
                native_description=excluded.native_description,
                last_modified_at=excluded.last_modified_at,
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
                info.native_description,
                info.last_modified_at,
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
        """
        Bring a container's columns up to date with what the scan saw, in one
        transaction: upsert what is there, then delete what is not.

        Upsert rather than delete-then-insert because a column row holds two
        things a scan did not produce and must not destroy — the written
        description and the profile. Rewriting the table wholesale would erase
        every description on the next rescan, which is the whole point of
        keeping them here.
        """
        schema_key = schema or NO_SCHEMA
        scanned_at = _now()
        current = list(columns)
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO columns (database, schema_name, container_name,
                    column_name, ordinal, native_type, nullable, is_pk, is_fk,
                    native_description, references_container, references_column,
                    scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (database, schema_name, container_name, column_name)
                DO UPDATE SET
                    ordinal=excluded.ordinal,
                    native_type=excluded.native_type,
                    nullable=excluded.nullable,
                    is_pk=excluded.is_pk,
                    is_fk=excluded.is_fk,
                    native_description=excluded.native_description,
                    references_container=excluded.references_container,
                    references_column=excluded.references_column,
                    scanned_at=excluded.scanned_at
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
                        column.native_description,
                        column.references_container,
                        column.references_column,
                        scanned_at,
                    )
                    for column in current
                ],
            )
            # A column that really did disappear still has to go. With nothing
            # left to keep, the unrestricted DELETE is the right statement: the
            # container has no columns any more.
            names = [column.name for column in current]
            placeholders = ",".join("?" * len(names))
            self._conn.execute(
                "DELETE FROM columns WHERE database=? AND schema_name=? "
                "AND container_name=?"
                + (f" AND column_name NOT IN ({placeholders})" if names else ""),
                (database, schema_key, container, *names),
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

    # ---- sensitivity ----

    def record_sensitivity(
        self,
        database: str,
        schema: str | None,
        container: str,
        levels: dict[str, Sensitivity],
        *,
        source: str,
    ) -> int:
        """
        What a scan decided about a container's columns.

        Only over its own earlier verdicts: a level set by `annotate` was put
        there by an agent or a person looking at the thing, and a pattern match
        does not get to overrule that. Nothing of the values that led to the
        decision is written — the verdict is the whole record.
        """
        schema_key = schema or NO_SCHEMA
        written = 0
        with self._lock:
            for column, level in levels.items():
                cursor = self._conn.execute(
                    "UPDATE columns SET sensitivity=?, sensitivity_source=? "
                    "WHERE database=? AND schema_name=? AND container_name=? "
                    "AND column_name=? AND (sensitivity_source IS NULL "
                    "OR sensitivity_source=?)",
                    (
                        str(level),
                        source,
                        database,
                        schema_key,
                        container,
                        column,
                        source,
                    ),
                )
                written += cursor.rowcount
            self._conn.commit()
        return written

    def sensitivity_of(
        self, database: str, container: str, schema: str | None = None
    ) -> dict[str, Sensitivity]:
        """A container's recorded levels, for masking a sample of it."""
        rows = self._rows(
            "SELECT column_name, sensitivity FROM columns WHERE database=? "
            "AND schema_name=? AND container_name=? AND sensitivity IS NOT NULL",
            (database, schema or NO_SCHEMA, container),
        )
        return {
            row["column_name"]: Sensitivity(row["sensitivity"])
            for row in rows
            if row["sensitivity"] in set(Sensitivity)
        }

    # ---- change history ----

    def record_change(
        self,
        job_id: str,
        database: str,
        schema: str | None,
        container: str,
        *,
        change_type: str,
        old_hash: str | None = None,
        new_hash: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append one change. Nothing here is ever updated or deleted: the
        value is in the sequence, and a corrected history is not one."""
        self._write(
            """
            INSERT INTO schema_changes (job_id, database, schema_name,
                container_name, change_type, old_hash, new_hash, detail,
                detected_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                database,
                schema or NO_SCHEMA,
                container,
                change_type,
                old_hash,
                new_hash,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                _now(),
            ),
        )

    def changes(
        self,
        database: str | None = None,
        *,
        since: str | None = None,
        limit: int = DEFAULT_CHANGE_LIMIT,
    ) -> list[SchemaChange]:
        """What moved, newest first. `since` is an ISO timestamp."""
        clauses, params = [], []
        if database:
            clauses.append("database=?")
            params.append(database)
        if since:
            clauses.append("detected_at>=?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._rows(
            f"SELECT * FROM schema_changes {where} "  # noqa: S608
            "ORDER BY detected_at DESC, id DESC LIMIT ?",
            params,
        )
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"]) if item["detail"] else None
            result.append(SchemaChange(**item))
        return result

    def container_keys(self, database: str | None = None) -> set[tuple[str, str, str]]:
        """
        Every container recorded, as (database, schema, name).

        For spotting the ones a scan no longer finds. Keys only — the point is
        not to read the rows. The schema is part of it because two schemas can
        hold the same name, and dropping the wrong one is not recoverable.
        """
        where, params = ("WHERE database=?", (database,)) if database else ("", ())
        return {
            (row["database"], row["schema_name"], row["container_name"])
            for row in self._rows(
                "SELECT database, schema_name, container_name "  # noqa: S608
                f"FROM containers {where}",
                params,
            )
        }

    def forget_container(
        self, database: str, schema: str | None, container: str
    ) -> None:
        """Drop a container and its columns, for one that is gone upstream."""
        schema_key = schema or NO_SCHEMA
        with self._lock:
            self._conn.execute(
                "DELETE FROM columns WHERE database=? AND schema_name=? "
                "AND container_name=?",
                (database, schema_key, container),
            )
            self._conn.execute(
                "DELETE FROM containers WHERE database=? AND schema_name=? "
                "AND container_name=?",
                (database, schema_key, container),
            )
            self._conn.commit()

    # ---- relationships ----

    def relationships(self, database: str | None = None) -> list[Relationship]:
        """
        Every foreign key as an edge.

        "How do these two tables join" is the first question anyone asks of a
        database they did not build, and `is_fk` on its own cannot answer it.
        """
        where, params = ("AND database=?", (database,)) if database else ("", ())
        rows = self._rows(
            "SELECT database, schema_name, container_name, column_name, "  # noqa: S608
            f"references_container, references_column FROM columns "
            f"WHERE references_container IS NOT NULL {where} "
            "ORDER BY container_name, ordinal",
            params,
        )
        return [
            Relationship(
                database=row["database"],
                schema_name=row["schema_name"],
                from_container=row["container_name"],
                from_column=row["column_name"],
                to_container=row["references_container"],
                to_column=row["references_column"],
            )
            for row in rows
        ]

    # ---- annotations ----

    def annotate(
        self,
        database: str,
        container: str,
        schema: str | None = None,
        *,
        container_description: str | None = None,
        columns: Sequence[ColumnAnnotation] = (),
        source: str = SOURCE_AI,
    ) -> AnnotateResult:
        """
        Write descriptions onto what was inventoried. Never onto the source: this
        store is the only thing a description ever reaches.

        A field left None is left as it was; a blank string clears it. `source`
        is decided by the caller's identity, not by the annotation.

        One transaction on purpose: an agent describing a table and its columns
        is making one statement about it, so a crash must not leave the table
        described and its columns not.
        """
        schema_key = schema or NO_SCHEMA
        key = (database, schema_key, container)
        written_at = _now()
        result = AnnotateResult()

        with self._lock:
            if (
                self._conn.execute(
                    "SELECT 1 FROM containers WHERE database=? AND schema_name=? "
                    "AND container_name=?",
                    key,
                ).fetchone()
                is None
            ):
                raise UnknownStagedContainerError(
                    f"{container!r} is not in the inventory of {database!r}; "
                    "run inventory_start first"
                )

            if container_description is not None:
                self._conn.execute(
                    "UPDATE containers SET description=?, description_source=?, "
                    "description_updated_at=? WHERE database=? AND schema_name=? "
                    "AND container_name=?",
                    (_blank_to_none(container_description), source, written_at, *key),
                )
                result.containers_updated = 1

            known = {
                row["column_name"]
                for row in self._conn.execute(
                    "SELECT column_name FROM columns WHERE database=? AND "
                    "schema_name=? AND container_name=?",
                    key,
                )
            }
            for annotation in columns:
                if annotation.column not in known:
                    result.unknown_columns.append(annotation.column)
                    continue
                assignments, params = [], []
                if annotation.description is not None:
                    assignments += [
                        "description=?",
                        "description_source=?",
                        "description_updated_at=?",
                    ]
                    params += [
                        _blank_to_none(annotation.description),
                        source,
                        written_at,
                    ]
                if annotation.sensitivity is not None:
                    assignments += ["sensitivity=?", "sensitivity_source=?"]
                    params += [str(annotation.sensitivity), source]
                if not assignments:
                    continue
                self._conn.execute(
                    f"UPDATE columns SET {', '.join(assignments)} "  # noqa: S608
                    "WHERE database=? AND schema_name=? AND container_name=? "
                    "AND column_name=?",
                    (*params, *key, annotation.column),
                )
                result.columns_updated += 1

            self._conn.commit()

        log.info(
            f"annotated {database}.{container} by {source}: "
            f"{result.columns_updated} columns"
        )
        return result

    # ---- reading ----

    def summary(self, database: str | None = None) -> InventorySummary:
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
        return InventorySummary(
            database=database,
            containers=containers["n"],
            containers_failed=containers["failed"] or 0,
            estimated_rows=containers["rows_total"],
            columns=columns["n"],
            columns_profiled=columns["profiled"] or 0,
        )

    def containers(
        self,
        database: str | None = None,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> StoredContainerPage:
        """Keyset on the name, same as the live catalog."""
        clauses, params = [], []
        if database:
            clauses.append("database=?")
            params.append(database)
        if cursor:
            clauses.append("container_name>?")
            params.append(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit + 1)  # one extra row tells us whether more remain
        rows = self._rows(
            f"SELECT * FROM containers {where} "  # noqa: S608
            "ORDER BY container_name LIMIT ?",
            params,
        )
        page = [StoredContainer(**dict(row)) for row in rows[:limit]]
        return StoredContainerPage(
            containers=page,
            next_cursor=page[-1].container_name if len(rows) > limit else None,
        )

    def columns(
        self,
        database: str,
        container: str,
        schema: str | None = None,
        *,
        limit: int = DEFAULT_COLUMN_PAGE,
        cursor: str | None = None,
        include_profile: bool = True,
    ) -> StoredColumnPage:
        """
        One page of a container's columns, in ordinal order.

        Paged because a wide table is the other way an inventory floods a
        caller's context, and `include_profile=False` because on such a table
        the statistics are most of the weight — the escape hatch for reading
        the shape of a 300-column table without its every top-values list.
        """
        selected = "*" if include_profile else _COLUMNS_WITHOUT_PROFILE
        clauses = ["database=?", "schema_name=?", "container_name=?"]
        params: list[Any] = [database, schema or NO_SCHEMA, container]
        if cursor is not None:
            # keyset on the ordering column, as elsewhere; ordinals are unique
            # within one container
            clauses.append("ordinal>?")
            params.append(_ordinal_cursor(cursor))
        params.append(limit + 1)  # one extra row tells us whether more remain

        rows = self._rows(
            f"SELECT {selected} FROM columns WHERE {' AND '.join(clauses)} "  # noqa: S608
            "ORDER BY ordinal LIMIT ?",
            params,
        )
        page = []
        for row in rows[:limit]:
            item = dict(row)
            raw = item.get("profile")
            item["profile"] = json.loads(raw) if raw else None
            page.append(StoredColumn(**item))
        return StoredColumnPage(
            columns=page,
            next_cursor=str(page[-1].ordinal) if len(rows) > limit else None,
        )

    def search(
        self,
        keyword: str,
        database: str | None = None,
        *,
        kind: str = "all",
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[SearchHit]:
        """
        Containers and columns whose name or description mentions `keyword`.

        The entry point for anyone who cannot read the whole catalog: without
        it the only way in is paging through every container, which puts the
        catalog into the context window one page at a time.

        `LIKE` over the `columns_by_name` index rather than FTS5. A full scan of
        a few hundred thousand rows is tens of milliseconds in SQLite, against
        which FTS5 costs an index to keep in step with two writers and a
        dependency on how CPython was compiled. Worth revisiting when something
        is actually measured to be slow.
        """
        if not keyword.strip():
            return []
        pattern = f"%{_escape_like(keyword.strip())}%"
        hits: list[SearchHit] = []
        if kind in ("all", "container"):
            hits.extend(self._container_hits(pattern, database, limit))
        if kind in ("all", "column"):
            hits.extend(self._column_hits(pattern, database, limit))
        # The order decides what survives the limit below, so it is ranking and
        # not tidiness. A keyword in a name is what the caller meant more often
        # than the same word buried in prose; and among equals a container is
        # the broader answer — "orders" almost always means the table, not some
        # `orders_count` column, and there are far fewer of them to lose.
        hits.sort(
            key=lambda hit: (
                hit.match_in != "name",
                hit.column_name is not None,
                hit.container_name,
                hit.column_name or "",
            )
        )
        return hits[:limit]

    def _container_hits(
        self, pattern: str, database: str | None, limit: int
    ) -> list[SearchHit]:
        clauses = [
            "(container_name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' "
            "OR native_description LIKE ? ESCAPE '\\')"
        ]
        params: list[Any] = [pattern, pattern, pattern]
        if database:
            clauses.append("database=?")
            params.append(database)
        params.append(limit)
        rows = self._rows(
            "SELECT database, schema_name, container_name, description, "  # noqa: S608
            f"native_description, container_name LIKE ? ESCAPE '\\' AS by_name "
            f"FROM containers WHERE {' AND '.join(clauses)} "
            "ORDER BY container_name LIMIT ?",
            [pattern, *params],
        )
        return [
            SearchHit(
                database=row["database"],
                schema_name=row["schema_name"],
                container_name=row["container_name"],
                description=_summarise(row["description"] or row["native_description"]),
                match_in="name" if row["by_name"] else "description",
            )
            for row in rows
        ]

    def _column_hits(
        self, pattern: str, database: str | None, limit: int
    ) -> list[SearchHit]:
        clauses = [
            "(column_name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\' "
            "OR native_description LIKE ? ESCAPE '\\')"
        ]
        params: list[Any] = [pattern, pattern, pattern]
        if database:
            clauses.append("database=?")
            params.append(database)
        params.append(limit)
        rows = self._rows(
            "SELECT database, schema_name, container_name, column_name, "  # noqa: S608
            "native_type, description, native_description, "
            f"column_name LIKE ? ESCAPE '\\' AS by_name "
            f"FROM columns WHERE {' AND '.join(clauses)} "
            "ORDER BY container_name, ordinal LIMIT ?",
            [pattern, *params],
        )
        return [
            SearchHit(
                database=row["database"],
                schema_name=row["schema_name"],
                container_name=row["container_name"],
                column_name=row["column_name"],
                native_type=row["native_type"],
                description=_summarise(row["description"] or row["native_description"]),
                match_in="name" if row["by_name"] else "description",
            )
            for row in rows
        ]
