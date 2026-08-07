from __future__ import annotations

import datetime as dt
import re
from typing import Any, ClassVar

from bson import Binary, Code, Decimal128, ObjectId, Regex, Timestamp
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import ConfigurationError, PyMongoError

from src.adapter.base import (
    AdapterBase,
    UnknownColumnError,
    UnknownContainerError,
)
from src.core.config import ConnectionInfo
from src.core.contracts import (
    ColumnInfo,
    ContainerInfo,
    ContainerPage,
    ContainerType,
    ProfileMode,
    ProfileResult,
    TopValue,
)
from src.core.log import log
from src.utils.serialize import as_text, jsonify

# The server's own databases. Never a user's catalog.
_SYSTEM_DATABASES = frozenset({"admin", "local", "config"})

# int32 is the boundary mongo itself draws between `int` and `long`, and python
# has one integer type, so the width has to be worked out from the value.
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1

# BSON type names, as `$type` reports them. Grouped by the statistics that mean
# something for them, which is not what the inherited type-name matching would
# make of `long` or `objectId`.
_NUMERIC_TYPES = frozenset({"int", "long", "double", "decimal"})
_TEMPORAL_TYPES = frozenset({"date", "timestamp"})
_CATEGORICAL_TYPES = frozenset({"string", "bool"})


