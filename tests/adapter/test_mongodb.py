"""
mongo without a mongod.

The client is replaced, not the adapter. What that leaves real is everything
worth pinning down offline: the schema inferred from documents the fake hands
over, the pipeline each profile mode sends, and what the answers are turned
into. An aggregation's *result* is scripted — reimplementing `$group` here would
test the reimplementation.
"""

from datetime import datetime
from typing import Any

import pytest
from bson import Binary, Code, Decimal128, ObjectId, Regex, Timestamp
from pymongo.errors import OperationFailure

from src.adapter.base import UnknownColumnError, UnknownContainerError
from src.adapter.mongodb import MongoAdapter, _bson_type
from src.core.config import ConnectionInfo
from src.core.contracts import ColumnInfo, ContainerType, ProfileMode, SourceAdaptor

USERS = [
    {"_id": ObjectId("65f000000000000000000001"), "email": "a@x.com", "age": 30},
    {"_id": ObjectId("65f000000000000000000002"), "email": "b@x.com"},
    {"_id": ObjectId("65f000000000000000000003"), "email": None, "age": 41},
]


class FakeCollection:
    def __init__(
        self, name: str, documents: list[dict[str, Any]] | None = None
    ) -> None:
        self.name = name
        self.documents = documents or []
        self.result: list[dict[str, Any]] = []
        self.pipelines: list[list[dict[str, Any]]] = []
        self.counted = 0

    def find(self, limit: int = 0) -> Any:
        return iter(self.documents[:limit] if limit else self.documents)

    def aggregate(self, pipeline: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.pipelines.append(pipeline)
        assert kwargs.get("allowDiskUse") is True
        return iter(self.result)

    def estimated_document_count(self) -> int:
        self.counted += 1
        return len(self.documents)


class FakeDatabase:
    def __init__(self, collections: dict[str, FakeCollection], kinds: dict[str, str]):
        self._collections = collections
        self._kinds = kinds

    def list_collections(self) -> Any:
        return iter(
            [
                {"name": name, "type": self._kinds.get(name, "collection")}
                for name in self._collections
            ]
        )

    def __getitem__(self, name: str) -> FakeCollection:
        return self._collections.setdefault(name, FakeCollection(name))


class FakeAdmin:
    def __init__(self) -> None:
        self.pinged = 0

    def command(self, name: str) -> dict[str, Any]:
        self.pinged += 1
        return {"ok": 1}


class FakeClient:
    def __init__(
        self,
        database: FakeDatabase,
        *,
        databases: list[str] | None = None,
        may_list: bool = True,
    ) -> None:
        self._database = database
        self._databases = databases or ["shop", "admin", "local", "config"]
        self._may_list = may_list
        self.admin = FakeAdmin()
        self.closed = False

    def __getitem__(self, name: str) -> FakeDatabase:
        return self._database

    def list_database_names(self) -> list[str]:
        if not self._may_list:
            raise OperationFailure("not authorized")
        return self._databases

    def close(self) -> None:
        self.closed = True


def build(
    documents: dict[str, list[dict[str, Any]]] | None = None,
    *,
    kinds: dict[str, str] | None = None,
    **kwargs: Any,
) -> MongoAdapter:
    collections = {
        name: FakeCollection(name, docs)
        for name, docs in (documents or {"users": USERS}).items()
    }
    client = FakeClient(FakeDatabase(collections, kinds or {}))
    return MongoAdapter(client, database="shop", **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def adapter() -> MongoAdapter:
    return build()


# ---- construction ----


def test_satisfies_source_adaptor_protocol(adapter: MongoAdapter):
    checked: SourceAdaptor = adapter
    assert isinstance(checked, SourceAdaptor)


def test_mongo_serves_more_than_one_database():
    assert MongoAdapter.SUPPORTS_MULTIPLE_DATABASES is True


def test_from_connection_without_a_host_or_uri_is_refused():
    with pytest.raises(ValueError, match="requires a <REF>_HOST"):
        MongoAdapter.from_connection(ConnectionInfo(user="reader"))


def test_a_connection_with_no_database_is_refused():
    """There is no catalog without one: a mongo client on its own points at a
    server, not at anything to inventory."""
    with pytest.raises(ValueError, match="requires a <REF>_DB"):
        MongoAdapter.from_connection(ConnectionInfo(uri="mongodb://localhost:27017"))


def test_the_database_can_come_from_the_uri_path():
    adapter = MongoAdapter.from_connection(
        ConnectionInfo(uri="mongodb://localhost:27017/shop")
    )
    try:
        assert adapter._database == "shop"
    finally:
        adapter.close()


# ---- catalog ----


def test_list_databases_leaves_out_the_servers_own(adapter: MongoAdapter):
    assert adapter.list_databases() == ["shop"]


def test_a_login_without_listdatabases_still_gets_the_one_it_can_read():
    """Failing the call over a privilege the other tools do not need would make
    a readable database unreadable."""
    client = FakeClient(FakeDatabase({}, {}), may_list=False)
    adapter = MongoAdapter(client, database="shop")  # type: ignore[arg-type]

    assert adapter.list_databases() == ["shop"]


def test_list_containers_calls_a_collection_a_collection(adapter: MongoAdapter):
    containers = adapter.list_containers().containers

    assert [c.container_name for c in containers] == ["users"]
    assert containers[0].container_type == ContainerType.COLLECTION
    assert containers[0].schema_name is None
    assert containers[0].database == "shop"


def test_a_view_is_reported_as_one_and_not_counted():
    """Counting a view would mean running it."""
    adapter = build({"users": USERS, "recent": []}, kinds={"recent": "view"})

    counts = {
        c.container_name: (c.container_type, c.estimated_count)
        for c in adapter.list_containers().containers
    }

    assert counts["users"] == (ContainerType.COLLECTION, 3)
    assert counts["recent"] == (ContainerType.VIEW, None)


def test_list_containers_pages_with_a_cursor():
    adapter = build({name: [] for name in ("a", "b", "c")})

    page = adapter.list_containers(limit=2)

    assert [c.container_name for c in page.containers] == ["a", "b"]
    assert page.next_cursor == "b"
    assert [
        c.container_name
        for c in adapter.list_containers(limit=2, cursor="b").containers
    ] == ["c"]
    assert adapter.list_containers(limit=2, cursor="c").next_cursor is None


def test_mongos_own_collections_are_hidden():
    adapter = build({"users": [], "system.views": []})

    assert [c.container_name for c in adapter.list_containers().containers] == ["users"]


def test_another_database_is_refused(adapter: MongoAdapter):
    with pytest.raises(UnknownContainerError, match="serves 'shop'"):
        adapter.list_containers(database="other")


def test_a_schema_argument_is_refused(adapter: MongoAdapter):
    with pytest.raises(UnknownContainerError, match="no schema layer"):
        adapter.list_containers(schema="public")


# ---- inferred schema ----


def test_get_schema_reports_the_fields_the_documents_carry(adapter: MongoAdapter):
    columns = adapter.get_schema("users")

    assert [c.name for c in columns] == ["_id", "email", "age"]
    # mongo keeps a document's fields in the order they were written
    assert [c.ordinal for c in columns] == [1, 2, 3]


def test_the_id_is_the_key(adapter: MongoAdapter):
    columns = {c.name: c for c in adapter.get_schema("users")}

    assert columns["_id"].is_pk is True
    assert columns["email"].is_pk is False
    # mongo declares no references, and guessing one would put a join in the
    # catalog that nothing enforces
    assert all(c.is_fk is False for c in columns.values())


def test_a_missing_field_counts_as_a_null_one(adapter: MongoAdapter):
    """To anything reading this catalog they are the same missing value."""
    columns = {c.name: c for c in adapter.get_schema("users")}

    assert columns["_id"].nullable is False
    # absent from one document, null in another
    assert columns["age"].nullable is True
    assert columns["email"].nullable is True


def test_a_field_of_more_than_one_type_is_reported_as_all_of_them():
    adapter = build({"mixed": [{"v": "a"}, {"v": 1}, {"v": None}]})

    assert adapter.get_schema("mixed")[0].native_type == "int|string"


def test_a_field_the_sample_only_ever_saw_null_has_no_type():
    adapter = build({"mixed": [{"v": None}]})

    assert adapter.get_schema("mixed")[0].native_type == ""


def test_the_schema_is_inferred_from_a_bounded_sample():
    adapter = build({"users": USERS}, schema_sample=2)

    adapter.get_schema("users")

    assert "limit(2)" in (adapter.pop_rendered_sql() or "")


def test_only_top_level_fields_are_reported():
    """Flattening `a.b.c` turns one collection into an unbounded list of paths,
    and the paths would still only describe the sample."""
    adapter = build({"nested": [{"profile": {"city": "taipei"}, "tags": ["a"]}]})

    types = {c.name: c.native_type for c in adapter.get_schema("nested")}

    assert types == {"profile": "object", "tags": "array"}


def test_get_schema_of_an_unknown_container(adapter: MongoAdapter):
    with pytest.raises(UnknownContainerError, match="absent"):
        adapter.get_schema("absent")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, "bool"),  # a bool is an int to python, and not to mongo
        (7, "int"),
        (2**40, "long"),
        (1.5, "double"),
        (Decimal128("1.5"), "decimal"),
        ("x", "string"),
        (ObjectId("65f000000000000000000001"), "objectId"),
        (datetime(2024, 1, 2), "date"),
        (Timestamp(1, 1), "timestamp"),
        (Binary(b"\x00"), "binData"),
        (b"\x00", "binData"),
        (Regex("^a"), "regex"),
        (Code("f(){}"), "javascript"),
        ([1], "array"),
        ({"a": 1}, "object"),
    ],
)
def test_bson_types_are_read_back_off_the_decoded_value(value: Any, expected: str):
    assert _bson_type(value) == expected


