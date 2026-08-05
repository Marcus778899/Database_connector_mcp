import pytest
from pydantic import ValidationError
from typing import Any

from core.tool import (
    ContainerType,
    ProfileMode,
    ContainerInfo,
    ColumnInfo,
    TopValue,
    ProfileResult,
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
        ContainerInfo(
            database="mydb",
            container_name="users",
            # Missing required container_type
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
    assert len(result.top_values) == 1
    assert result.top_values[0].value == "a"
    assert result.min_value == "a"
    assert result.max_value == "z"


def test_source_adaptor_protocol():
    class DummyAdaptor:
        def list_databases(self) -> list[str]:
            return []

        def list_containers(
            self, database: str | None = None, schema: str | None = None
        ) -> list[ContainerInfo]:
            return []

        def get_schema(self, container: str) -> list[ColumnInfo]:
            return []

        def get_sample(self, container: str, limit: int = 3) -> list[dict[str, Any]]:
            return []

        def profile_column(
            self, container: str, column: str, mode: ProfileMode
        ) -> ProfileResult:
            return ProfileResult()

    adaptor = DummyAdaptor()
    assert isinstance(adaptor, SourceAdaptor)

    class InvalidAdaptor:
        pass

    assert not isinstance(InvalidAdaptor(), SourceAdaptor)
