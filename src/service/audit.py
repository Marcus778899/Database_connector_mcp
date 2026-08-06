from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.log import log


@dataclass
class AuditContext:
    """Filled in by the operation while it runs; written out when it ends."""

    rendered_sql: str | None = None
    rows_returned: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AuditLogger:
    """
    One record per tool call: who asked, for what, what actually ran against the
    source, and how much came back.

    Deliberately not the row contents — that would make the audit trail a second
    copy of the data it exists to account for.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def operation(
        self, *, key_id: str, tool: str, params: dict[str, Any]
    ) -> Generator[AuditContext]:
        context = AuditContext()
        started = time.monotonic()
        try:
            yield context
        except Exception as exc:
            self._emit(key_id, tool, params, context, started, type(exc).__name__, exc)
            raise
        self._emit(key_id, tool, params, context, started, "ok", None)

    def _emit(
        self,
        key_id: str,
        tool: str,
        params: dict[str, Any],
        context: AuditContext,
        started: float,
        outcome: str,
        error: BaseException | None,
    ) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "key_id": key_id,
            "tool": tool,
            "params": {k: v for k, v in params.items() if v is not None},
            "outcome": outcome,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "rendered_sql": context.rendered_sql,
            "rows_returned": context.rows_returned,
            **context.extra,
        }
        if error is not None:
            record["error"] = str(error)

        line = json.dumps(record, ensure_ascii=False, default=str)
        if outcome == "ok":
            log.info(f"audit {tool} by {key_id}")
        else:
            log.warning(f"audit {tool} by {key_id} failed: {outcome}")

        if self.path is None:
            return
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def records(self) -> list[dict[str, Any]]:
        """Read the trail back. For tests and for answering "who read what"."""
        if self.path is None or not self.path.exists():
            return []
        with self._lock:
            text = self.path.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]