# ---- which statistics suit a bson type ----


def _modes_for(native_type: str) -> tuple[ProfileMode, ...]:
    return build().default_profile_modes(
        ColumnInfo(
            name="c",
            ordinal=1,
            native_type=native_type,
            nullable=True,
            is_pk=False,
            is_fk=False,
        )
    )


@pytest.mark.parametrize("native_type", ["int", "long", "double", "decimal"])
def test_a_number_gets_a_range(native_type: str):
    """`long` is the one the inherited sql-name matching reads as nothing."""
    assert _modes_for(native_type) == (ProfileMode.NULL_RATIO, ProfileMode.MIN_MAX)


@pytest.mark.parametrize("native_type", ["date", "timestamp"])
def test_a_time_gets_a_range(native_type: str):
    assert _modes_for(native_type) == (ProfileMode.NULL_RATIO, ProfileMode.MIN_MAX)


@pytest.mark.parametrize("native_type", ["string", "bool"])
def test_a_categorical_field_gets_counted_before_it_is_listed(native_type: str):
    assert _modes_for(native_type) == (
        ProfileMode.NULL_RATIO,
        ProfileMode.DISTINCT_COUNT,
        ProfileMode.TOP_VALUES,
    )


@pytest.mark.parametrize("native_type", ["object", "array", "binData", "objectId", ""])
def test_a_field_no_statistic_describes_gets_only_a_null_ratio(native_type: str):
    assert _modes_for(native_type) == (ProfileMode.NULL_RATIO,)


