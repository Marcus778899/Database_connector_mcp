"""
Which columns hold personal data, and what a sample of one may show.

The reason this exists: `get_sample` puts real rows into an LLM's context, and
from there into whatever logs, transcripts and training corpora that context
touches. An agent exploring a `users` table is the ordinary case, not the
adversarial one, which is why masking is the default rather than an option.

What counts as personal is a guess and is meant to be corrected —
`inventory_annotate` overrides anything decided here. What a given key may see
is not a guess and is not correctable from outside: that lives in `src/auth`.
"""

from __future__ import annotations

import re
from typing import Any

from src.core.contracts import Sensitivity

# Written by detection rather than by a person, so a rescan may overwrite it
# while an annotation stays put.
SOURCE_DETECTED = "detected"

# Column names that give it away. Matched against the name lowercased, with
# separators removed, so `e_mail`, `eMail` and `email` are one pattern.
_NAME_PATTERNS: tuple[tuple[str, Sensitivity], ...] = (
    (
        r"password|passwd|secret|apikey|accesstoken|privatekey|credential",
        Sensitivity.SECRET,
    ),
    (r"email|mail(address)?", Sensitivity.PII),
    (r"phone|mobile|telephone|msisdn", Sensitivity.PII),
    (r"ssn|socialsecurity|nationalid|idcard|passport|taxid", Sensitivity.PII),
    (r"creditcard|cardnumber|cardno|iban|bankaccount", Sensitivity.PII),
    (r"(home|postal|street|mailing)?address|postcode|zipcode", Sensitivity.PII),
    (r"birth(date|day)|dateofbirth|dob$", Sensitivity.PII),
    (r"latitude|longitude|geolocation", Sensitivity.PII),
    (r"firstname|lastname|surname|givenname|fullname|realname", Sensitivity.PII),
)

# What the values themselves look like, for a column whose name says nothing.
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], Sensitivity], ...] = (
    (re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"), Sensitivity.PII),
    (re.compile(r"^\+?\d[\d\s().-]{7,}\d$"), Sensitivity.PII),
    (re.compile(r"^\d{3}-\d{2}-\d{4}$"), Sensitivity.PII),
    (re.compile(r"^(?:\d[ -]?){13,19}$"), Sensitivity.PII),
)

_SEPARATORS = re.compile(r"[^a-z0-9]+")

# Enough values to be convincing, few enough to stay cheap.
_SAMPLE_FOR_DETECTION = 20

# What a masked value looks like. Type-preserving where it can be: an agent
# reasoning about a column needs to see that it holds an email address, and
# nothing more than that.
_MASK = "***"


def normalise(name: str) -> str:
    """`user_email`, `userEmail` and `USER-EMAIL` are the same name here."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return _SEPARATORS.sub("", spaced.lower())


def from_name(column_name: str) -> Sensitivity | None:
    """What the column is called, if that settles it."""
    flattened = normalise(column_name)
    for pattern, level in _NAME_PATTERNS:
        if re.search(pattern, flattened):
            return level
    return None


def from_values(values: list[Any]) -> Sensitivity | None:
    """
    What the values look like, for a column whose name gives nothing away.

    A majority has to match: one email address in a free-text column is a
    coincidence, and masking a `notes` column because of it helps nobody.
    """
    texts = [str(value) for value in values if value not in (None, "")]
    if len(texts) < 3:
        return None
    for pattern, level in _VALUE_PATTERNS:
        matched = sum(1 for text in texts if pattern.match(text.strip()))
        if matched * 2 > len(texts):
            return level
    return None


def detect(column_name: str, values: list[Any] | None = None) -> Sensitivity | None:
    """The name first, then the values. None means nothing was recognised —
    not that the column is safe, which is a claim this cannot make."""
    return from_name(column_name) or (from_values(values) if values else None)


def mask_value(value: Any, level: Sensitivity) -> Any:
    """
    Hide a value while leaving its shape.

    `a***@***.com` still tells an agent the column holds email addresses at a
    particular domain shape, which is what it needs to reason about the table.
    A secret keeps nothing at all: there is no shape worth showing.
    """
    if value is None:
        return None
    if level is Sensitivity.SECRET:
        return _MASK
    text = str(value)
    if "@" in text:
        local, _, domain = text.partition("@")
        suffix = domain.rpartition(".")[2]
        return (
            f"{local[:1]}{_MASK}@{_MASK}.{suffix}"
            if suffix
            else f"{local[:1]}{_MASK}@{_MASK}"
        )
    if len(text) <= 4:
        return _MASK
    return f"{text[:2]}{_MASK}{text[-2:]}"


def mask_rows(
    rows: list[dict[str, Any]], sensitive: dict[str, Sensitivity]
) -> list[dict[str, Any]]:
    """Apply `sensitive` to every row. Columns not in it are left alone."""
    if not sensitive:
        return rows
    return [
        {
            key: mask_value(value, sensitive[key]) if key in sensitive else value
            for key, value in row.items()
        }
        for row in rows
    ]


def sensitive_columns(
    rows: list[dict[str, Any]], known: dict[str, Sensitivity] | None = None
) -> dict[str, Sensitivity]:
    """
    Which columns of a sample to mask.

    `known` is what the inventory recorded, and it wins: it may have been
    corrected by hand. Anything it does not cover is judged from the name and
    then the values here and now, because masking has to work on a server with
    no inventory at all.
    """
    decided = dict(known or {})
    for key in rows[0] if rows else ():
        if key in decided:
            continue
        level = detect(key, [row.get(key) for row in rows[:_SAMPLE_FOR_DETECTION]])
        if level is not None and level is not Sensitivity.NONE:
            decided[key] = level
    return {
        name: level for name, level in decided.items() if level is not Sensitivity.NONE
    }
