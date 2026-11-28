"""CROI on main's core (ADR 0016 row 10): html_table listing config, page_fields, crawl-private into two stores.

Every page is synthetic and written here (hard rule 5). The catalog entries are the real ones, so their selectors,
scope and listing rules are what is tested. The crawl's clock is faked: no test waits CROI's 120 s.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from health_data_registry import listing, private
from health_data_registry.adapters import LayoutError
from health_data_registry.adapters.extensions import page_fields as pf
from health_data_registry.private_crawl import NotCrawlable, crawl_private, row_for, stored_from
from health_data_registry.registry import Registry, load_registry
from tests.adapters.helpers import Site

HOST = "https://www.croiconference.org"
YEARS = range(2022, 2027)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def reg() -> Registry:
    return load_registry()


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Clock:
    clock = Clock()
    monkeypatch.setattr(listing, "time", clock)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))  # host locks of this test only
    return clock


def search(year: int, page: int = 1) -> str:
    return f"{HOST}/search-abstracts/page/{page}/?aa=&at=&an=&sn=&kw=&yr={year}&st=&q=1"


def listing_html(total: int, items: list[tuple[str, str]]) -> bytes:
    links = "".join(
        f'<div class="post-entry-content"><h3 class="entry-title"><a href="/abstract/{slug}/">{number} – Title {number}</a></h3></div>'
        for slug, number in items
    )
    return f'<p class="abstract-search-results-count">{total} Results</p>{links}'.encode()


def item_html(number: str, *, text: str, poster: bool = True, sidebar_number: str | None = None) -> bytes:
    poster_link = (
        f'<a href="{HOST}/wp-content/uploads/p{number}.pdf" title="Download Poster">Poster</a>'
        if poster
        else ""
    )
    return f"""<html><body>
<h1 class="entry-title">{number} – Same-day ART start in Kenya</h1>
<article class="abstract abstract-session-type-poster abstract-category-hiv-treatment">
  <div class="entry-content"><p>{text}</p></div>
