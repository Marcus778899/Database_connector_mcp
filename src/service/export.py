"""
Writing the inventory out as a file.

The point of it is what it does *not* do: an export returns where it wrote and
how much, never the contents. A catalog of any size is the one thing that must
not travel through a tool result, and "a full sweep" is exactly the request
that would otherwise do it.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from src.core.log import log
from src.service.staging import StagingStore, StoredColumn, StoredContainer

ExportFormat = Literal["markdown", "csv", "dbt_yaml"]

_SUFFIXES: dict[str, str] = {"markdown": ".md", "csv": ".csv", "dbt_yaml": ".yml"}

# How much of the catalog is held in memory at once.
_PAGE = 200


class ExportError(Exception):
    """The export cannot be written where it was asked to go."""


class ExportResult(BaseModel):
    """Where it went and how big it is — deliberately not what is in it."""

    path: str
    bytes_written: int
    containers: int
    columns: int


def resolve_target(export_dir: Path, path: str | None, format: str) -> Path:
    """
    Where an export may be written, which is only ever under `export_dir`.

    Resolved before the check, so neither `../` nor a symlink pointing out of
    the directory gets there: the caller is an agent relaying a path it was
    given, and this is the one place that can tell.
    """
    root = Path(export_dir).expanduser().resolve()
    if path is None:
        return root / f"inventory{_SUFFIXES[format]}"

    candidate = Path(path).expanduser()
    target = (
        (root / candidate).resolve()
        if not candidate.is_absolute()
        else (candidate.resolve())
    )
    if target != root and not target.is_relative_to(root):
        raise ExportError(
            f"{path!r} resolves outside the export directory ({root}); an export "
            "may only be written under it"
        )
    if target == root or target.is_dir():
        raise ExportError(f"{path!r} is a directory; name the file to write")
    return target


def export_inventory(
    store: StagingStore,
    export_dir: Path,
    *,
    format: ExportFormat = "markdown",
    database: str | None = None,
    path: str | None = None,
    permits: Callable[[str], bool] | None = None,
) -> ExportResult:
    """
    Write the inventory out, and report only where it went.

    `permits` decides which containers belong in it — a key restricted to part
    of the catalog gets a file covering that part, which is more use to it than
    a refusal and is the same rule the listings apply.
    """
    target = resolve_target(export_dir, path, format)
    target.parent.mkdir(parents=True, exist_ok=True)

    writer = {"markdown": _markdown, "csv": _csv, "dbt_yaml": _dbt_yaml}[format]
    containers = columns = 0
    written = 0
    with target.open("w", encoding="utf-8", newline="") as handle:
        for chunk, more_containers, more_columns in writer(store, database, permits):
            handle.write(chunk)
            written += len(chunk.encode("utf-8"))
            containers += more_containers
            columns += more_columns

    log.info(f"inventory exported to {target} ({written} bytes, {containers} tables)")
    return ExportResult(
        path=str(target),
        bytes_written=written,
        containers=containers,
        columns=columns,
    )


# ---- walking the inventory ----


def _walk(
    store: StagingStore,
    database: str | None,
    permits: Callable[[str], bool] | None = None,
) -> Iterator[tuple[StoredContainer, list[StoredColumn]]]:
    """Every container the caller may see, with its columns, a page at a time."""
    cursor: str | None = None
    while True:
        page = store.containers(database, limit=_PAGE, cursor=cursor)
        for container in page.containers:
            if permits is not None and not permits(container.container_name):
                continue
            yield container, _all_columns(store, container)
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _all_columns(store: StagingStore, container: StoredContainer) -> list[StoredColumn]:
    columns: list[StoredColumn] = []
    cursor: str | None = None
    while True:
        page = store.columns(
            container.database,
            container.container_name,
            container.schema_name or None,
            limit=_PAGE,
            cursor=cursor,
            include_profile=False,
        )
        columns.extend(page.columns)
        if page.next_cursor is None:
            return columns
        cursor = page.next_cursor


def _described(item: StoredContainer | StoredColumn) -> str:
    """What was written about it, or what the source said, or nothing."""
    return item.description or item.native_description or ""


# ---- formats ----


def _markdown(
    store: StagingStore,
    database: str | None,
    permits: Callable[[str], bool] | None = None,
) -> Iterator[tuple[str, int, int]]:
    """A data dictionary for a person to read."""
    scope = database or "every database"
    yield (
        f"# Data dictionary — {scope}\n\n"
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')} from the "
        "inventory. Descriptions marked *written* came from `inventory_annotate`; "
        "the rest are the source's own comments.\n",
        0,
        0,
    )

    for container, columns in _walk(store, database, permits):
        heading = f"\n## {container.container_name}\n\n"
        facts = [container.container_type]
        if container.estimated_count is not None:
            facts.append(f"~{container.estimated_count:,} rows")
        body = f"{heading}{' · '.join(facts)}\n"
        described = _described(container)
        if described:
            body += f"\n{described}\n"
        if container.error:
            body += f"\n> Not readable at the last scan: {container.error}\n"
            yield body, 1, 0
            continue

        body += "\n| column | type | null | key | description |\n"
        body += "|---|---|---|---|---|\n"
        for column in columns:
            key = "PK" if column.is_pk else ""
            if column.is_fk:
                target = column.references_container or "?"
                if column.references_column:
                    target += f".{column.references_column}"
                key = f"{key} FK → {target}".strip()
            note = _described(column)
            if column.description and column.description_source != "native":
                note = f"{note} *(written)*" if note else ""
            body += (
                f"| `{column.column_name}` | {column.native_type} | "
                f"{'yes' if column.nullable else 'no'} | {key} | "
                f"{_cell(note)} |\n"
            )
        yield body, 1, len(columns)


def _cell(text: str) -> str:
    """A pipe or a newline in a description would break the row it is in."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _csv(
    store: StagingStore,
    database: str | None,
    permits: Callable[[str], bool] | None = None,
) -> Iterator[tuple[str, int, int]]:
    """One row per column, for a spreadsheet or another tool to read."""
    header = [
        "database",
        "schema",
        "container",
        "column",
        "ordinal",
        "native_type",
        "nullable",
        "is_pk",
        "is_fk",
        "references",
        "description",
        "description_source",
        "sensitivity",
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    yield buffer.getvalue(), 0, 0

    for container, columns in _walk(store, database, permits):
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        for column in columns:
            references = ""
            if column.references_container:
                references = column.references_container
                if column.references_column:
                    references += f".{column.references_column}"
            writer.writerow(
                [
                    column.database,
                    column.schema_name,
                    column.container_name,
                    column.column_name,
                    column.ordinal,
                    column.native_type,
                    int(column.nullable),
                    int(column.is_pk),
                    int(column.is_fk),
                    references,
                    _described(column),
                    column.description_source or "",
                    str(column.sensitivity) if column.sensitivity else "",
                ]
            )
        yield buffer.getvalue(), 1, len(columns)


def _dbt_yaml(
    store: StagingStore,
    database: str | None,
    permits: Callable[[str], bool] | None = None,
) -> Iterator[tuple[str, int, int]]:
    """A `schema.yml` that can be dropped into a dbt project."""
    yield "version: 2\n\nmodels:\n", 0, 0

    for container, columns in _walk(store, database, permits):
        if not columns:
            continue  # a model with no columns is not one dbt can use
        body = f"  - name: {_yaml(container.container_name)}\n"
        described = _described(container)
        if described:
            body += f"    description: {_yaml(described)}\n"
        body += "    columns:\n"
        for column in columns:
            body += f"      - name: {_yaml(column.column_name)}\n"
            note = _described(column)
            if note:
                body += f"        description: {_yaml(note)}\n"
        yield body, 1, len(columns)


def _yaml(text: str) -> str:
    """
    A scalar, quoted so nothing in it can restructure the document.

    JSON strings are valid YAML 1.2 scalars, so `json.dumps` does the escaping
    — including the newlines and colons that make hand-written YAML a
    liability, and without adding a dependency for one line of work.
    """
    return json.dumps(text, ensure_ascii=False)
