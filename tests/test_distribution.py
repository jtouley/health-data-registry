"""The plugin, its marketplace and the Claude Projects kit: generated from helpers.py, in Anthropic's documented shapes.

Field lists come from https://claude.com/docs/plugins/build.md, https://code.claude.com/docs/en/plugins/
manifest-reference.md and marketplace-reference.md, and https://agentskills.io/specification (SKILL.md).
"""

from __future__ import annotations

import io
import json
import re
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import distribution, helpers
from tests.test_workflows import asks_for_patient_details, frontmatter

ROOT = Path(__file__).resolve().parents[1]
NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")  # kebab-case, as the manifest reference asks
MARKETPLACE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Top-level keys the manifest reference lists for plugin.json (the subset a plugin like this could use).
PLUGIN_KEYS = {
    "$schema", "name", "displayName", "version", "description", "author", "homepage", "repository", "license",
    "keywords", "metadata", "icon", "documentationUrl", "supportUrl", "privacyPolicyUrl", "termsOfServiceUrl",
    "defaultEnabled", "dependencies", "skills", "commands", "agents", "hooks", "mcpServers",
}  # fmt: skip
MARKETPLACE_KEYS = {
    "$schema", "name", "owner", "plugins", "description", "version", "metadata", "forceRemoveDeletedPlugins",
    "allowCrossMarketplaceDependenciesOn", "renames",
}  # fmt: skip
ENTRY_KEYS = PLUGIN_KEYS | {"source", "category", "tags", "strict", "relevance", "engines"}
SKILL_SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
RESERVED_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-", "claudeai-")


def load(rel: str) -> Any:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def kit() -> dict[str, bytes]:
    return distribution.kit_files(ROOT)


def test_plugin_files_match_the_generator() -> None:
    files = distribution.plugin_files(ROOT)
    for rel, text in files.items():
        path = ROOT / rel
        assert path.is_file(), f"{rel} is missing: run make docs"
        assert path.read_text(encoding="utf-8") == text, f"{rel} drifted: run make docs"
    on_disk = {
        p.relative_to(ROOT).as_posix() for p in (ROOT / distribution.PLUGIN_DIR).rglob("*") if p.is_file()
    }
    assert on_disk == {r for r in files if r.startswith(distribution.PLUGIN_DIR + "/")}, (
        "stale files: make docs"
    )


def test_settings_come_from_pyproject() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    s = distribution.settings(ROOT)
    assert s.version == data["project"]["version"]
    assert s.public_url == data["tool"]["health-data-registry"]["HDR_PUBLIC_URL"]
    assert s.public_url.startswith("https://")
    assert s.repository == "https://github.com/jtouley/health-data-registry"


def test_plugin_json_has_the_documented_fields() -> None:
    p = load("plugin/.claude-plugin/plugin.json")
    assert set(p) <= PLUGIN_KEYS
    assert p["name"] == "health-data-registry" and NAME_RE.fullmatch(p["name"])
    assert not p["name"].startswith(RESERVED_PREFIXES)
    assert p["version"] == distribution.settings(ROOT).version
    assert p["license"] == "Apache-2.0"
    assert p["repository"] == distribution.settings(ROOT).repository
    assert isinstance(p["description"], str) and p["description"]
    assert isinstance(p["author"], dict) and p["author"]["name"]
    assert all(isinstance(k, str) for k in p["keywords"])
    assert "@" not in json.dumps(p), "no email addresses"


def test_marketplace_json_is_at_the_root_and_lists_the_plugin() -> None:
    m = load(".claude-plugin/marketplace.json")
    assert set(m) <= MARKETPLACE_KEYS and {"name", "owner", "plugins"} <= set(m)
    assert MARKETPLACE_NAME_RE.fullmatch(m["name"]) and ".." not in m["name"]
    assert not m["name"].startswith(RESERVED_PREFIXES)
    assert m["owner"]["name"] and "email" not in m["owner"]
    [entry] = m["plugins"]
    assert set(entry) <= ENTRY_KEYS and {"name", "source"} <= set(entry)
    assert entry["name"] == load("plugin/.claude-plugin/plugin.json")["name"], (
        "entry and manifest names differ"
    )
    src = entry["source"]
    assert src.startswith("./") and ".." not in src
    assert (ROOT / src / ".claude-plugin/plugin.json").is_file()
    assert "version" not in entry, "plugin.json owns the version"
    assert not (ROOT / src / "bin").exists(), "a top-level bin/ stops claude.ai installing the plugin"


def test_mcp_json_has_exactly_one_http_server_at_the_setting() -> None:
    servers = load("plugin/.mcp.json")["mcpServers"]
    assert list(servers) == ["health-data-registry"]
    [server] = servers.values()
    assert server == {"type": "http", "url": distribution.settings(ROOT).public_url}


@pytest.mark.parametrize("w", helpers.SKILLS, ids=lambda w: w.name)
def test_portable_skill_frontmatter_is_valid(w: helpers.Workflow) -> None:
    text = (ROOT / f"plugin/skills/{w.name}/SKILL.md").read_text(encoding="utf-8")
    data, body = frontmatter(text)
    assert set(data) <= SKILL_SPEC_KEYS and {"name", "description"} <= set(data)
    assert data["name"] == w.name and NAME_RE.fullmatch(w.name) and len(w.name) <= 64
    desc = data["description"]
    assert isinstance(desc, str) and 0 < len(desc) <= 1024 and "<" not in desc and ">" not in desc
    assert "$ARGUMENTS" not in body, "chat doesn't fill in $ARGUMENTS"
    assert ("from my message" in body) == bool(w.args)
    assert body.rstrip().endswith(helpers.RULES)
    assert asks_for_patient_details(body) == []


