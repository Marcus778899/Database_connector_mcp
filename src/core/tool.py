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
    # True when the adapter stopped short of a full scan, so the figures above
    # describe a prefix of the data rather than all of it.
    approximate: bool = False


@runtime_checkable
class SourceAdaptor(Protocol):
    "define all mcp methods"

    def list_databases(self) -> list[str]: ...

    def list_containers(
        self, database: str | None = None, schema: str | None = None
    ) -> list[ContainerInfo]: ...

    def get_schema(self, container: str) -> list[ColumnInfo]: ...

    def get_sample(self, container: str, limit: int = 3) -> list[dict[str, Any]]: ...

    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult: ...
