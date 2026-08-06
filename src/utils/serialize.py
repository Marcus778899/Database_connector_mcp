from __future__ import annotations

from base64 import b64encode
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

_PASSTHROUGH = (str, int, float, bool)


def jsonify(value: Any) -> Any:
    """
    Coerce a driver-native value into something JSON-serialisable.

    Unknown types fall back to str() instead of raising: a readable
    approximation beats a failed tool call.
    """
    if value is None or isinstance(value, _PASSTHROUGH):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        # str(b"x") gives "b'x'", which is not round-trippable
        return b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (Decimal, UUID, timedelta)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonify(v) for v in value]
    return str(value)
