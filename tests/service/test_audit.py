import json
import threading
from pathlib import Path

import pytest

from src.service.audit import AuditLogger


def test_nothing_is_written_without_a_path():
    trail = AuditLogger(None)

    with trail.operation(key_id="k", tool="t", params={}):
        pass

    assert trail.records() == []


def test_a_successful_operation_is_recorded(tmp_path: Path):
    trail = AuditLogger(tmp_path / "audit.jsonl")

    with trail.operation(
        key_id="alice", tool="get_sample", params={"container": "c"}
    ) as ctx:
        ctx.rendered_sql = "SELECT * FROM c LIMIT ?"
        ctx.rows_returned = 3

    (record,) = trail.records()
    assert record["key_id"] == "alice"
    assert record["tool"] == "get_sample"
    assert record["outcome"] == "ok"
    assert record["rendered_sql"] == "SELECT * FROM c LIMIT ?"
    assert record["rows_returned"] == 3
    assert record["ts"].endswith("+00:00")


def test_a_failure_is_recorded_and_still_raises(tmp_path: Path):
    trail = AuditLogger(tmp_path / "audit.jsonl")

    with pytest.raises(KeyError):
        with trail.operation(key_id="k", tool="get_schema", params={}):
            raise KeyError("missing")

    (record,) = trail.records()
    assert record["outcome"] == "KeyError"
    assert "missing" in record["error"]


def test_none_params_are_dropped(tmp_path: Path):
    trail = AuditLogger(tmp_path / "audit.jsonl")

    with trail.operation(key_id="k", tool="t", params={"a": 1, "b": None}):
        pass

    assert trail.records()[0]["params"] == {"a": 1}


def test_extra_fields_are_merged(tmp_path: Path):
    trail = AuditLogger(tmp_path / "audit.jsonl")

    with trail.operation(key_id="k", tool="t", params={}) as ctx:
        ctx.extra["job_id"] = "abc"

    assert trail.records()[0]["job_id"] == "abc"


def test_records_append_rather_than_replace(tmp_path: Path):
    trail = AuditLogger(tmp_path / "audit.jsonl")

    for tool in ("a", "b", "c"):
        with trail.operation(key_id="k", tool=tool, params={}):
            pass

    assert [r["tool"] for r in trail.records()] == ["a", "b", "c"]


def test_the_directory_is_created(tmp_path: Path):
    path = tmp_path / "nested" / "deeper" / "audit.jsonl"

    AuditLogger(path)

    assert path.parent.exists()


def test_every_line_is_valid_json(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    trail = AuditLogger(path)

    with trail.operation(key_id="k", tool="t", params={"weird": {"nested": [1, 2]}}):
        pass

    for line in path.read_text().splitlines():
        json.loads(line)


def test_unserialisable_params_do_not_break_the_trail(tmp_path: Path):
    """A record that cannot be written is worse than a lossy one."""
    trail = AuditLogger(tmp_path / "audit.jsonl")

    with trail.operation(key_id="k", tool="t", params={"obj": object()}):
        pass

    assert len(trail.records()) == 1


def test_concurrent_operations_do_not_interleave(tmp_path: Path):
    """Tool calls arrive from FastMCP's threadpool."""
    path = tmp_path / "audit.jsonl"
    trail = AuditLogger(path)
    barrier = threading.Barrier(16)

    def worker(index: int) -> None:
        barrier.wait()
        with trail.operation(key_id=f"k{index}", tool="t", params={"i": index}):
            pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = path.read_text().splitlines()
    assert len(lines) == 16
    for line in lines:
        json.loads(line)