def test_a_field_of_two_types_gets_only_a_null_ratio():
    """A range over values that are sometimes text and sometimes numbers orders
    them by BSON type, which describes the encoding rather than the data."""
    assert _modes_for("int|string") == (ProfileMode.NULL_RATIO,)


# ---- profiling ----


def _answer(adapter: MongoAdapter, rows: list[dict[str, Any]]) -> FakeCollection:
    collection = adapter._db["users"]
    collection.result = rows
    return collection


def test_profile_null_ratio(adapter: MongoAdapter):
    collection = _answer(adapter, [{"_id": None, "total": 4, "nulls": 1}])

    assert (
        adapter.profile_column("users", "email", ProfileMode.NULL_RATIO).null_ratio
        == 0.25
    )
    # missing and null both count, which is what `$type` is asked for
    stage = collection.pipelines[-1][0]["$group"]["nulls"]["$sum"]["$cond"][0]
    assert stage == {"$in": [{"$type": "$email"}, ["missing", "null"]]}


def test_profile_of_an_empty_collection_has_no_ratio(adapter: MongoAdapter):
    """No documents means no ratio to report, which is not zero."""
    _answer(adapter, [])

    assert (
        adapter.profile_column("users", "email", ProfileMode.NULL_RATIO).null_ratio
        is None
    )


