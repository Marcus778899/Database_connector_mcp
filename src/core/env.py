from __future__ import annotations

import os
from pathlib import Path

from src.core.log import log


def _find_dotenv(filename: str, start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def parse_dotenv(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def load_repo_dotenv(
    *,
    filename: str = ".env",
    start: Path | None = None,
    override: bool = False,
) -> Path | None:
    start = Path.cwd() if start is None else start
    path = _find_dotenv(filename, start.resolve())
    if path is None:
        log.info(f"no {filename} found from {start}; using the ambient environment")
        return None

    applied = 0
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied += 1
    log.info(f"loaded {applied} variables from {path}")
    return path
