import pytest

from src.auth.permissions import (
    CLAIM_ANNOTATE_AS_HUMAN,
    CLAIM_CONTAINERS,
    CLAIM_DATABASES,
    CLAIM_RAW_SAMPLE,
    LEGACY_HUMAN_SCOPE,
    ContainerRules,
    Permissions,
)


def _granted(**claims) -> Permissions:
    return Permissions.from_claims(["get_schema"], claims)


# ---- the token that predates any of this ----


def test_a_token_carrying_only_scopes_is_unchanged_by_the_new_claims():
    """The whole point of adding claims rather than replacing scopes: nothing
    already issued has to be reissued."""
    rights = Permissions.from_claims(["get_schema", "get_sample"], {})

    assert rights.may_call("get_schema") is True
    assert rights.may_call("get_sample") is True
    assert rights.may_call("inventory_start") is False
    assert rights.may_use_database("anything") is True
    assert rights.may_read("any_table") is True


def test_raw_rows_are_not_granted_by_silence():
    """The one claim whose absence means no. A token issued before masking
    existed must not be read as permission to bypass it."""
    assert Permissions.from_claims(["get_sample"], {}).allow_raw_sample is False


# ---- stdio ----


def test_stdio_is_unrestricted_because_there_is_nothing_left_to_restrict():
    """Whoever spawned the process already holds the database credential."""
    local = Permissions.local()

    assert local.may_call("anything") is True
    assert local.may_read("users") is True
    assert local.may_use_database("whatever") is True
    assert local.allow_raw_sample is True


def test_stdio_still_does_not_write_descriptions_as_a_persons():
    """Unrestricted is about access, not about provenance: an agent over stdio
    is still an agent."""
    assert Permissions.local().annotate_as_human is False


# ---- databases ----


def test_a_named_database_list_excludes_the_others():
    rights = _granted(**{CLAIM_DATABASES: ["analytics"]})

    assert rights.may_use_database("analytics") is True
    assert rights.may_use_database("payroll") is False


def test_a_restricted_token_may_not_ride_the_servers_default():
    """`database=None` means "whatever this server is pointed at", which is not
    a database this key was named on."""
    assert _granted(**{CLAIM_DATABASES: ["analytics"]}).may_use_database(None) is False


def test_no_database_claim_means_all_of_them():
    assert _granted().may_use_database(None) is True
    assert _granted().may_use_database("anything") is True


# ---- containers ----


def test_an_allow_list_excludes_what_is_not_on_it():
    rights = _granted(**{CLAIM_CONTAINERS: {"allow": ["dim_*", "fct_*"]}})

    assert rights.may_read("dim_product") is True
    assert rights.may_read("fct_orders") is True
    assert rights.may_read("users") is False


def test_a_deny_list_excludes_only_what_is_on_it():
    rights = _granted(**{CLAIM_CONTAINERS: {"deny": ["*_pii", "secrets"]}})

    assert rights.may_read("users") is True
    assert rights.may_read("users_pii") is False
    assert rights.may_read("secrets") is False


def test_deny_beats_allow():
    """Otherwise a broad allow quietly reopens what a deny was written to shut."""
    rules = ContainerRules(allow=["dim_*"], deny=["dim_person*"])

    assert rules.permits("dim_product") is True
    assert rules.permits("dim_person") is False


def test_a_containers_claim_that_is_not_an_object_grants_nothing():
    """Malformed reads as "not granted". A claim someone got wrong must not
    become unrestricted access."""
    rights = _granted(**{CLAIM_CONTAINERS: "dim_*"})

    assert rights.may_read("dim_product") is False


@pytest.mark.parametrize("claim", [42, "everything", {"allow": "dim_*"}, None])
def test_a_malformed_database_claim_grants_nothing_rather_than_everything(claim):
    rights = Permissions.from_claims(["get_schema"], {CLAIM_DATABASES: claim})

    assert rights.databases == []


# ---- raw rows ----


def test_raw_rows_take_the_claim_being_exactly_true():
    assert _granted(**{CLAIM_RAW_SAMPLE: True}).allow_raw_sample is True
    assert _granted(**{CLAIM_RAW_SAMPLE: "yes"}).allow_raw_sample is False
    assert _granted(**{CLAIM_RAW_SAMPLE: 1}).allow_raw_sample is False


# ---- writing as a person ----


def test_the_claim_grants_writing_as_a_person():
    assert _granted(**{CLAIM_ANNOTATE_AS_HUMAN: True}).annotate_as_human is True


def test_the_scope_that_carried_it_before_still_does():
    """A token issued against the earlier build keeps working."""
    rights = Permissions.from_claims(["inventory_annotate", LEGACY_HUMAN_SCOPE], {})

    assert rights.annotate_as_human is True