def test_profile_min_max(adapter: MongoAdapter):
    _answer(adapter, [{"_id": None, "lo": 30, "hi": 41}])

    result = adapter.profile_column("users", "age", ProfileMode.MIN_MAX)

    assert (result.min_value, result.max_value) == ("30", "41")


def test_profile_min_max_reports_a_date_as_itself(adapter: MongoAdapter):
    _answer(adapter, [{"_id": None, "lo": datetime(2024, 1, 2), "hi": None}])

    result = adapter.profile_column("users", "age", ProfileMode.MIN_MAX)

    assert (result.min_value, result.max_value) == ("2024-01-02T00:00:00", None)


def test_profile_distinct_count_does_not_count_the_nulls(adapter: MongoAdapter):
    """Matching COUNT(DISTINCT x). `$ne: null` drops a missing field too."""
    collection = _answer(adapter, [{"n": 2}])

    result = adapter.profile_column("users", "email", ProfileMode.DISTINCT_COUNT)

    assert result.distinct_count == 2
    assert collection.pipelines[-1][0] == {"$match": {"email": {"$ne": None}}}


def test_profile_distinct_count_of_a_field_no_document_fills(adapter: MongoAdapter):
    """`$count` emits no stage at all over an empty result."""
    _answer(adapter, [])

    assert (
        adapter.profile_column(
            "users", "email", ProfileMode.DISTINCT_COUNT
        ).distinct_count
        == 0
    )


def test_profile_top_values(adapter: MongoAdapter):
    collection = _answer(adapter, [{"_id": "a@x.com", "c": 2}, {"_id": None, "c": 1}])

    result = adapter.profile_column("users", "email", ProfileMode.TOP_VALUES)

    assert result.top_values is not None
    assert [(v.value, v.count) for v in result.top_values] == [("a@x.com", 2), ("", 1)]
    assert collection.pipelines[-1][-1] == {"$limit": 20}


def test_profile_of_an_unknown_container(adapter: MongoAdapter):
    with pytest.raises(UnknownContainerError):
        adapter.profile_column("absent", "email", ProfileMode.NULL_RATIO)


def test_profile_of_a_field_the_schema_never_saw(adapter: MongoAdapter):
    """Profiling it as wholly null would read as a fact about the collection,
    when all it says is that the sample missed it."""
    with pytest.raises(UnknownColumnError, match="users.nickname"):
        adapter.profile_column("users", "nickname", ProfileMode.NULL_RATIO)


def test_an_unsupported_mode_is_refused(adapter: MongoAdapter):
    with pytest.raises(ValueError, match="unsupported profile mode"):
        adapter.profile_column("users", "email", "median")  # type: ignore[arg-type]


# ---- sample ----


def test_get_sample_serialises_what_json_cannot_hold(adapter: MongoAdapter):
    rows = adapter.get_sample("users", limit=1)

    assert rows == [{"_id": "65f000000000000000000001", "email": "a@x.com", "age": 30}]


def test_get_sample_caps_the_limit():
    adapter = build(max_sample_limit=1)

    assert len(adapter.get_sample("users", limit=100)) == 1


def test_get_sample_of_an_unknown_container(adapter: MongoAdapter):
    with pytest.raises(UnknownContainerError):
        adapter.get_sample("absent")


# ---- audit and liveness ----


def test_what_reached_the_source_is_recorded(adapter: MongoAdapter):
    adapter.pop_rendered_sql()

    adapter.get_sample("users", limit=2)

    assert "db.users.find().limit(2)" in (adapter.pop_rendered_sql() or "")


def test_a_pipeline_is_recorded_as_what_ran(adapter: MongoAdapter):
    _answer(adapter, [{"n": 1}])
    adapter.pop_rendered_sql()

    adapter.profile_column("users", "email", ProfileMode.DISTINCT_COUNT)

    assert "db.users.aggregate(" in (adapter.pop_rendered_sql() or "")


def test_ping(adapter: MongoAdapter):
    assert adapter.ping() is True


def test_ping_does_not_pollute_the_audit_trail(adapter: MongoAdapter):
    adapter.pop_rendered_sql()
    adapter.ping()

    assert adapter.pop_rendered_sql() is None


def test_ping_after_close(adapter: MongoAdapter):
    adapter.close()

    assert adapter.ping() is False
    assert adapter._client.closed is True