def test_skill_zip_has_the_folder_at_the_top_and_is_deterministic(kit: dict[str, bytes]) -> None:
    for w in helpers.SKILLS:
        data = kit[f"skills/{w.name}.zip"]
        assert data == distribution.skill_zip(w) == distribution.kit_files(ROOT)[f"skills/{w.name}.zip"]
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            assert z.testzip() is None
            names = z.namelist()
            assert names == sorted(names) == [f"{w.name}/", f"{w.name}/SKILL.md"]
            assert all(i.date_time == distribution.ZIP_TIME for i in z.infolist())
            assert z.read(f"{w.name}/SKILL.md").decode() == helpers.portable_skill_md(w)
            assert (z.getinfo(f"{w.name}/SKILL.md").external_attr >> 16) == 0o100644


def test_kit_has_the_expected_files(kit: dict[str, bytes]) -> None:
    knowledge = {k.removeprefix("knowledge/") for k in kit if k.startswith("knowledge/")}
    datasets = {f"dataset-{p.stem}.md" for p in (ROOT / "catalog").glob("*.yaml")}
    assert knowledge == {
        "data-conventions.md", "publication-outlook.md", "rubric.md", "case-study-standard.md", *datasets,
    }  # fmt: skip
    assert {k for k in kit if k.startswith("skills/")} == {f"skills/{w.name}.zip" for w in helpers.SKILLS}
    assert set(kit) - {f"knowledge/{k}" for k in knowledge} - {k for k in kit if k.startswith("skills/")} == {
        "project-instructions.md"
    }
    assert (
        "The rubric for scoring a hypothesis or a pitch isn't written yet"
        in kit["knowledge/rubric.md"].decode()
    )


def test_no_data_files_in_knowledge(kit: dict[str, bytes]) -> None:
    for rel, data in kit.items():
        if not rel.startswith("knowledge/"):
            continue
        assert rel.endswith(".md"), f"{rel}: knowledge files are Markdown only"
        text = data.decode("utf-8")
        assert len(data) < 100_000, f"{rel} is too big to be prose"
        assert not re.search(r"\.(csv|tsv|parquet|duckdb|xlsx|json|ndjson)\)", text), (
            f"{rel} links a data file"
        )
        rows = [ln for ln in text.splitlines() if ln.count(",") >= 4 and not ln.startswith(("-", "|", "#"))]
        assert len(rows) < 3, f"{rel} looks like CSV"
    for rel in ("publication-outlook.md", "rubric.md", "case-study-standard.md"):  # written in helpers.py
        assert asks_for_patient_details(kit[f"knowledge/{rel}"].decode()) == [], rel


def test_project_instructions_cover_the_rules_and_fit(kit: dict[str, bytes]) -> None:
    text = kit["project-instructions.md"].decode()
    assert len(text) <= distribution.INSTRUCTIONS_LIMIT
    for part in (
        helpers.RULES, helpers.LANE_RULES, helpers.NO_PATIENT_DATA_RULE, helpers.CITATION_RULE, helpers.ARTIFACT_RULE,
    ):  # fmt: skip
        assert part in text
    for lane in ("IHME GBD connector", "PubMed", "Observed and reported data"):
        assert lane in text
    assert asks_for_patient_details(text) == []
    assert "@" not in text


def test_instructions_over_the_limit_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helpers, "ARTIFACT_RULE", "x" * distribution.INSTRUCTIONS_LIMIT)
    with pytest.raises(ValueError, match="limit is 16000"):
        distribution.project_instructions()


def test_relative_links_become_repository_links() -> None:
    out = distribution._absolute_links(
        "[a](../README.md#example) [b](adr/0001.md) [c](#local) [d](https://x.org/y)", "docs", "https://g/r"
    )
    assert out == (
        "[a](https://g/r/blob/main/README.md#example) [b](https://g/r/blob/main/docs/adr/0001.md) "
        "[c](#local) [d](https://x.org/y)"
    )


def test_dataset_card_says_draft_and_lists_causes() -> None:
    entry = {
        "id": "toy", "title": "Toy", "publisher": "P", "years": {"start": 2020, "end": None},
        "geography": ["state"], "refresh": "weekly", "grain": "One row.", "citation": "Cite me.",
        "licence": {"id": "public-domain-us-federal", "terms_url": "https://t"},
        "source": {"landing_url": "https://l"}, "codebook_url": "https://c", "status": "draft",
        "caveats": ["Careful."], "gbd": {"causes": [{"condition": "C", "cause": "Syphilis", "pathogen": "T. p"}]},
    }  # fmt: skip
    card = distribution.dataset_card(entry)
    assert "2020 to the latest published year" in card and "Draft:" in card and "- Careful." in card
    assert "- C: Syphilis (pathogen: T. p)" in card
    reviewed = distribution.dataset_card(
        {**entry, "status": "reviewed", "gbd": None, "years": {"start": 1, "end": 2}}
    )
    assert (
        "Reviewed by a maintainer." in reviewed
        and "Matching GBD causes" not in reviewed
        and "1 to 2" in reviewed
    )


def test_no_forbidden_names_in_generated_text(kit: dict[str, bytes]) -> None:
    texts = [*distribution.plugin_files(ROOT).values()]
    texts += [v.decode() for k, v in kit.items() if not k.endswith(".zip")]
    for t in texts:
        assert "GBD companion" not in t


def test_tool_lists_match_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """README.md, docs/tools.md and CLAUDE.md list exactly the tools the server registers (make docs writes them)."""
    monkeypatch.setenv("HDR_HUB", "off")
    table = distribution.tool_table()
    for rel in distribution.TOOL_LIST_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert distribution.TOOLS_START in text, rel
        assert distribution.with_tool_table(text, table) == text, f"{rel} is stale: run make docs"
    assert distribution.with_tool_table("no markers", table) == "no markers"
