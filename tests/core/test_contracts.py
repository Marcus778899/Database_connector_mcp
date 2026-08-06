from typing import Any

import pytest
from pydantic import ValidationError

from src.core.contracts import (
    ContainerType,
    ProfileMode,
    ContainerInfo,
    ContainerPage,
    ColumnInfo,
    TopValue,
    ProfileResult,
    Sensitivity,
    SourceAdaptor,
)


def test_container_type_enum():
    assert ContainerType.TABLE == "table"
    assert ContainerType.VIEW == "view"
    assert ContainerType.COLLECTION == "collection"


def test_profile_mode_enum():
    assert ProfileMode.DISTINCT_COUNT == "distinct_count"
    assert ProfileMode.TOP_VALUES == "top_values"
    assert ProfileMode.NULL_RATIO == "null_ratio"
    assert ProfileMode.MIN_MAX == "min_max"


def test_sensitivity_enum():
    assert Sensitivity.NONE == "none"
    assert Sensitivity.PII == "pii"
    assert Sensitivity.SECRET == "secret"


def test_container_info_valid():
    info = ContainerInfo(
        database="mydb",
        schema_name="public",
        container_name="users",
        container_type=ContainerType.TABLE,
        estimated_count=1000,
    )
    assert info.database == "mydb"
    assert info.schema_name == "public"
    assert info.container_name == "users"
    assert info.container_type == ContainerType.TABLE
    assert info.estimated_count == 1000


def test_container_info_defaults():
    info = ContainerInfo(
        database="mydb",
        container_name="users",
        container_type=ContainerType.VIEW,
    )
    assert info.schema_name is None
    assert info.estimated_count is None


def test_container_info_invalid():
    with pytest.raises(ValidationError):
        ContainerInfo(  # type: ignore[call-arg] - the missing field is the point
            database="mydb",
            container_name="users",
        )


def test_column_info_valid():
    info = ColumnInfo(
        name="id",
        ordinal=1,
        native_type="INTEGER",
        nullable=False,
        is_pk=True,
        is_fk=False,
    )
    assert info.name == "id"
    assert info.ordinal == 1
    assert info.native_type == "INTEGER"
    assert info.nullable is False
    assert info.is_pk is True
    assert info.is_fk is False


def test_column_info_describes_itself_and_what_it_points_at():
    info = ColumnInfo(
        name="user_id",
        ordinal=2,
        native_type="INTEGER",
        nullable=False,
        is_pk=False,
        is_fk=True,
        native_description="who placed the order",
        references_container="users",
        references_column="id",
    )
    assert info.native_description == "who placed the order"
    assert (info.references_container, info.references_column) == ("users", "id")


def test_the_description_fields_default_to_none():
    """Every adapter written before they existed still compiles."""
    info = ColumnInfo(
        name="id",
        ordinal=1,
        native_type="INTEGER",
        nullable=False,
        is_pk=True,
        is_fk=False,
    )
    container = ContainerInfo(
        database="mydb", container_name="users", container_type=ContainerType.TABLE
    )
    assert info.native_description is None
    assert info.references_container is None
    assert container.native_description is None
    assert container.last_modified_at is None


def test_top_value_valid():
    tv = TopValue(value="admin", count=42)
    assert tv.value == "admin"
    assert tv.count == 42


def test_profile_result_defaults():
    result = ProfileResult()
    assert result.distinct_count is None
    assert result.null_ratio is None
    assert result.top_values is None
    assert result.min_value is None
    assert result.max_value is None


def test_profile_result_valid():
    result = ProfileResult(
        distinct_count=10,
        null_ratio=0.5,
        top_values=[TopValue(value="a", count=1)],
        min_value="a",
        max_value="z",
    )
    assert result.distinct_count == 10
    assert result.null_ratio == 0.5
    assert result.top_values is not None and len(result.top_values) == 1
    assert result.top_values[0].value == "a"
    assert result.min_value == "a"
    assert result.max_value == "z"


def test_source_adaptor_protocol():
    class DummyAdaptor:
        def list_databases(self) -> list[str]:
            return []

        def list_containers(
            self,
            database: str | None = None,
            schema: str | None = None,
            limit: int | None = None,
            cursor: str | None = None,
        ) -> ContainerPage:
            return ContainerPage(containers=[])

        def get_schema(self, container: str) -> list[ColumnInfo]:
            return []

        def get_sample(self, container: str, limit: int = 3) -> list[dict[str, Any]]:
            return []

        def profile_column(
            self, container: str, column: str, mode: ProfileMode
        ) -> ProfileResult:
            return ProfileResult()

        def default_profile_modes(self, column: ColumnInfo) -> tuple[ProfileMode, ...]:
            return ()

        def close(self) -> None:
            pass

        def ping(self) -> bool:
            return True

        def pop_rendered_sql(self) -> str | None:
            return None

    adaptor = DummyAdaptor()
    assert isinstance(adaptor, SourceAdaptor)

    class InvalidAdaptor:
        pass

    assert not isinstance(InvalidAdaptor(), SourceAdaptor)
