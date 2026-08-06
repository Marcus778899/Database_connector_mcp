import csv
import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.core.contracts import ColumnInfo, ContainerInfo, ContainerType
from src.service.export import ExportError, export_inventory, resolve_target
from src.service.staging import ColumnAnnotation, StagingStore


def _column(name: str, ordinal: int = 1, native_type: str = "TEXT", **kwargs):
    return ColumnInfo(
        name=name,
        ordinal=ordinal,
        native_type=native_type,
        nullable=kwargs.get("nullable", True),
        is_pk=kwargs.get("is_pk", False),
        is_fk=kwargs.get("is_fk", False),
        native_description=kwargs.get("native_description"),
        references_container=kwargs.get("references_container"),
        references_column=kwargs.get("references_column"),
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[StagingStore]:
    with StagingStore(tmp_path / "staging.db") as opened:
        opened.upsert_container(
            ContainerInfo(
                database="main",
                container_name="users",
                container_type=ContainerType.TABLE,
                estimated_count=1200,
            ),
            hash_="h",
        )
        opened.replace_columns(
            "main",
            None,
            "users",
            [
                _column("id", 1, "INTEGER", is_pk=True, nullable=False),
                _column("email", 2, "TEXT"),
            ],
        )
        opened.upsert_container(
            ContainerInfo(
                database="main",
                container_name="orders",
                container_type=ContainerType.TABLE,
            ),
            hash_="h",
        )
        opened.replace_columns(
            "main",
            None,
            "orders",
            [
                _column("id", 1, "INTEGER", is_pk=True),
                _column(
                    "user_id",
                    2,
                    "INTEGER",
                    is_fk=True,
                    references_container="users",
                    references_column="id",
                ),
            ],
        )
        opened.annotate(
            "main",
            "users",
            container_description="everyone who signed up",
            columns=[ColumnAnnotation(column="email", description="login address")],
        )
        yield opened


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "exports"
    directory.mkdir()
    return directory


# ---- where it may write ----


def test_the_default_target_is_inside_the_export_directory(export_dir: Path):
    assert resolve_target(export_dir, None, "markdown").parent == export_dir.resolve()


@pytest.mark.parametrize(
    "suffix", [("markdown", ".md"), ("csv", ".csv"), ("dbt_yaml", ".yml")]
)
def test_the_default_name_follows_the_format(export_dir: Path, suffix):
    fmt, extension = suffix
    assert resolve_target(export_dir, None, fmt).suffix == extension


def test_a_relative_path_lands_under_the_export_directory(export_dir: Path):
    target = resolve_target(export_dir, "sub/dir/catalog.md", "markdown")

    assert target.is_relative_to(export_dir.resolve())


@pytest.mark.parametrize(
    "path",
    ["../escaped.md", "../../etc/passwd", "sub/../../escaped.md"],
)
def test_a_path_climbing_out_is_refused(export_dir: Path, path: str):
    """The caller is an agent relaying a path it was given; this is the only
    layer that can tell where it points."""
    with pytest.raises(ExportError, match="outside the export directory"):
        resolve_target(export_dir, path, "markdown")


def test_an_absolute_path_elsewhere_is_refused(export_dir: Path, tmp_path: Path):
    with pytest.raises(ExportError, match="outside the export directory"):
        resolve_target(export_dir, str(tmp_path / "elsewhere.md"), "markdown")


def test_an_absolute_path_inside_is_allowed(export_dir: Path):
    target = resolve_target(export_dir, str(export_dir / "fine.md"), "markdown")

    assert target == (export_dir / "fine.md").resolve()


def test_the_export_directory_itself_is_not_a_file(export_dir: Path):
    with pytest.raises(ExportError, match="name the file"):
        resolve_target(export_dir, ".", "markdown")


def test_a_symlink_pointing_out_is_refused(export_dir: Path, tmp_path: Path):
    """`resolve()` before the check, so a link cannot be the way out."""
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (export_dir / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this platform will not let us make a symlink")

    with pytest.raises(ExportError, match="outside the export directory"):
        resolve_target(export_dir, "link/escaped.md", "markdown")


# ---- what comes back ----


def test_an_export_returns_where_it_went_and_not_what_is_in_it(
    store: StagingStore, export_dir: Path
):
    """The core of the context control: a full sweep must not travel through a
    tool result."""
    result = export_inventory(store, export_dir)

    assert Path(result.path).exists()
    assert result.containers == 2
    assert result.columns == 4
    assert result.bytes_written == Path(result.path).stat().st_size
    assert not hasattr(result, "content")
    assert "email" not in result.model_dump_json()


def test_the_export_can_be_scoped_to_one_database(
    store: StagingStore, export_dir: Path
):
    store.upsert_container(
        ContainerInfo(
            database="other",
            container_name="elsewhere",
            container_type=ContainerType.TABLE,
        ),
        hash_="h",
    )
    store.replace_columns("other", None, "elsewhere", [_column("id")])

    scoped = export_inventory(store, export_dir, database="main", path="main.md")

    assert scoped.containers == 2
    assert "elsewhere" not in Path(scoped.path).read_text(encoding="utf-8")


# ---- markdown ----


def test_markdown_is_a_data_dictionary(store: StagingStore, export_dir: Path):
    result = export_inventory(store, export_dir, format="markdown")

    written = Path(result.path).read_text(encoding="utf-8")
    assert "## users" in written
    assert "everyone who signed up" in written
    assert "login address" in written
    assert "`email`" in written
    assert "~1,200 rows" in written


def test_markdown_says_what_a_foreign_key_points_at(
    store: StagingStore, export_dir: Path
):
    result = export_inventory(store, export_dir, format="markdown")

    assert "FK → users.id" in Path(result.path).read_text(encoding="utf-8")


def test_a_pipe_in_a_description_cannot_break_the_table(
    store: StagingStore, export_dir: Path
):
    store.annotate(
        "main",
        "users",
        columns=[ColumnAnnotation(column="id", description="a | b\nand a newline")],
    )

    result = export_inventory(store, export_dir, format="markdown")

    rows = [
        line
        for line in Path(result.path).read_text(encoding="utf-8").splitlines()
        if "and a newline" in line
    ]
    assert len(rows) == 1, "the description belongs to exactly one row"
    # cell boundaries are the *unescaped* pipes; five columns means six of them
    assert rows[0].replace("\\|", "").count("|") == 6, rows[0]
    assert "a \\| b" in rows[0]


def test_a_container_that_could_not_be_read_says_so(
    store: StagingStore, export_dir: Path
):
    store.upsert_container(
        ContainerInfo(
            database="main",
            container_name="locked",
            container_type=ContainerType.TABLE,
        ),
        hash_="",
        error="PermissionError: denied",
    )

    result = export_inventory(store, export_dir, format="markdown")

    written = Path(result.path).read_text(encoding="utf-8")
    assert "## locked" in written
    assert "Not readable at the last scan" in written


# ---- csv ----


def test_csv_is_one_row_per_column(store: StagingStore, export_dir: Path):
    result = export_inventory(store, export_dir, format="csv")

    rows = list(csv.DictReader(io.StringIO(Path(result.path).read_text("utf-8"))))
    assert len(rows) == 4
    assert {row["container"] for row in rows} == {"users", "orders"}
    email = next(row for row in rows if row["column"] == "email")
    assert email["description"] == "login address"
    assert email["description_source"] == "ai"


def test_csv_records_the_foreign_key_target(store: StagingStore, export_dir: Path):
    result = export_inventory(store, export_dir, format="csv")

    rows = list(csv.DictReader(io.StringIO(Path(result.path).read_text("utf-8"))))
    user_id = next(row for row in rows if row["column"] == "user_id")
    assert user_id["references"] == "users.id"


def test_a_comma_in_a_description_does_not_shift_the_columns(
    store: StagingStore, export_dir: Path
):
    store.annotate(
        "main",
        "users",
        columns=[ColumnAnnotation(column="id", description='a, b and a "quote"')],
    )

    result = export_inventory(store, export_dir, format="csv")

    rows = list(csv.DictReader(io.StringIO(Path(result.path).read_text("utf-8"))))
    row = next(r for r in rows if r["container"] == "users" and r["column"] == "id")
    assert row["description"] == 'a, b and a "quote"'
    assert row["native_type"] == "INTEGER"


# ---- dbt ----


def test_dbt_yaml_is_a_schema_file(store: StagingStore, export_dir: Path):
    result = export_inventory(store, export_dir, format="dbt_yaml")

    written = Path(result.path).read_text(encoding="utf-8")
    assert written.startswith("version: 2")
    assert '- name: "users"' in written
    assert 'description: "everyone who signed up"' in written
    assert '- name: "email"' in written


def test_a_description_cannot_restructure_the_yaml(
    store: StagingStore, export_dir: Path
):
    """Hand-written YAML is a liability exactly here: a colon or a newline in a
    description would otherwise end the scalar and start something else."""
    store.annotate(
        "main",
        "users",
        columns=[
            ColumnAnnotation(
                column="id", description="key: value\n      - name: injected"
            )
        ],
    )

    result = export_inventory(store, export_dir, format="dbt_yaml")

    written = Path(result.path).read_text(encoding="utf-8")
    # the text is in there — escaped, inside the scalar, and not a line of YAML
    assert "- name: injected" in written
    assert not any(
        line.strip() == "- name: injected" for line in written.splitlines()
    ), "the description opened a structure of its own"
    for line in written.splitlines():
        if line.strip().startswith("description:"):
            json.loads(line.split("description:", 1)[1].strip())


def test_a_container_with_no_columns_is_left_out_of_dbt(
    store: StagingStore, export_dir: Path
):
    """dbt cannot use a model with no columns."""
    store.upsert_container(
        ContainerInfo(
            database="main",
            container_name="empty",
            container_type=ContainerType.TABLE,
        ),
        hash_="h",
    )

    result = export_inventory(store, export_dir, format="dbt_yaml")

    assert '"empty"' not in Path(result.path).read_text(encoding="utf-8")


# ---- size ----


def test_a_wide_catalog_is_written_without_being_held_in_memory(
    store: StagingStore, export_dir: Path
):
    """Paged both ways: the export walks containers and their columns a page at
    a time, so its cost does not grow with the catalog."""
    for index in range(250):
        store.upsert_container(
            ContainerInfo(
                database="main",
                container_name=f"t{index:04d}",
                container_type=ContainerType.TABLE,
            ),
            hash_="h",
        )
        store.replace_columns(
            "main",
            None,
            f"t{index:04d}",
            [_column(f"c{n:02d}", n) for n in range(1, 21)],
        )

    result = export_inventory(store, export_dir, format="csv")

    assert result.containers == 252
    assert result.columns == 5004