</article>
<div class="abstract-sidebar-content"><h4>Abstract Number</h4><div class="abstract-sidebar-text">{sidebar_number or number}</div></div>
<div class="abstract-sidebar-content"><h4>Session Title</h4><div class="abstract-sidebar-text">HIV treatment</div></div>
<div class="abstract-sidebar-content"><h4>Authors</h4><div class="abstract-sidebar-text">A. Author, B. Author</div></div>
{poster_link}
</body></html>""".encode()  # fmt: skip


def site(items_2025: list[tuple[str, str]], **item_kw: Any) -> Site:
    pages: dict[str, bytes] = {search(y): listing_html(0, []) for y in YEARS if y != 2025}
    pages[search(2025)] = listing_html(len(items_2025), items_2025)
    for slug, number in items_2025:
        pages[f"{HOST}/abstract/{slug}/"] = item_html(
            number, text=f"A retrospective cohort of PrEP users, {number}.", **item_kw
        )
    return Site(pages)


ITEMS = [("2112-2025", "957"), ("2113-2025", "958"), ("2114-2025", "959")]


def _stored(lake: Path, reg: Registry, ds_id: str) -> private.PrivateRows:
    got = private.read_rows(lake, reg.get(ds_id))
    assert got is not None
    return got


def test_a_slice_then_the_rest_fills_both_stores_and_marks_the_year_complete(
    reg: Registry, tmp_path: Path
) -> None:
    lake = tmp_path / "lake"
    s = site(ITEMS)
    first = crawl_private(reg, "croi_abstracts_index", lake, s, max_requests=2)
    assert first.stores == ["croi_abstracts_index", "croi_abstracts_text"]
    assert first.years[2025] == {"read": 2, "listed": 3, "complete": False}
    index = _stored(lake, reg, "croi_abstracts_index")
    assert len(index.rows) == 2 and not index.complete  # a year still being read is never complete
    # The poster is linked, never fetched; no listing outside the catalogued years, nothing outside scope.
    assert not [u for u in s.calls if u.endswith(".pdf")]
    second = crawl_private(reg, "croi_abstracts_index", lake, s, max_requests=10)
    assert second.years[2025] == {"read": 3, "listed": 3, "complete": True}
    assert "the year is complete" in "\n".join(second.lines())
    index = _stored(lake, reg, "croi_abstracts_index")
    text = _stored(lake, reg, "croi_abstracts_text")
    assert len(index.rows) == len(text.rows) == 3
    by_number = {r["abstract_number"]: r for r in index.rows}
    row = by_number["957"]
    assert row["year"] == 2025 and row["heading"] == "957 – Same-day ART start in Kenya"
    assert row["session_type"] == "poster" and row["category"] == "hiv-treatment"
    assert row["authors"] == "A. Author, B. Author" and row["session_title"] == "HIV treatment"
    assert row["poster_url"] == f"{HOST}/wp-content/uploads/p957.pdf"
    assert row["page_url"] == f"{HOST}/abstract/2112-2025/" and len(row["page_sha256"]) == 64
    assert set(row["condition_tags"].split("; ")) >= {"hiv", "hiv_prevention"}  # ART and PrEP in capitals
    assert "cohort" in row["design_tags"] and "kenya" in row["geography_tags"]
    # The text never reaches the metadata store; the text store holds only the key, the text and provenance.
    assert "abstract_text" not in row
    assert set(text.rows[0]) == {
        "abstract_number",
        "year",
        "abstract_text",
        "page_url",
        "page_sha256",
        "retrieved_at",
    }
    assert {r["abstract_text"] for r in text.rows} >= {"A retrospective cohort of PrEP users, 957."}
    # Each item page was fetched once across both runs, for both stores.
    assert sum(u.startswith(f"{HOST}/abstract/") for u in s.calls) == 3


def test_a_listing_and_page_that_disagree_on_the_number_are_refused(reg: Registry, tmp_path: Path) -> None:
    with pytest.raises(LayoutError):
        crawl_private(reg, "croi_abstracts_index", tmp_path / "lake", site(ITEMS[:1], sidebar_number="999"))
    assert private.read_rows(tmp_path / "lake", reg.get("croi_abstracts_index")) is None  # nothing stored


def test_only_the_crawled_source_is_crawled(reg: Registry, tmp_path: Path) -> None:
    with pytest.raises(NotCrawlable, match="filled by croi_abstracts_index"):
        crawl_private(reg, "croi_abstracts_text", tmp_path, site([]))
    with pytest.raises(NotCrawlable, match="private store"):
        crawl_private(reg, "cdc_nndss_weekly", tmp_path, site([]))
    assert [d.id for d in stored_from(reg, "croi_abstracts_index")] == ["croi_abstracts_text"]


def test_the_two_entries_read_the_same_listing(reg: Registry) -> None:
    a, b = reg.get("croi_abstracts_index").entry.source, reg.get("croi_abstracts_text").entry.source
    assert a.model_dump() == b.model_dump()
    assert a.min_interval_s == 120  # CROI's own crawl delay, as a floor the core can only lengthen


def test_rows_carry_only_their_contracts_columns(reg: Registry) -> None:
    record = {"abstract_number": "1", "heading": "1 – TB in Peru", "abstract_text": "x", "document_url": "u",
              "document_sha256": "s", "document_retrieved_at": "t", "poster_url": None, "extra": "never"}  # fmt: skip
    for ds_id in ("croi_abstracts_index", "croi_abstracts_text"):
        ds = reg.get(ds_id)
        row = row_for(ds, record, 2024)
        assert set(row) == set(ds.contract.column_names) and row["year"] == 2024
    assert row_for(reg.get("croi_abstracts_index"), record, 2024)["condition_tags"] == "tuberculosis"


def test_crawl_croi_workflow_runs_the_command(reg: Registry) -> None:
    wf = yaml.safe_load((REPO / ".github" / "workflows" / "crawl-croi.yml").read_text(encoding="utf-8"))
    text = json.dumps(wf)
    assert "crawl-private croi_abstracts_index" in text and "index-croi" not in text


# ---------------------------------------------------------------------------------------------------- page_fields


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({}, "at least one field"),
        ({"links": {"Poster": {"selector": "a"}}}, "lower case"),
        ({"links": {"x_y": {"selector": "a:hover"}}}, "selector"),
        ({"links": {"ab": {"selector": "a"}}, "class_slugs": {"ab": {"selector": "a", "prefix": "p-"}}}, "once"),
    ],
)  # fmt: skip
def test_page_fields_arguments(args: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        pf.Args.model_validate(args)


def test_page_fields_refuse_what_they_would_misread() -> None:
    args = pf.Args.model_validate(
        {
            "labelled": {
                "block_selector": "div.b",
                "heading_selector": "h4",
                "value_selector": "p",
                "fields": {"number": "Number"},
            }
        }  # fmt: skip
    )
    twice = b'<div class="b"><h4>Number</h4><p>1</p></div><div class="b"><h4> number </h4><p>2</p></div>'
    with pytest.raises(LayoutError, match="twice"):
        pf.read(args, twice, "u")
    assert pf.read(args, b"<p>nothing</p>", "u") == [{"number": None}]  # not printed: empty, never guessed
    slugs = pf.Args.model_validate({"class_slugs": {"kind": {"selector": "article", "prefix": "t-"}}})
    with pytest.raises(LayoutError, match="2 classes"):
        pf.read(slugs, b'<article class="t-a t-b"></article>', "u")


# ------------------------------------------------------------------------------------------------ find_prior_work


def test_find_prior_work_answers_from_the_index_with_coverage_and_provenance(
    reg: Registry, tmp_path: Path
) -> None:
    from health_data_registry import tools

    lake = tmp_path / "lake"
    crawl_private(reg, "croi_abstracts_index", lake, site(ITEMS), max_requests=2)  # 2025 still being read
    out = tools.find_prior_work(reg, condition="prep", years="2024-2025", lake=lake, fmt="json")
    assert isinstance(out, dict)
    rows = [dict(zip(out["columns"], r, strict=True)) for r in out["rows"]]
    assert {r["abstract_number"] for r in rows} == {"957", "958"}  # what the index holds, never "no answer"
    r = rows[0]
    assert r["source_id"] == "croi_abstracts_index" and r["year"] == 2025
    assert r["link"].startswith(f"{HOST}/abstract/") and r["poster_url"].endswith(".pdf")
    assert r["year_coverage"] == "index_incomplete" and r["matched_on"].startswith("condition tag")
    assert r["read_from"] == r["link"] and len(r["sha256"]) == 64 and r["retrieved_at"]  # hard rule 8
    assert "abstract_text" not in out["columns"]
    assert not any("A retrospective cohort" in str(v) for row in out["rows"] for v in row)  # text never shown
    cov = "\n".join(out["coverage"])
    assert "CROI Foundation 2025: index_incomplete, 2 abstracts held" in cov
    assert "CROI Foundation 2024: not_indexed" in cov
    assert any("is not 'no match'" in n for n in out["notes"])
    # A word only the private text holds still matches: the text is searched, not returned.
    by_text = tools.find_prior_work(
        reg, condition="retrospective cohort", dataset="croi_abstracts_index", lake=lake, fmt="json"
    )
    assert isinstance(by_text, dict) and len(by_text["rows"]) == 2
    # The other conferences have no store here: named as not reachable, never as "no match".
    assert "IDWeek 2025: not_reachable" in cov
    # Nothing matched in a complete year is said to be complete; a place filter narrows.
    crawl_private(reg, "croi_abstracts_index", lake, site(ITEMS))
    none = tools.find_prior_work(
        reg, condition="measles", years="2025", dataset="croi_abstracts_index", lake=lake, fmt="json"
    )
    assert (
        isinstance(none, dict)
        and none["rows"] == []
        and "2025: complete, 3 abstracts held" in none["coverage"][0]
    )
    kenya = tools.find_prior_work(reg, condition="hiv", geography="Peru", years="2025", lake=lake, fmt="json")
    assert isinstance(kenya, dict) and kenya["rows"] == []


@pytest.mark.parametrize(
    ("kw", "code"),
    [({"years": "last year"}, "bad_years"), ({"dataset": "cdc_nndss_weekly"}, "not_in_research_index")],
)
def test_find_prior_work_refusals_say_what_to_do(
    reg: Registry, tmp_path: Path, kw: dict[str, str], code: str
) -> None:
    from health_data_registry import tools

    out = tools.find_prior_work(reg, condition="hiv", lake=tmp_path, fmt="json", **kw)
    assert isinstance(out, dict) and out["refused"] == code and out["next_step"]


# ------------------------------------------------------------------------------------------- export-public leaks

LONG = (
    "Among 412 adults starting same-day antiretroviral therapy in western Kenya, retention at twelve months "
    "was higher in clinics offering community refill groups than in clinics without them."
)


def test_export_public_refuses_a_partial_copy_of_private_text(reg: Registry, tmp_path: Path) -> None:
    from health_data_registry.publish import private_leaks

    lake, out = tmp_path / "lake", tmp_path / "release"
    out.mkdir()
    private.write_rows(lake, reg.get("croi_abstracts_text"), "year=2025", [
        {"abstract_number": "957", "year": 2025, "abstract_text": LONG, "page_url": f"{HOST}/abstract/2112-2025/",
         "page_sha256": "a" * 64, "retrieved_at": "2026-11-24T00:00:00+00:00"}])  # fmt: skip
    (out / "card.md").write_text("A card about HIV retention, in our own words.", encoding="utf-8")
    assert private_leaks(reg, lake, out) == []
    excerpt = " ".join(LONG.split()[3:16])  # from a word start, longer than 64 characters
    assert len(excerpt) > 64
    (out / "notes.md").write_text(
        f"Quoted:\n{excerpt.replace(' ', chr(10), 2)}\n", encoding="utf-8"
    )  # line breaks
    (out / "link").symlink_to(lake / "private" / "croi_abstracts_text")
    problems = private_leaks(reg, lake, out)
    assert "notes.md holds text from a private-tier source; nothing private is published" in problems
    assert "link points into the private area of the lake" in problems


def test_the_leak_check_also_reads_the_crawls_own_records(reg: Registry, tmp_path: Path) -> None:
    from health_data_registry.publish import private_values

    lake = tmp_path / "lake"
    crawl_private(reg, "croi_abstracts_index", lake, site(ITEMS[:1]), max_requests=1)
    held = private_values(reg, lake)
    assert any("A retrospective cohort of PrEP users" in v for v in held)
