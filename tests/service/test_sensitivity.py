import pytest

from src.core.contracts import Sensitivity
from src.service.sensitivity import (
    detect,
    from_name,
    from_values,
    mask_rows,
    mask_value,
    normalise,
    sensitive_columns,
)


# ---- reading the name ----


@pytest.mark.parametrize(
    ("name", "flattened"),
    [
        ("user_email", "useremail"),
        ("userEmail", "useremail"),
        ("USER-EMAIL", "useremail"),
        ("e_mail", "email"),
    ],
)
def test_a_name_is_read_however_it_is_spelled(name: str, flattened: str):
    assert normalise(name) == flattened


@pytest.mark.parametrize(
    "name",
    [
        "email",
        "user_email",
        "emailAddress",
        "phone",
        "mobile_number",
        "ssn",
        "national_id",
        "passport_no",
        "credit_card",
        "iban",
        "home_address",
        "postcode",
        "date_of_birth",
        "first_name",
        "latitude",
    ],
)
def test_a_name_that_gives_it_away(name: str):
    assert from_name(name) is Sensitivity.PII


@pytest.mark.parametrize("name", ["password", "api_key", "access_token", "secret"])
def test_a_secret_is_its_own_level(name: str):
    """Nothing about a secret's shape is worth showing, so it is masked
    differently."""
    assert from_name(name) is Sensitivity.SECRET


@pytest.mark.parametrize(
    "name", ["id", "total", "created_at", "status", "quantity", "description"]
)
def test_an_ordinary_name_says_nothing(name: str):
    assert from_name(name) is None


# ---- reading the values ----


def test_a_column_of_email_addresses_is_recognised_whatever_it_is_called():
    assert from_values(["a@x.com", "b@y.org", "c@z.net"]) is Sensitivity.PII


def test_a_column_of_phone_numbers_is_recognised():
    values = ["+1 555 123 4567", "+44 20 7946 0958", "0912345678"]

    assert from_values(values) is Sensitivity.PII


def test_one_address_in_a_column_of_prose_is_a_coincidence():
    """Masking a `notes` column because someone wrote an address in it helps
    nobody, so a majority has to match."""
    values = ["hello", "how are you", "mail me at a@x.com", "bye", "ok"]

    assert from_values(values) is None


def test_too_few_values_to_judge():
    assert from_values(["a@x.com"]) is None
    assert from_values([]) is None


def test_nulls_are_not_evidence_either_way():
    assert from_values([None, None, "a@x.com", "b@y.com", "c@z.com"]) is Sensitivity.PII


def test_the_name_is_believed_before_the_values():
    """A `password` column full of things that look like nothing is still a
    password column."""
    assert detect("password", ["a@x.com", "b@x.com", "c@x.com"]) is Sensitivity.SECRET


# ---- masking ----


def test_an_email_keeps_the_shape_that_makes_it_useful():
    """An agent reasoning about the column needs to see that it holds email
    addresses. It does not need the addresses."""
    masked = mask_value("alice@example.com", Sensitivity.PII)

    assert masked == "a***@***.com"
    assert "example" not in masked
    assert "alice" not in masked


def test_a_secret_keeps_nothing():
    assert mask_value("hunter2", Sensitivity.SECRET) == "***"
    assert mask_value("sk-1234567890abcdef", Sensitivity.SECRET) == "***"


def test_a_short_value_is_hidden_entirely():
    """Two characters either side of a four-character value is the whole value."""
    assert mask_value("abcd", Sensitivity.PII) == "***"


def test_a_longer_value_keeps_its_ends():
    assert mask_value("+886912345678", Sensitivity.PII) == "+8***78"


def test_a_null_stays_null():
    """Masking a missing value into a present one would be a lie about the data."""
    assert mask_value(None, Sensitivity.PII) is None


def test_only_the_named_columns_are_touched():
    rows = [{"id": 1, "email": "a@x.com", "note": "hi"}]

    masked = mask_rows(rows, {"email": Sensitivity.PII})

    assert masked[0]["id"] == 1
    assert masked[0]["note"] == "hi"
    assert masked[0]["email"] == "a***@***.com"


def test_masking_nothing_leaves_the_rows_alone():
    rows = [{"id": 1}]

    assert mask_rows(rows, {}) is rows


# ---- choosing what to mask ----


def test_a_sample_is_judged_when_nothing_is_on_record():
    """Masking has to work on a server with no inventory at all."""
    rows = [{"id": 1, "contact": "a@x.com"}, {"id": 2, "contact": "b@y.com"}]

    assert sensitive_columns(rows) == {}  # two values is not enough to judge

    rows.append({"id": 3, "contact": "c@z.com"})
    assert sensitive_columns(rows) == {"contact": Sensitivity.PII}


def test_what_the_inventory_recorded_wins():
    """It may have been corrected by a person looking at the thing; a pattern
    match has not."""
    rows = [{"email": "a@x.com"}, {"email": "b@y.com"}, {"email": "c@z.com"}]

    decided = sensitive_columns(rows, {"email": Sensitivity.NONE})

    assert decided == {}, "a column ruled harmless stays unmasked"


def test_a_recorded_level_covers_a_column_no_pattern_would_catch():
    rows = [{"internal_code": "AB-1"}, {"internal_code": "CD-2"}]

    decided = sensitive_columns(rows, {"internal_code": Sensitivity.SECRET})

    assert decided == {"internal_code": Sensitivity.SECRET}


def test_an_empty_sample_decides_nothing():
    assert sensitive_columns([]) == {}


@pytest.mark.parametrize(
    "address",
    ["user@localhost", "ops@internal-payroll", "a@b", "weird@.com"],
)
def test_a_domain_with_no_public_suffix_is_hidden_entirely(address: str):
    """
    `internal-payroll` names the system an address belongs to, so keeping it
    would show the thing this is here to hide — and appending it after a dot
    that is not in the value would misdescribe it as well.
    """
    masked = mask_value(address, Sensitivity.PII)

    assert masked.endswith("@***")
    domain = address.partition("@")[2]
    assert domain.strip(".") not in masked


def test_a_real_suffix_is_still_kept():
    """It is what tells an agent these are addresses at one organisation."""
    assert mask_value("x@sub.example.co.uk", Sensitivity.PII) == "x***@***.uk"
    assert mask_value("alice@example.com", Sensitivity.PII) == "a***@***.com"
