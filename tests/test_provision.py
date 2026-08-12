"""
The generated client bundle.

`docker/provision.py` is a script rather than a package module, so it is loaded
by path here — the same way the entrypoint runs it.

The frontmatter tests are the point of this file. A SKILL.md whose frontmatter
does not parse is not a skill with a missing description: it is a file the
client cannot load at all, and nothing in this project would notice, because
nothing in this project reads YAML.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROVISION = ROOT / "docker" / "provision.py"
TEMPLATES = ROOT / "docker" / "templates"
LANGUAGES = sorted(entry.name for entry in TEMPLATES.iterdir() if entry.is_dir())


def _load():
    spec = importlib.util.spec_from_file_location("provision_under_test", PROVISION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


provision = _load()


def frontmatter(text: str) -> dict:
    """What a client parses out of the top of a SKILL.md."""
    assert text.startswith("---\n"), "no frontmatter at all"
    _, block, _ = text.split("---", 2)
    return yaml.safe_load(block)


# ---- the frontmatter has to parse ----


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_description_survives_a_yaml_parser(lang: str):
    """
    The failure this guards against: a plain YAML scalar may not contain a
    colon followed by a space, and a description is a sentence, so eventually
    it does. Unquoted, the parser reads the rest as a nested mapping and the
    whole document fails.
    """
    phrases, _ = provision.load_phrases(lang)
    raw = phrases["skill"]["description"].format(
        server_name="etl-agent-mcp", engine_display="SQL Server"
    )
    document = (
        f"---\nname: etl-agent-mcp\ndescription: {provision.yaml_scalar(raw)}\n---\n"
    )

    parsed = frontmatter(document)
    assert parsed["name"] == "etl-agent-mcp"
    assert parsed["description"] == " ".join(raw.split())


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_shipped_description_actually_contains_the_hazard(lang: str):
    """
    Otherwise the test above passes for the wrong reason. Both descriptions
    name the tools and the data, so both contain `: ` — if one stops, the
    quoting is still right but this file is no longer testing it.
    """
    phrases, _ = provision.load_phrases(lang)

    assert ": " in phrases["skill"]["description"]


@pytest.mark.parametrize(
    "hazard",
    [
        'a "quoted" phrase',
        "a colon: and a space",
        "a backslash \\ and a brace }",
        "a # hash and a - dash",
        "  leading and trailing  ",
        "a\nnewline",
    ],
)
def test_awkward_text_still_parses_back_unchanged(hazard: str):
    document = f"---\ndescription: {provision.yaml_scalar(hazard)}\n---\n"

    assert frontmatter(document)["description"] == " ".join(hazard.split())


def test_the_quoted_form_is_a_json_string():
    """Which is what makes it safe: a JSON string is a YAML double-quoted
    scalar, escapes and all."""
    quoted = provision.yaml_scalar('say "hi": now')

    assert json.loads(quoted) == 'say "hi": now'


def test_non_ascii_is_not_escaped_into_unreadability():
    """The zh-TW description is mostly Chinese; `\\u67e5` for every character
    would make the generated file unreadable to the person reviewing it."""
    quoted = provision.yaml_scalar("查詢資料目錄")

    assert "查詢資料目錄" in quoted


# ---- every language pack has to be complete ----


@pytest.mark.parametrize("lang", LANGUAGES)
def test_a_language_pack_has_every_key_the_generator_asks_for(lang: str):
    phrases, directory = provision.load_phrases(lang)

    for section, keys in (
        ("skill", ("description", "role_line", "role_line_plain", "expiry")),
        ("catalog", ("head", "empty", "closing", "closing_export", "routes")),
        (
            "tools",
            ("heading", "catalog", "inventory", "withheld_heading", "withheld_body"),
        ),
        (
            "limits",
            (
                "heading",
                "databases_all",
                "databases_only",
                "containers_none",
                "containers_allow",
                "containers_deny",
                "raw_granted",
                "raw_denied",
                "human",
            ),
        ),
        (
            "install",
            (
                "title",
                "claude_code",
                "codex",
                "credential_inline",
                "credential_env",
                "credential_stdio",
                "role_note",
            ),
        ),
    ):
        missing = [key for key in keys if key not in phrases.get(section, {})]
        assert not missing, f"{lang}/phrases.toml [{section}] is missing {missing}"

    for _, filename in provision.CONDITIONAL_SECTIONS:
        assert (directory / filename).is_file(), f"{lang} has no {filename}"
    assert (directory / "SKILL.md.tmpl").is_file()


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_routes_name_real_tools(lang: str):
    """A route pointing at a tool that does not exist would simply never be
    shown, which is the kind of nothing that goes unnoticed."""
    from src.core.tools import TOOL_NAMES

    phrases, _ = provision.load_phrases(lang)

    for route in phrases["catalog"]["routes"]:
        assert route["tool"] in TOOL_NAMES, f"{lang}: no tool {route['tool']}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_translated_tool_summaries_name_real_tools(lang: str):
    from src.core.tools import TOOL_NAMES

    phrases, _ = provision.load_phrases(lang)

    for name in phrases["tools"].get("summaries", {}):
        assert name in TOOL_NAMES, f"{lang}: no tool {name}"


def test_an_unknown_language_is_refused_rather_than_falling_back():
    """A deployment that set PROVISION_LANG=zh and silently got English would
    look like the setting did nothing."""
    with pytest.raises(provision.ProvisionError, match="no templates"):
        provision.load_phrases("zh")


# ---- the whole document ----


@pytest.mark.parametrize("lang", LANGUAGES)
def test_a_rendered_skill_parses_end_to_end(
    lang: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The real thing: sign a token, generate a bundle, parse what came out."""
    import main as entry
    from src.core.roles import ENV_ROLES_FILE

    monkeypatch.setenv(ENV_ROLES_FILE, str(ROOT / "docker" / "roles.toml"))
    keys, private = tmp_path / "keys", tmp_path / "de.pem"
    entry.main(
        [
            "token",
            "keygen",
            "--kid",
            "de",
            "--keys-dir",
            str(keys),
            "--out",
            str(private),
        ]
    )
    token_file = tmp_path / "out" / "de" / "de.jwt"
    entry.main(
        [
            "token",
            "issue",
            "--key",
            str(private),
            "--kid",
            "de",
            "--role",
            "de",
            "--out",
            str(token_file),
        ]
    )

    for name, value in (
        ("PROVISION_KID", "de"),
        ("PROVISION_ROLE", "de"),
        ("PROVISION_TOKEN_FILE", str(token_file)),
        ("MCP_OUT_DIR", str(tmp_path / "out")),
        ("PROVISION_LANG", lang),
        # Pinned, not inherited: the slug below is derived from it, and
        # `provision` reads the repo's own .env — so a developer whose .env
        # names their server fails this test on an unrelated setting.
        ("MCP_SERVER_NAME", "etl-agent-mcp"),
        ("MCP_ENGINE", "mssql"),
        ("MCP_TRANSPORT", "streamable-http"),
        ("MCP_STAGING_DB", "/data/staging.db"),
        ("MCP_EXPORT_DIR", "/data/export"),
    ):
        monkeypatch.setenv(name, value)

    assert provision.main() == 0

    skill = (
        tmp_path / "out" / "de" / "skills" / "etl-agent-mcp" / "SKILL.md"
    ).read_text(encoding="utf-8")
    parsed = frontmatter(skill)

    assert parsed["name"] == "etl-agent-mcp"
    assert isinstance(parsed["description"], str)
    assert parsed["description"].strip()
    # The engine's display name, not the flag spelling, because a person reads it
    assert "SQL Server" in parsed["description"]
    # Tool names are identifiers and are never translated
    assert "`inventory_export`" in skill
