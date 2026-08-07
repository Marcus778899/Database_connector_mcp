from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from src.utils.serialize import as_text, jsonify


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("s", "s"),
        (7, 7),
        (1.5, 1.5),
        (True, True),
        (datetime(2024, 1, 2, 3, 4, 5), "2024-01-02T03:04:05"),
        (date(2024, 1, 2), "2024-01-02"),
        (time(3, 4, 5), "03:04:05"),
        (timedelta(hours=1), "1:00:00"),
        (Decimal("1.20"), "1.20"),
        (
            UUID("00000000-0000-0000-0000-000000000001"),
            "00000000-0000-0000-0000-000000000001",
        ),
    ],
)
def test_scalars(value, expected):
    assert jsonify(value) == expected


@pytest.mark.parametrize("value", [b"raw", bytearray(b"raw"), memoryview(b"raw")])
def test_binary_becomes_base64(value):
    # str(b"raw") would give "b'raw'", which is neither the value nor decodable
    assert jsonify(value) == "cmF3"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"a": date(2024, 1, 2)}, {"a": "2024-01-02"}),
        ({1: "x"}, {"1": "x"}),
        ([date(2024, 1, 2), 1], ["2024-01-02", 1]),
        ((date(2024, 1, 2),), ["2024-01-02"]),
        ({"a": [{"b": Decimal("1")}]}, {"a": [{"b": "1"}]}),
    ],
)
def test_containers_are_walked(value, expected):
    assert jsonify(value) == expected


def test_unknown_type_falls_back_to_str():
    class Weird:
        def __str__(self) -> str:
            return "weird"

    assert jsonify(Weird()) == "weird"


# ---- as_text ----


def test_as_text_keeps_none_as_none():
    """A profile bound that is absent is not the string "None"."""
    assert as_text(None) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("already", "already"),
        (7, "7"),
        (Decimal("9.50"), "9.50"),
        (date(2024, 1, 2), "2024-01-02"),
        (datetime(2024, 1, 2, 3, 4), "2024-01-02T03:04:00"),
        (
            UUID("00000000-0000-0000-0000-000000000001"),
            "00000000-0000-0000-0000-000000000001",
        ),
    ],
)
def test_as_text_reports_a_value_as_itself_rather_than_as_its_repr(value, expected):
    """Through `jsonify` first: `str(date(...))` happens to agree, and
    `str(Decimal)` and `str(bytes)` do not."""
    assert as_text(value) == expected


def test_as_text_of_bytes_is_the_base64_jsonify_chose():
    assert as_text(b"\x00\x01") == "AAE="
