from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from health_data_registry import helpers, tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry
from health_data_registry.server import build_server

SPEC_TOOLS = {
    "list_sources", "search_datasets", "describe_dataset", "suggest_datasets", "get_series",
    "get_aligned_with_gbd", "get_rate", "get_provenance", "list_caveats", "find_prior_work", "query_sql",
}  # fmt: skip
SPEC_HELPERS = {
    "start_here", "set_up_my_project", "spot_a_pattern", "check_its_new", "find_data_for_my_question",
    "draft_my_pitch", "help_me_fix_it", "walk_me_through_install",
}  # fmt: skip


def test_tool_and_helper_surface_matches_spec(registry: Registry, lake: Path) -> None:
    s = build_server(registry, Engine(registry=registry, lake=lake))
    listed = asyncio.run(s.list_tools())
    assert {t.name for t in listed} == SPEC_TOOLS
    assert all(t.annotations and t.annotations.read_only_hint for t in listed)
    assert {p.name for p in asyncio.run(s.list_prompts())} == SPEC_HELPERS


def test_helper_prompt_fills_arguments(registry: Registry, lake: Path) -> None:
    s = build_server(registry, Engine(registry=registry, lake=lake))
    r = asyncio.run(s.get_prompt("spot_a_pattern", {"condition": "syphilis", "place": "Ohio"}))
    text = r.messages[0].content.text  # type: ignore[union-attr]
    assert "syphilis in Ohio" in text and "patient information" in text


def test_helpers_never_ask_for_secrets_and_doc_is_current() -> None:
    for h in helpers.HELPERS:
        body = h.render().lower()
        assert "patient" in body or h.name in ("walk_me_through_install",), h.name
        assert "password" not in body or "never" in body
    doc = Path(__file__).resolve().parents[1] / "docs" / "helpers.md"
    assert doc.read_text(encoding="utf-8") == helpers.markdown(), "run: make docs"


def test_tool_call_end_to_end_returns_csv(registry: Registry, lake: Path) -> None:
    s = build_server(registry, Engine(registry=registry, lake=lake))
    out = asyncio.run(s.call_tool("get_series", {"condition": "syphilis", "locations": ["Michigan"]}))
    text = out.content[0].text  # type: ignore[union-attr]
    assert text.startswith("location,year,value")


def test_unexpected_failure_is_plain_words(
    registry: Registry, lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> str:
        raise RuntimeError("duckdb internal error at 0x7f")

    monkeypatch.setattr(tools, "get_series", boom)
    s = build_server(registry, Engine(registry=registry, lake=lake))
    out = asyncio.run(s.call_tool("get_series", {"condition": "syphilis"}))
    text = out.content[0].text  # type: ignore[union-attr]
    assert "Something went wrong" in text and "0x7f" not in text and "Traceback" not in text
