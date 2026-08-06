"""
What a caller may do, as opposed to who they are.

A list of tool names was enough while every key that could call `get_sample`
was equally trusted with what came back. Opening the server to people who
should see `dim_product` but not `users` needs more, and the extra claims are
carried by the same token — added rather than replacing `scopes`, so a token
issued before any of this existed keeps working exactly as it did.

The judgement about *which* columns are personal belongs elsewhere and is
correctable. Whether a given key may see unmasked rows is decided here, in
code, and nothing outside can talk it round.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

from pydantic import BaseModel, Field

# Claims beyond `scopes`. Absent means "no restriction of this kind", except
# for `allow_raw_sample`, where absent means no.
CLAIM_DATABASES = "databases"
CLAIM_CONTAINERS = "containers"
CLAIM_RAW_SAMPLE = "allow_raw_sample"
CLAIM_ANNOTATE_AS_HUMAN = "annotate_as_human"

# The scope name that carried this before there were claims for it.
LEGACY_HUMAN_SCOPE = "annotate:human"


class ContainerRules(BaseModel):
    """Globs, because a catalog is named in families: `dim_*`, `*_pii`."""

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)

    def permits(self, container: str) -> bool:
        """Deny wins. An empty allow list is "anything not denied", so that a
        token saying nothing about containers keeps working."""
        if any(fnmatch(container, pattern) for pattern in self.deny):
            return False
        if not self.allow:
            return True
        return any(fnmatch(container, pattern) for pattern in self.allow)


class Permissions(BaseModel):
    """
    Everything a key is allowed, in one object.

    Built from a verified token, or from `local()` for stdio — where the caller
    already holds the database credential and there is nothing left to protect.
    """

    tools: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)
    containers: ContainerRules = Field(default_factory=ContainerRules)
    allow_raw_sample: bool = False
    annotate_as_human: bool = False
    # False for a token, True for stdio: the difference between "allowed
    # everything that was granted" and "there is nothing to grant".
    unrestricted: bool = False

    @classmethod
    def local(cls) -> Permissions:
        """No token: stdio, where spawning the process already handed over the
        database credential."""
        return cls(unrestricted=True, allow_raw_sample=True)

    @classmethod
    def from_claims(cls, scopes: list[str], claims: dict[str, Any]) -> Permissions:
        """
        Read a verified token.

        Anything malformed is read as "not granted" rather than ignored: a
        `containers` claim that is not an object should not silently become
        unrestricted access.
        """
        raw_containers = claims.get(CLAIM_CONTAINERS)
        containers = ContainerRules()
        if isinstance(raw_containers, dict):
            containers = ContainerRules(
                allow=_strings(raw_containers.get("allow")),
                deny=_strings(raw_containers.get("deny")),
            )
        elif raw_containers is not None:
            containers = ContainerRules(deny=["*"])

        return cls(
            tools=list(scopes),
            databases=_strings(claims.get(CLAIM_DATABASES)),
            containers=containers,
            allow_raw_sample=claims.get(CLAIM_RAW_SAMPLE) is True,
            annotate_as_human=(
                claims.get(CLAIM_ANNOTATE_AS_HUMAN) is True
                or LEGACY_HUMAN_SCOPE in scopes
            ),
        )

    # ---- the questions the tools ask ----

    def may_call(self, tool: str) -> bool:
        return self.unrestricted or tool in self.tools

    def may_use_database(self, database: str | None) -> bool:
        """An unnamed database is the server's own default, which a key
        restricted to particular ones has not been given."""
        if self.unrestricted or not self.databases:
            return True
        return database is not None and database in self.databases

    def may_read(self, container: str) -> bool:
        return self.unrestricted or self.containers.permits(container)


def _strings(value: Any) -> list[str]:
    """A claim that is not a list of strings grants nothing."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
