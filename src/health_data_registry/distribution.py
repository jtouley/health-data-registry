"""Package the workflows in helpers.py for claude.ai: the plugin, its marketplace, and the Claude Projects kit.

- plugin/ and .claude-plugin/marketplace.json are kept in git (`make docs` writes them; a test checks they match).
  The marketplace file sits at the repo root because a marketplace is the directory holding
  `.claude-plugin/marketplace.json`, and claude.ai adds a marketplace by its repository URL.
- dist/projects-kit/ is built by `make kit` (in CI, and attached to releases), never committed: the Project
  instructions to paste, the knowledge files to upload, and one upload zip per skill.

Formats follow Anthropic's docs: https://claude.com/docs/plugins/build.md (plugin folder, plugin.json, .mcp.json),
https://code.claude.com/docs/en/plugins/marketplace-reference.md (marketplace.json) and
https://claude.com/docs/skills/how-to.md (a skill zip holds the skill folder at its top level).
"""

from __future__ import annotations

import io
import json
import posixpath
import re
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from health_data_registry import helpers

NAME = "health-data-registry"
MARKETPLACE_PATH = ".claude-plugin/marketplace.json"
PLUGIN_DIR = "plugin"
KIT_DIR = "dist/projects-kit"

# Project instructions: 16,000 characters is the documented limit for the new version of Projects
# (https://code.claude.com/docs/en/claude-projects.md, "Project instructions ... up to 16,000 characters").
# No limit is published for classic claude.ai chat Projects; this one is applied to both.
INSTRUCTIONS_LIMIT = 16_000

# 1980-01-01 is the earliest time a zip entry can hold; a fixed time keeps the zips byte-for-byte reproducible.
ZIP_TIME = (1980, 1, 1, 0, 0, 0)

DESCRIPTION = (
    "Observed public health data for infectious disease research (CDC, WHO), with source, version and method on "
    "every number, plus step-by-step research workflows for clinician-researchers."
)


@dataclass(frozen=True)
class Settings:
    version: str
    public_url: str
    repository: str
    owner: str


def settings(root: Path) -> Settings:
    """The values the plugin takes from pyproject.toml, so the version and the URL are each set in one place."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    return Settings(
        version=project["version"],
        public_url=data["tool"][NAME]["HDR_PUBLIC_URL"],
        repository=project["urls"]["Repository"],
        owner=project["authors"][0]["name"],
    )


def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def marketplace_json(s: Settings) -> dict[str, Any]:
    return {
        "name": NAME,
        "description": "The health-data-registry plugin for Claude.",
        "owner": {"name": s.owner, "url": s.repository},
        "plugins": [{"name": NAME, "source": f"./{PLUGIN_DIR}", "description": DESCRIPTION}],
    }


def plugin_json(s: Settings) -> dict[str, Any]:
    return {
        "name": NAME,
        "displayName": "Health Data Registry",
        "version": s.version,
        "description": DESCRIPTION,
        "author": {"name": s.owner, "url": s.repository},
        "homepage": s.repository,
        "repository": s.repository,
        "license": "Apache-2.0",
        "keywords": ["public-health", "infectious-disease", "surveillance", "research"],
    }


def mcp_json(s: Settings) -> dict[str, Any]:
    """Only this server: the IHME GBD and PubMed connectors are the fellow's own, named in the instructions."""
    return {"mcpServers": {NAME: {"type": "http", "url": s.public_url}}}


def plugin_readme() -> str:
    skills = "\n".join(f"- `{w.name}`: {w.description}" for w in helpers.SKILLS)
    return "\n".join(
        [
            "# Health Data Registry plugin",
            "",
            "<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->",
            "",
            "Adds the health-data-registry connector and its research workflows to Claude. The connector answers "
            "plain questions about infectious disease with observed, population-level public data (CDC, WHO), each "
            "number with its source, version and method. Use it beside your own IHME GBD and PubMed connectors.",
            "",
            "## Use it",
            "",
            "Add the plugin, connect the connector from the plugin's Connectors tab, then start with `start-here`. "
            "The skills:",
            "",
            skills,
            "",
            "## Data",
            "",
            "The connector is read-only and reads only catalogued public sources. It sends your questions to the "
            "hosted server and stores nothing about you. Never share patient information with it.",
            "",
        ]
    )


def plugin_files(root: Path) -> dict[str, str]:
    """Every file of the plugin and its marketplace, by path from the repo root."""
    s = settings(root)
    files = {
        MARKETPLACE_PATH: _json(marketplace_json(s)),
        f"{PLUGIN_DIR}/.claude-plugin/plugin.json": _json(plugin_json(s)),
        f"{PLUGIN_DIR}/.mcp.json": _json(mcp_json(s)),
        f"{PLUGIN_DIR}/README.md": plugin_readme(),
    }
    for w in helpers.SKILLS:
        files[f"{PLUGIN_DIR}/skills/{w.name}/SKILL.md"] = helpers.portable_skill_md(w)
    return files


def project_instructions() -> str:
    """project-instructions.md: pasted into the Project's instructions; must stay under INSTRUCTIONS_LIMIT."""
    text = "\n\n".join(
        [
            helpers.PROJECT_INSTRUCTIONS_INTRO,
            helpers.LANE_RULES,
            helpers.NO_PATIENT_DATA_RULE,
            helpers.CITATION_RULE,
            helpers.ARTIFACT_RULE,
            helpers.RULES,
        ]
    )
    if len(text) > INSTRUCTIONS_LIMIT:
        raise ValueError(
            f"project instructions are {len(text)} characters; the limit is {INSTRUCTIONS_LIMIT}"
        )
    return text + "\n"


_LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)")


def _absolute_links(text: str, base: str, repository: str) -> str:
    """Relative links in a repo doc point at GitHub once the file is uploaded somewhere else."""

    def fix(m: re.Match[str]) -> str:
        path, _, anchor = m.group(1).partition("#")
        url = f"{repository}/blob/main/{posixpath.normpath(posixpath.join(base, path))}"
        return f"]({url}{'#' + anchor if anchor else ''})"

    return _LINK.sub(fix, text)


def dataset_card(entry: dict[str, Any]) -> str:
    """A plain-words card for one catalog entry: what it is, what it covers, how to cite it, what to watch for."""
    years = entry["years"]
    span = f"{years['start']} to {years['end'] or 'the latest published year'}"
    licence = entry["licence"]
    status = (
        "Draft: the contract has not been reviewed by a maintainer yet, so say so when you use it."
        if entry["status"] == "draft"
        else "Reviewed by a maintainer."
    )
    lines = [
        f"# Dataset: {entry['title']}",
        "",
        f"- **Id** (for this tool's describe_dataset and get_series): `{entry['id']}`",
        f"- **Publisher:** {entry['publisher']}",
        f"- **Years:** {span}; **places:** {', '.join(entry['geography'])}; **updated:** {entry['refresh']}",
        f"- **One row:** {entry['grain']}",
        f"- **Licence:** {licence['id']} ({licence['terms_url']})",
        f"- **Source:** {entry['source']['landing_url']}; **codebook:** {entry['codebook_url']}",
        f"- **Status:** {status}",
        "",
        "## How to cite it",
        "",
        entry["citation"],
        "",
        "## Caveats",
        "",
        *[f"- {c}" for c in entry.get("caveats", [])],
    ]
    causes = (entry.get("gbd") or {}).get("causes") or []
    if causes:
        lines += ["", "## Matching GBD causes", ""]
        for c in causes:
            pathogen = f" (pathogen: {c['pathogen']})" if c.get("pathogen") else ""
            lines.append(f"- {c['condition']}: {c['cause']}{pathogen}")
    return "\n".join(lines) + "\n"


def knowledge_files(root: Path) -> dict[str, str]:
    """knowledge/*.md: Markdown only, never data files."""
    s = settings(root)
    conventions = (root / "docs/data-conventions.md").read_text(encoding="utf-8")
    files = {
        "data-conventions.md": _absolute_links(conventions, "docs", s.repository),
        "publication-outlook.md": helpers.PUBLICATION_OUTLOOK,
        "rubric.md": helpers.RUBRIC,
        "case-study-standard.md": helpers.CASE_STUDY_STANDARD,
    }
    for path in sorted((root / "catalog").glob("*.yaml")):
        entry = yaml.safe_load(path.read_text(encoding="utf-8"))
        files[f"dataset-{entry['id']}.md"] = dataset_card(entry)
    return files


def skill_zip(w: helpers.Workflow) -> bytes:
    """The upload zip for one skill: `<name>/SKILL.md` at the top level, fixed times and order, same bytes each time."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for arcname, data in ((f"{w.name}/", None), (f"{w.name}/SKILL.md", helpers.portable_skill_md(w))):
            info = zipfile.ZipInfo(arcname, date_time=ZIP_TIME)
            info.create_system = 3  # Unix, so the permissions below are read the same everywhere
            if data is None:
                info.external_attr = (0o40755 << 16) | 0x10
                z.writestr(info, b"")
            else:
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, data.encode("utf-8"), compresslevel=9)
    return buf.getvalue()


def kit_files(root: Path) -> dict[str, bytes]:
    """Every file of the Projects kit, by path inside the kit folder."""
    files = {"project-instructions.md": project_instructions().encode("utf-8")}
    for name, text in knowledge_files(root).items():
        files[f"knowledge/{name}"] = text.encode("utf-8")
    for w in helpers.SKILLS:
        files[f"skills/{w.name}.zip"] = skill_zip(w)
    return files


# ------------------------------------------------------------------------------- generated tool list

TOOLS_START = "<!-- tools:start (generated by `make docs` from the server's tool registry; don't edit) -->"
TOOLS_END = "<!-- tools:end -->"
TOOL_LIST_FILES = ("README.md", "docs/tools.md")


def tool_table() -> str:
    """The server's registered tools, one row each, with the first sentence of the description Claude reads."""
    import asyncio

    from health_data_registry.query import Engine
    from health_data_registry.registry import load_registry
    from health_data_registry.server import build_server

    reg = load_registry()
    listed = asyncio.run(build_server(reg, Engine(registry=reg, lake=Path("lake"), hub=None)).list_tools())
    rows = ["| Tool | What it gives you |", "|---|---|"]
    for t in listed:
        text = " ".join((t.description or "").split("\n\n")[0].split())
        first = re.split(r"(?<!e\.g)(?<!i\.e)(?<=\.)\s(?=[A-Z])", text, maxsplit=1)[0]
        rows.append(f"| `{t.name}` | {first.replace('|', '/')} |")
    return "\n".join(rows)


def with_tool_table(text: str, table: str) -> str:
    """`text` with the block between the tool-list markers replaced by `table` (unchanged if it has no markers)."""
    start, end = text.find(TOOLS_START), text.find(TOOLS_END)
    if start < 0 or end < start:
        return text
    return text[: start + len(TOOLS_START)] + "\n\n" + table + "\n\n" + text[end:]
