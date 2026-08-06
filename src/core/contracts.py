from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class ContainerType(StrEnum):
    TABLE = "table"
    VIEW = "view"
    COLLECTION = "collection"


class ProfileMode(StrEnum):
    DISTINCT_COUNT = "distinct_count"
    TOP_VALUES = "top_values"
    NULL_RATIO = "null_ratio"
    MIN_MAX = "min_max"


class ContainerInfo(BaseModel):
    database: str
    schema_name: str | None = None
    container_name: str
    container_type: ContainerType
    estimated_count: int | None = None


class ContainerPage(BaseModel):
    """One page of `list_containers`. `next_cursor` is None on the last page and
    opaque — callers pass it back untouched."""

    containers: list[ContainerInfo]
    next_cursor: str | None = None


class ColumnInfo(BaseModel):
    name: str
    ordinal: int
    native_type: str
    nullable: bool
    is_pk: bool
    is_fk: bool


class TopValue(BaseModel):
    value: str
    count: int


class ProfileResult(BaseModel):
    distinct_count: int | None = None
    null_ratio: float | None = None
    top_values: list[TopValue] | None = None
    min_value: str | None = None
    max_value: str | None = None
    # True when the figures describe only a prefix of the data.
    approximate: bool = False


@runtime_checkable
class SourceAdaptor(Protocol):
    "define all mcp methods"

    def list_databases(self) -> list[str]: ...

    def list_containers(
        self,
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage: ...

    def get_schema(self, container: str) -> list[ColumnInfo]: ...

    def get_sample(self, container: str, limit: int = 3) -> list[dict[str, Any]]: ...

    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult: ...

    def close(self) -> None: ...

    # False when unusable, e.g. the server dropped an idle connection. Pools
    # check this before handing a cached adapter out.
    def ping(self) -> bool: ...
