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


# ---- rotation ----


def _fill(trail: AuditLogger, records: int = 40) -> None:
    for index in range(records):
        with trail.operation(key_id="k", tool="t", params={"i": index}):
            pass


def test_the_trail_is_rotated_once_it_is_big_enough(tmp_path: Path):
    """Authentication turns this file into the record of who read what, so it
    has to survive being written to indefinitely."""
    path = tmp_path / "audit.jsonl"
    trail = AuditLogger(path, max_bytes=500, backups=3)

    _fill(trail)

    assert path.exists()
    assert path.with_suffix(".jsonl.1").exists()
    assert path.stat().st_size < 500 + 200


def test_no_more_than_the_configured_backups_are_kept(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    trail = AuditLogger(path, max_bytes=200, backups=2)

    _fill(trail, records=200)

    rotated = sorted(p.name for p in tmp_path.glob("audit.jsonl.*"))
    assert rotated == ["audit.jsonl.1", "audit.jsonl.2"]


def test_the_newest_rotation_is_the_one_just_moved_aside(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    trail = AuditLogger(path, max_bytes=300, backups=3)

    _fill(trail, records=60)

    first = path.with_suffix(".jsonl.1").read_text(encoding="utf-8")
    second = path.with_suffix(".jsonl.2").read_text(encoding="utf-8")
    newest_in_first = json.loads(first.splitlines()[-1])["params"]["i"]
    newest_in_second = json.loads(second.splitlines()[-1])["params"]["i"]
    assert newest_in_first > newest_in_second, ".1 must be the most recent"


def test_a_record_is_never_split_across_two_files(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    trail = AuditLogger(path, max_bytes=250, backups=5)

    _fill(trail, records=80)

    for written in [path, *tmp_path.glob("audit.jsonl.*")]:
        for line in written.read_text(encoding="utf-8").splitlines():
            json.loads(line)


def test_rotation_can_be_turned_off(tmp_path: Path):
    """For somewhere with its own logrotate, or a disk that will never fill."""
    path = tmp_path / "audit.jsonl"
    trail = AuditLogger(path, max_bytes=0)

    _fill(trail, records=100)

    assert list(tmp_path.glob("audit.jsonl.*")) == []
    assert len(trail.records()) == 100


def test_what_survives_rotation_is_an_unbroken_run(tmp_path: Path):
    """Records are lost only by falling off the end of the retention, never
    from the middle — a trail with a hole in it is worse than a short one."""
    path = tmp_path / "audit.jsonl"
    trail = AuditLogger(path, max_bytes=300, backups=9)

    _fill(trail, records=50)

    seen = []
    for written in [path, *tmp_path.glob("audit.jsonl.*")]:
        for line in written.read_text(encoding="utf-8").splitlines():
            seen.append(json.loads(line)["params"]["i"])
    assert sorted(seen) == list(range(min(seen), 50))
    assert 49 in seen, "the most recent record is the one that must be there"


def test_nothing_is_lost_while_the_retention_is_deep_enough(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    trail = AuditLogger(path, max_bytes=400, backups=20)

    _fill(trail, records=30)

    seen = []
    for written in [path, *tmp_path.glob("audit.jsonl.*")]:
        for line in written.read_text(encoding="utf-8").splitlines():
            seen.append(json.loads(line)["params"]["i"])
    assert sorted(seen) == list(range(30))