class MongoAdapter(AdapterBase):
    """
    SourceAdapter over one mongo database.

    A collection has no declared schema, so this one is **inferred** from the
    first `schema_sample` documents: the fields they carry, and the BSON types
    each was seen holding. That makes it a description of the sample rather than
    a guarantee about the collection, which is why a field seen holding more
    than one type is reported as all of them (`string|int`) rather than as
    whichever came first.

    Only top-level fields. A nested document is reported as `object` and an
    array as `array`, because flattening `a.b.c` turns one collection into an
    unbounded list of paths — and the paths would still only describe the sample.
    """

    DEFAULT_PORT: ClassVar[int] = 27017
    # How many documents a schema is inferred from. Enough for the fields that
    # matter to appear, few enough that reading a schema is not reading the data.
    DEFAULT_SCHEMA_SAMPLE: ClassVar[int] = 100
    # A server that is not there should say so rather than hang a tool call.
    DEFAULT_SERVER_TIMEOUT_MS: ClassVar[int] = 5_000

    def __init__(
        self,
        client: MongoClient[dict[str, Any]],
        *,
        database: str,
        max_sample_limit: int | None = None,
        schema_sample: int | None = None,
    ) -> None:
        if not database:
            raise ValueError(
                "The mongodb connection requires a <REF>_DB, or a database in the "
                "<REF>_URI path: there is no catalog without one."
            )
        super().__init__(database=database, max_sample_limit=max_sample_limit)
        self._client = client
        self._schema_sample = schema_sample or self.DEFAULT_SCHEMA_SAMPLE
        self._closed = False
        log.info(f"opened mongodb {database}")

    @classmethod
    def from_connection(
        cls,
        conn_info: ConnectionInfo,
        *,
        database: str | None = None,
        max_sample_limit: int | None = None,
    ) -> MongoAdapter:
        if conn_info.uri:
            client: MongoClient[dict[str, Any]] = MongoClient(
                conn_info.uri,
                serverSelectionTimeoutMS=cls.DEFAULT_SERVER_TIMEOUT_MS,
            )
            name = database or conn_info.database or _database_in_uri(client)
        elif conn_info.host:
            client = MongoClient(
                host=conn_info.host,
                port=conn_info.port or cls.DEFAULT_PORT,
                username=conn_info.user,
                password=conn_info.password,
                serverSelectionTimeoutMS=cls.DEFAULT_SERVER_TIMEOUT_MS,
            )
            name = database or conn_info.database or ""
        else:
            raise ValueError(
                "The mongodb connection requires a <REF>_HOST, or a <REF>_URI "
                "(mongodb://…)."
            )
        return cls(
            client,
            database=name or "",
            max_sample_limit=max_sample_limit,
        )

    # ---- lifecycle ----

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.close()

    def ping(self) -> bool:
        """A real round-trip, so the pool rebuilds a client whose server went
        away. Not recorded: pool bookkeeping is not something a tool call
        asked for."""
        if self._closed:
            return False
        try:
            self._client.admin.command("ping")
        except Exception:  # noqa: BLE001 - any failure means unusable
            return False
        return True

    # ---- catalog ----

    @property
    def _db(self) -> Any:
        return self._client[self._database]

    def _collections(self) -> dict[str, str]:
        """Every collection and view, as name -> "collection" | "view"."""
        return {
            str(info["name"]): str(info.get("type", "collection"))
            for info in self._db.list_collections()
            # mongo's own bookkeeping, e.g. system.views
            if not str(info["name"]).startswith("system.")
        }

    def _require_collection(self, container: str) -> Collection[dict[str, Any]]:
        if container not in self._known_containers():
            raise UnknownContainerError(container)
        return self._db[container]

    def _require_field(self, container: str, field: str) -> str:
        """
        A field the inferred schema knows about.

        A field that no sampled document carried is refused rather than profiled
        as wholly null — the second reads as a fact about the collection, when
        all it says is that the sample missed it.
        """
        names = {column.name for column in self._cached_schema(container)}
        if field not in names:
            raise UnknownColumnError(
                f"{container}.{field} (no field of that name in the "
                f"{self._schema_sample} documents the schema was inferred from)"
            )
        return field

    def _walk_containers(self) -> frozenset[str]:
        """Names only. The inherited walk pages through `list_containers`,
        counting every collection on the way."""
        self._record_sql(f"db.getCollectionNames()  // {self._database}")
        return frozenset(self._collections())

    # 4 tools (READ ONLY)

    def list_databases(self) -> list[str]:
        """
        Every database on this server, minus its own.

        A login without `listDatabases` gets the one it was pointed at, which is
        the one it can actually read — better than failing the call over a
        privilege the rest of the tools do not need.
        """
        self._record_sql("db.adminCommand({listDatabases: 1})")
        try:
            names = self._client.list_database_names()
        except PyMongoError as exc:
            log.info(f"cannot list databases ({exc}); reporting {self._database!r}")
            return [self._database]
        return [name for name in sorted(names) if name not in _SYSTEM_DATABASES]

    def list_containers(
        self,
        database: str | None = None,
        schema: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> ContainerPage:
        """
        One page of collections and views, ordered by name.

        Keyset, not offset: the cursor is the last name returned, so a collection
        appearing or disappearing mid-walk cannot make the caller skip or repeat
        one. Only the page's collections are counted.
        """
        if database is not None and database != self._database:
            raise UnknownContainerError(
                f"this connection serves {self._database!r}, got {database!r}"
            )
        if schema is not None:
            raise UnknownContainerError(
                f"mongodb has no schema layer, got schema={schema!r}"
            )

        self._record_sql(f"db.listCollections()  // {self._database}")
        names = sorted(self._collections().items())
        if cursor is not None:
            names = [(name, kind) for name, kind in names if name > cursor]

        page_size = self._cap_page_size(limit)
        page, has_more = names[:page_size], len(names) > page_size
        containers = [
            ContainerInfo(
                database=self._database,
                schema_name=None,
                container_name=name,
                container_type=(
                    ContainerType.VIEW if kind == "view" else ContainerType.COLLECTION
                ),
                # From the collection's metadata, not a count of its documents —
                # and a view has none, since counting one would mean running it.
                estimated_count=(self._estimate(name) if kind != "view" else None),
            )
            for name, kind in page
        ]
        return ContainerPage(
            containers=containers,
            next_cursor=page[-1][0] if has_more else None,
        )

    def get_schema(self, container: str) -> list[ColumnInfo]:
        collection = self._require_collection(container)
        self._record_sql(f"db.{container}.find().limit({self._schema_sample})")

        seen: dict[str, set[str]] = {}
        present: dict[str, int] = {}
        total = 0
        for document in collection.find(limit=self._schema_sample):
            total += 1
            for field, value in document.items():
                types = seen.setdefault(field, set())
                if value is None:
                    continue
                types.add(_bson_type(value))
                present[field] = present.get(field, 0) + 1

        return [
            ColumnInfo(
                name=field,
                # First-seen order: mongo documents keep the order their fields
                # were written in, and there is no other order to report.
                ordinal=ordinal,
                # Every type the field was seen holding. A field the sample only
                # ever saw null has no type to report at all.
                native_type="|".join(sorted(types)),
                # Absent counts as null: to anything reading this catalog, a
                # field that is not there and a field set to null are the same
                # missing value.
                nullable=present.get(field, 0) < total,
                is_pk=field == "_id",
                # mongo declares no references, and guessing one from a field
                # name would put a join in the catalog that nothing enforces
                is_fk=False,
            )
            for ordinal, (field, types) in enumerate(seen.items(), start=1)
        ]

    def get_sample(
        self, container: str, limit: int = AdapterBase._DEFAULT_SAMPLE_LIMIT
    ) -> list[dict[str, Any]]:
        collection = self._require_collection(container)
        capped = self._cap_limit(limit)
        self._record_sql(f"db.{container}.find().limit({capped})")
        return [
            {str(key): jsonify(value) for key, value in document.items()}
            for document in collection.find(limit=capped)
        ]

    def profile_column(
        self, container: str, column: str, mode: ProfileMode
    ) -> ProfileResult:
        collection = self._require_collection(container)
        field = self._require_field(container, column)

        if mode == ProfileMode.NULL_RATIO:
            return self._profile_null_ratio(collection, field)
        if mode == ProfileMode.MIN_MAX:
            return self._profile_min_max(collection, field)
        if mode == ProfileMode.DISTINCT_COUNT:
            return self._profile_distinct_count(collection, field)
        if mode == ProfileMode.TOP_VALUES:
            return self._profile_top_values(collection, field)

        raise ValueError(f"unsupported profile mode: {mode!r}")

    def default_profile_modes(self, column: ColumnInfo) -> tuple[ProfileMode, ...]:
        """
        Which statistics suit a BSON type.

        The inherited version matches substrings of a sql type name, which reads
        `long` as nothing and `objectId` as nothing either. Here the names are a
        closed set, so they can simply be looked up.

        A field of more than one type gets only a null ratio: a range over
        values that are sometimes text and sometimes numbers compares them by
        BSON type order, which describes the encoding rather than the data.
        """
        native = column.native_type
        if not native or "|" in native:
            return (ProfileMode.NULL_RATIO,)
        if native in _NUMERIC_TYPES or native in _TEMPORAL_TYPES:
            return (ProfileMode.NULL_RATIO, ProfileMode.MIN_MAX)
        if native in _CATEGORICAL_TYPES:
            return (
                ProfileMode.NULL_RATIO,
                ProfileMode.DISTINCT_COUNT,
                ProfileMode.TOP_VALUES,
            )
        return (ProfileMode.NULL_RATIO,)

    # ---- profiling ----

    def _estimate(self, name: str) -> int | None:
        """The count mongo keeps in the collection's metadata. Unreadable is an
        unknown count, not a failed listing."""
        try:
            return int(self._db[name].estimated_document_count())
        except PyMongoError as exc:
            log.warning(f"cannot count {name!r}: {exc}")
            return None

    def _aggregate(
        self, collection: Collection[dict[str, Any]], pipeline: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self._record_sql(f"db.{collection.name}.aggregate({pipeline})")
        # A group over a large collection can outgrow the 100MB stage limit, and
        # spilling is slower than failing but answers the question.
        return list(collection.aggregate(pipeline, allowDiskUse=True))

    def _profile_null_ratio(
        self, collection: Collection[dict[str, Any]], field: str
    ) -> ProfileResult:
        rows = self._aggregate(
            collection,
            [
                {
                    "$group": {
                        "_id": None,
                        "total": {"$sum": 1},
                        "nulls": {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$in": [
                                            {"$type": f"${field}"},
                                            ["missing", "null"],
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        },
                    }
                }
            ],
        )
        # No group at all means no documents: an empty collection has no ratio
        # to report, which is not zero.
        if not rows or not rows[0]["total"]:
            return ProfileResult(null_ratio=None)
        return ProfileResult(null_ratio=rows[0]["nulls"] / rows[0]["total"])

    def _profile_min_max(
        self, collection: Collection[dict[str, Any]], field: str
    ) -> ProfileResult:
        rows = self._aggregate(
            collection,
            [
                {
                    "$group": {
                        "_id": None,
                        "lo": {"$min": f"${field}"},
                        "hi": {"$max": f"${field}"},
                    }
                }
            ],
        )
        if not rows:
            return ProfileResult()
        return ProfileResult(
            min_value=as_text(rows[0]["lo"]),
            max_value=as_text(rows[0]["hi"]),
        )

    def _profile_distinct_count(
        self, collection: Collection[dict[str, Any]], field: str
    ) -> ProfileResult:
        """Grouped rather than `distinct()`: that command has to fit its whole
        answer in one 16MB reply, which a high-cardinality field will not."""
        rows = self._aggregate(
            collection,
            [
                # matching COUNT(DISTINCT x), which does not count the nulls.
                # `$ne: null` drops a missing field too.
                {"$match": {field: {"$ne": None}}},
                {"$group": {"_id": f"${field}"}},
                {"$count": "n"},
            ],
        )
        return ProfileResult(distinct_count=int(rows[0]["n"]) if rows else 0)

    def _profile_top_values(
        self, collection: Collection[dict[str, Any]], field: str
    ) -> ProfileResult:
        rows = self._aggregate(
            collection,
            [
                {"$group": {"_id": f"${field}", "c": {"$sum": 1}}},
                {"$sort": {"c": -1, "_id": 1}},
                {"$limit": self._DEFAULT_TOP_N},
            ],
        )
        return ProfileResult(
            top_values=[
                TopValue(value=as_text(row["_id"]) or "", count=int(row["c"]))
                for row in rows
            ]
        )


def _database_in_uri(client: MongoClient[dict[str, Any]]) -> str:
    """
    The database named in the connection url's path, if it named one.

    `default=None` does not stop pymongo raising when the url named none — the
    default is what it returns, not what it falls back to — so the absence has
    to be caught. It is not an error here: `<REF>_DB` is the other way to say it.
    """
    try:
        return client.get_default_database().name
    except ConfigurationError:
        return ""


def _bson_type(value: Any) -> str:
    """
    What `$type` would call this value.

    pymongo has already decoded the document, so the BSON type has to be read
    back off the python object. Two of these checks are load-bearing in their
    order: a `bool` *is* an `int` to python, and `bson.Code` *is* a `str`, so
    testing the general case first would quietly lose both distinctions — which
    are exactly what a catalog is read for.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int" if _INT32_MIN <= value <= _INT32_MAX else "long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, Decimal128):
        return "decimal"
    if isinstance(value, Code):
        return "javascript"
    if isinstance(value, str):
        return "string"
    if isinstance(value, ObjectId):
        return "objectId"
    if isinstance(value, dt.datetime):
        return "date"
    if isinstance(value, Timestamp):
        return "timestamp"
    if isinstance(value, (Binary, bytes, bytearray)):
        return "binData"
    if isinstance(value, (Regex, re.Pattern)):
        return "regex"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__
