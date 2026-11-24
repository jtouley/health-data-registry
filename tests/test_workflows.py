"""The workflows in helpers.py are the one source for the MCP prompts, docs/helpers.md and the template's skills."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
import yaml

from health_data_registry import helpers
from health_data_registry.query import Engine
from health_data_registry.registry import Registry
from health_data_registry.server import build_server

ROOT = Path(__file__).resolve().parents[1]
SKILL_KEYS = {"name", "description", "argument-hint"}
PATIENT_TERMS = re.compile(
    r"patient|\bmrn\b|medical record|date of birth|\bdob\b|\bchart\b|your case|the case\b", re.I
)
NEGATION = re.compile(r"\bnever\b|\bno\b|\bnot\b|n't\b|outside this chat|\bstop\b", re.I)


def asks_for_patient_details(text: str) -> list[str]:
    """Sentences that mention a patient or a case without saying it stays out of the chat."""
    sentences = re.split(r"(?<=[.!?])\s+|\n", text)
    return [s for s in sentences if PATIENT_TERMS.search(s) and not NEGATION.search(s)]


def frontmatter(text: str) -> tuple[dict[str, object], str]:
    assert text.startswith("---\n"), "frontmatter must open on the first line"
    head, sep, body = text[4:].partition("\n---\n")
    assert sep, "frontmatter is not closed"
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data, body


def test_generated_files_match_the_workflows() -> None:
    files = helpers.generated_files()
    for rel, text in files.items():
        path = ROOT / rel
        assert path.is_file(), f"{rel} is missing: run make docs"
        assert path.read_text(encoding="utf-8") == text, f"{rel} drifted: run make docs"
    on_disk = {p.name for p in (ROOT / helpers.SKILLS_DIR).iterdir()}
    assert on_disk == {w.name for w in helpers.SKILLS}, "stale skill folders: run make docs"


def test_the_six_story_skills_and_the_kept_helpers() -> None:
    assert [w.name for w in helpers.SKILLS] == [
        "start-here", "research-story", "scope-an-area", "find-opportunities", "refine-hypothesis",
        "build-story", "help-me-fix-it",
    ]  # fmt: skip
    assert {w.name for w in helpers.WORKFLOWS} - {w.name for w in helpers.SKILLS} == {
        "walk-me-through-install",
        "set-up-my-project",
    }
    assert helpers.get("set_up_my_project") is helpers.get("set-up-my-project")
    with pytest.raises(KeyError):
        helpers.get("spot_a_pattern")
    arts = {w.name: w.artifact for w in helpers.WORKFLOWS}
    assert arts["refine-hypothesis"] == "hypothesis_card"
    assert arts["find-opportunities"] == "opportunity_brief"
    assert arts["build-story"] == "pitch"


def test_every_named_tool_exists_or_is_coming(registry: Registry, lake: Path) -> None:
    s = build_server(registry, Engine(registry=registry, lake=lake))
    served = {t.name for t in asyncio.run(s.list_tools())}
    assert not helpers.COMING_TOOLS & served, "a coming tool has arrived: take it off COMING_TOOLS"
    for w in helpers.WORKFLOWS:
        for step in w.steps:
            for tool in step.tools:
                assert tool in served | helpers.COMING_TOOLS, (w.name, tool)
                assert tool in step.text, f"{w.name}: step names {tool} in tools but not in its words"
                if tool in helpers.COMING_TOOLS:
                    assert "coming" in step.text, f"{w.name}: say plainly that {tool} is coming"
        for word in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", w.skill_body()):
            assert word in served | helpers.COMING_TOOLS, f"{w.name} mentions {word}, not a tool"
    assert helpers.get("find-opportunities").tools[0] == "find_research_opportunities"


@pytest.mark.parametrize("w", helpers.SKILLS, ids=lambda w: w.name)
def test_skill_frontmatter_is_valid(w: helpers.Workflow) -> None:
    data, body = frontmatter(helpers.skill_md(w))
    assert set(data) <= SKILL_KEYS and {"name", "description"} <= set(data)
    name, desc = data["name"], data["description"]
    assert isinstance(name, str) and isinstance(desc, str)
    assert name == w.name and re.fullmatch(r"[a-z0-9-]{1,64}", name)
    assert "anthropic" not in name and "claude" not in name
    assert 0 < len(desc) <= 1024 and "<" not in desc and ">" not in desc
    if w.argument_hint:
        assert data["argument-hint"] == w.argument_hint
    else:
        assert "argument-hint" not in data
    assert ("$ARGUMENTS" in body) == bool(w.args)
    assert body.count("\n") < 500


@pytest.mark.parametrize("w", helpers.WORKFLOWS, ids=lambda w: w.name)
def test_no_workflow_asks_for_patient_details(w: helpers.Workflow) -> None:
    for text in (w.render(), w.skill_body(), w.description):
        assert asks_for_patient_details(text) == [], w.name


def test_patient_check_catches_a_request() -> None:
    assert asks_for_patient_details("Now tell me about your patient. Then go on.")
    assert asks_for_patient_details("Paste the chart here.")
    assert not asks_for_patient_details("Never paste patient details.")


def test_rules_end_every_skill() -> None:
    for w in helpers.SKILLS:
        assert w.rules and helpers.skill_md(w).rstrip().endswith(helpers.RULES), w.name
    assert helpers.RULES in helpers.markdown()


def test_render_fills_or_marks_the_argument() -> None:
    w = helpers.get("refine-hypothesis")
    assert "My idea: [idea]." in w.render()
    assert "My idea: x." in w.render(idea="x")
    assert w.tools == ("get_series", "get_aligned_with_gbd", "find_prior_work", "list_caveats")
    assert (
        helpers.get("help-me-fix-it")
        .render(what_i_see="a red box")
        .startswith("Something isn't working. What I see: a red box")
    )
    plain = helpers.get("set-up-my-project").render()
    assert helpers.RULES not in plain and "\n\n" not in plain.split("Help me set up")[1][:20]


@pytest.mark.parametrize(
    ("kw", "msg"),
    [
        ({"name": "Bad_Name"}, "lowercase"),
        ({"name": "a--b"}, "lowercase"),
        ({"args": ("a", "b")}, "one free-text"),
        ({"rules": False}, "RULES"),
    ],
)
def test_workflow_rejects_bad_records(kw: dict[str, object], msg: str) -> None:
    base: dict[str, object] = {
        "name": "ok",
        "title": "t",
        "description": "d",
        "argument_hint": "",
        "intro": "i",
        "steps": (),
    }
    with pytest.raises(ValueError, match=msg):
        helpers.Workflow(**{**base, **kw})  # type: ignore[arg-type]
