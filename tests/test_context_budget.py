"""
What the inventory tools cost a caller's context, measured on a catalog too big
to read.

The budgets below are the point of Phase 2. They are asserted rather than
described because "keep the payloads small" is not a property anyone can hold
in their head while adding a field to a model — a number that fails the build
is.

Sizes are the actual serialised tool results, taken through a client, not
estimates from the models.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import Client

from src.core.config import ServerConfig, SourceEngine
from src.core.contracts import (
    ColumnInfo,
    ContainerInfo,
    ContainerType,
    ProfileMode,
    ProfileResult,
    TopValue,
)
from src.server import build_server
from src.service.inventory import InventoryService
from src.service.pool import SingleAdapter
from src.service.staging import StagingStore

CONTAINERS = 500
COLUMNS = 30

# What a tool result may cost, in bytes of JSON.
#
# The roadmap guessed 10KB for a search before any of this was built. Measured
# against a catalog whose columns carry real descriptions it comes to ~250
# bytes a hit, so fifty of them is ~12.5KB: the guess was optimistic about how
# much a useful hit weighs, not wrong about the shape. The number here is the
# measurement plus room to move, and it is asserted so that a field added to
# SearchHit has to be argued for.
SUMMARY_BUDGET = 1_024
SEARCH_BUDGET = 16 * 1024
COLUMNS_BUDGET = 40 * 1024


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> Iterator[StagingStore]:
    """500 containers of 30 columns, some profiled — a catalog nobody could
    read a page at a time."""
    path = tmp_path_factory.mktemp("budget") / "staging.db"
    with StagingStore(path) as opened:
        for index in range(CONTAINERS):
            name = f"table_{index:04d}"
            opened.upsert_container(
                ContainerInfo(
                    database="main",
                    container_name=name,
                    container_type=ContainerType.TABLE,
                    estimated_count=index * 1000,
                    native_description=f"one row per thing in {name}",
                ),
                hash_="h",
            )
            opened.replace_columns(
                "main",
                None,
                name,
                [
                    ColumnInfo(
                        name=f"column_{ordinal:02d}_email",
                        ordinal=ordinal,
                        native_type="VARCHAR(255)",
                        nullable=True,
                        is_pk=ordinal == 1,
                        is_fk=False,
                        native_description="a reasonably wordy description of "
                        "what this column holds, as a real catalog would carry",
                    )
                    for ordinal in range(1, COLUMNS + 1)
                ],
            )
        # profiles on one container, so include_profile has something to drop
        for ordinal in range(1, COLUMNS + 1):
            opened.record_profile(
                "main",
                None,
                "table_0000",
                f"column_{ordinal:02d}_email",
                ProfileMode.TOP_VALUES,
                ProfileResult(
                    top_values=[
                        TopValue(value=f"value-{n}", count=n) for n in range(20)
                    ]
                ),
            )
        yield opened


@pytest.fixture(scope="module")
def served(store: StagingStore, tmp_path_factory):
    """The tools over that catalog. No adapter is touched: every tool here
    reads staging."""
    config = ServerConfig(
        server_name="budget", engine=SourceEngine.SQLITE, max_sample_limit=1
    )

    class _Unused:
        """
        The live tools need a source; none of these tests call them, and the
        trap says so out loud — a budget that quietly measured a live read
        would be measuring the wrong thing.
        """

        def list_databases(self) -> list[str]:
            return ["main"]

        def close(self) -> None:
            """Shutdown, not a read."""

        def ping(self) -> bool:
            return True

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"a budget test read from the source: {name}")

    service = InventoryService(SingleAdapter(_Unused()), store)  # type: ignore[arg-type]
    return build_server(config, _Unused(), inventory=service)  # type: ignore[arg-type]


def _payload(mcp, tool: str, args: dict[str, Any] | None = None) -> tuple[int, Any]:
    """The bytes a caller actually receives, and the parsed result."""

    async def run() -> tuple[int, Any]:
        async with Client(mcp) as client:
            result = await client.call_tool(tool, args or {})
            size = sum(
                len(getattr(block, "text", "").encode("utf-8"))
                for block in result.content
            )
            return size, result.data

    return asyncio.run(run())


def test_the_summary_of_a_huge_catalog_is_a_few_hundred_bytes(served):
    """The call an agent is told to make first. It has to stay cheap enough
    that making it first is never a bad trade."""
    size, summary = _payload(served, "inventory_summary", {"database": "main"})

    assert summary.containers == CONTAINERS
    assert summary.columns == CONTAINERS * COLUMNS
    assert size < SUMMARY_BUDGET, f"{size} bytes"


def test_a_search_over_a_huge_catalog_stays_small(served):
    """15,000 columns match this keyword; the limit is what keeps the answer
    readable."""
    size, hits = _payload(
        served, "inventory_search", {"keyword": "email", "database": "main"}
    )

    assert len(hits) == 50  # the default limit
    assert size < SEARCH_BUDGET, f"{size} bytes"
    # against the alternative it replaces: reading the catalog to find these
    assert size < CONTAINERS * COLUMNS


def test_a_search_hit_carries_no_statistics(served):
    """Profiles are what would make a hundred hits expensive."""
    _, hits = _payload(served, "inventory_search", {"keyword": "column_01", "limit": 5})

    assert hits
    assert all(not hasattr(hit, "profile") for hit in hits)


def test_one_page_of_columns_is_bounded_even_with_profiles(served):
    size, page = _payload(
        served,
        "inventory_columns",
        {"container": "table_0000", "database": "main"},
    )

    assert len(page.columns) == COLUMNS
    assert size < COLUMNS_BUDGET, f"{size} bytes"


def test_dropping_the_profiles_is_the_escape_hatch_it_is_meant_to_be(served):
    """The whole reason `include_profile` exists: on a wide profiled table the
    statistics are most of the payload."""
    with_profile, _ = _payload(
        served,
        "inventory_columns",
        {"container": "table_0000", "database": "main"},
    )
    without, page = _payload(
        served,
        "inventory_columns",
        {
            "container": "table_0000",
            "database": "main",
            "include_profile": False,
        },
    )

    assert all(column.profile is None for column in page.columns)
    assert without < with_profile / 2, f"{without} vs {with_profile}"


def test_a_page_of_containers_is_bounded_by_its_limit(served):
    size, page = _payload(served, "inventory_containers", {"limit": 20})

    assert len(page.containers) == 20
    assert page.next_cursor is not None
    assert size < COLUMNS_BUDGET, f"{size} bytes"


def test_reading_the_whole_catalog_through_the_tools_is_not_the_cheap_path(served):
    """The measurement the export tool exists because of: paging the catalog
    into a context window costs two orders of magnitude more than a summary,
    which is why a full sweep is delivered as a file."""
    summary, _ = _payload(served, "inventory_summary", {"database": "main"})
    page, _ = _payload(served, "inventory_containers", {"limit": 100})

    assert page > summary * 10
