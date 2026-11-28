"""find_prior_work: conference abstracts on a condition from the research index, with what the index covers.

The research index is every private-tier source whose contract has a ``condition_tags`` column: the DOI-published
conferences (IDWeek, SHEA Spring, APIC, ICID) and CROI. Each is read from its own private store (private.read_rows:
the local area, then its private Hugging Face dataset), never fetched for a question.

Show everything, qualify, don't decide (ADR 0018). An abstract matches when its rule-based condition tags include a
tag the question names, or, when the question names no tag, when every word of the condition is in its title or
text. Every matching abstract is returned with its link and provenance (the page or DOI it was read from, when, and
the sha256 of those bytes; hard rule 8). Beside them, the index's coverage for every year asked: complete, being
read (``index_incomplete``), not indexed, or not reachable from this setup (``bad_years``). A year that isn't
complete is never reported as "no match"; the answer says what it holds so far.

Abstract text is used only to match. It is never returned: CROI's text is personal-use only and every abstract's
text is kept private (hard rule 10); the link opens it on the publisher's site. Poster PDFs are linked, never read.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from health_data_registry import private, research_tags
from health_data_registry.registry import Dataset, Registry
from health_data_registry.source_config import HtmlTableConfig

COLUMNS = [
    "source_id", "conference", "year", "abstract_number", "title", "authors", "session", "link", "poster_url",
    "matched_on", "condition_tags", "design_tags", "data_source_tags", "geography_tags", "year_coverage",
    "retrieved_at", "read_from", "sha256",
]  # fmt: skip
COVERAGE_COLUMNS = ["source_id", "conference", "year", "coverage", "abstracts_held", "what_it_means"]
COMPLETE, INCOMPLETE, NOT_INDEXED, UNAVAILABLE = (
    "complete",
    "index_incomplete",
    "not_indexed",
    "not_reachable",
)
MEANING = {
    COMPLETE: "every abstract the publisher lists for the year is indexed: no row means no tagged match",
    INCOMPLETE: "the year is still being indexed: abstracts not yet read may match, so no row is not 'no match'",
    NOT_INDEXED: "the year is catalogued but not indexed yet: nothing is known about it here",
    UNAVAILABLE: "this setup can't reach the source's private store: nothing is known about it here",
}
_WORD = re.compile(r"[a-z0-9]{3,}")


def research_index(registry: Registry, dataset: str | None = None) -> list[Dataset]:
    """The sources find_prior_work searches: private-tier, with rule-based condition tags."""
    out = [
        ds
        for ds in registry
        if ds.entry.tier == "private" and "condition_tags" in ds.contract.column_names
        and (dataset is None or ds.id == dataset)
    ]  # fmt: skip
    return sorted(out, key=lambda d: d.id)


def catalogued_years(ds: Dataset) -> list[int]:
    """The years a source's catalog entry says it indexes: its DOI supplements, or its listing's years."""
    src = ds.entry.source
    if src.supplements:
        return sorted({s.year for s in src.supplements})
    cfg = src.config() if src.kind == "html_table" else None
    if isinstance(cfg, HtmlTableConfig) and cfg.listing is not None:
        return list(range(cfg.listing.years.first, cfg.listing.years.last + 1))
    return []


def parse_years(years: str | None) -> tuple[int, int] | None:
    """'2023' or '2020-2024' (or '2020–2024'); None for all years. Anything else is refused in plain words."""
    if years is None or not years.strip():
        return None
    m = re.fullmatch(r"\s*(\d{4})\s*(?:[-–]\s*(\d{4})\s*)?", years)
    if m is None:
        raise ValueError(f"years must be one year (2023) or a range (2020-2024), not {years!r}")
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    if last < first:
        first, last = last, first
    return first, last


@dataclass(frozen=True)
class Search:
    tags: tuple[str, ...]
    words: tuple[str, ...]
    place_tags: tuple[str, ...]
    place_words: tuple[str, ...]

    @staticmethod
    def of(condition: str, geography: str | None) -> Search:
        place = geography or ""
        return Search(
            tags=tuple(research_tags.query_tags(research_tags.CONDITION_RULES, condition)),
            words=tuple(_WORD.findall(condition.casefold())),
            place_tags=tuple(research_tags.query_tags(research_tags.GEOGRAPHY_RULES, place)),
            place_words=tuple(_WORD.findall(place.casefold())),
        )

    def match(self, row: Mapping[str, Any], text: str) -> str | None:
        """How the row matches (its tags, or the words), or None."""
        held = set(_split(row.get("condition_tags")))
        how = None
        if self.tags and held & set(self.tags):
            how = "condition tag: " + ", ".join(sorted(held & set(self.tags)))
        elif not self.tags and self.words and all(w in text for w in self.words):
            how = "words in the title or text"
        if how is None:
            return None
        if self.place_tags or self.place_words:
            places = set(_split(row.get("geography_tags")))
            if not (
                places & set(self.place_tags)
                or (self.place_words and all(w in text for w in self.place_words))
            ):
                return None
        return how


def _split(value: Any) -> list[str]:
    return [t.strip() for t in str(value or "").split(";") if t.strip()]


def _texts(lake: Path, registry: Registry, ds: Dataset) -> dict[tuple[str, str], str]:
    """Text held in another store that shares this source's primary entity (CROI's text), by key; used only to
    match. Empty when there is none or it can't be reached."""
    mine = {e.name for e in ds.semantic.entities if e.type == "primary"}
    out: dict[tuple[str, str], str] = {}
    for other in registry:
        if not any(e.type == "foreign" and e.name in mine for e in other.semantic.entities):
            continue
        if "abstract_text" not in other.contract.column_names:
            continue
        try:
            got = private.read_rows(lake, other)
        except private.PrivateRefused:
            continue
        for r in got.rows if got else []:
            out[(str(r.get("abstract_number")), str(r.get("year")))] = str(r.get("abstract_text") or "")
    return out


def _coverage(
    ds: Dataset, got: private.PrivateRows | None, span: tuple[int, int] | None
) -> dict[int, tuple[str, int]]:
    held: dict[int, tuple[bool, int]] = {}
    for m in got.manifests if got else []:
        part = str(m.get("partition", ""))
        if part.startswith("year=") and part[5:].isdigit():
            held[int(part[5:])] = (m.get("complete") is True, int(m.get("rows") or 0))
    years = sorted(set(catalogued_years(ds)) | set(held))
    if span is not None:
        years = [y for y in years if span[0] <= y <= span[1]] or list(range(span[0], span[1] + 1))
    out: dict[int, tuple[str, int]] = {}
    for y in years:
        if got is None:
            out[y] = (UNAVAILABLE, 0)
        elif y not in held:
            out[y] = (NOT_INDEXED, 0)
        else:
            out[y] = (COMPLETE if held[y][0] else INCOMPLETE, held[y][1])
    return out


def _conference(ds: Dataset) -> str:
    return ds.entry.source.conference_name or ds.entry.agency


def _provenance(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """(retrieved_at, read_from, sha256) of the bytes this row was read from."""
    if row.get("page_sha256"):
        return str(row.get("retrieved_at") or ""), str(row.get("page_url") or ""), str(row["page_sha256"])
    return (
        str(row.get("retrieved_at") or ""),
        str(row.get("source_url") or ""),
        str(row.get("raw_sha256") or ""),
    )


def search(
    registry: Registry,
    lake: Path,
    *,
    condition: str,
    geography: str | None = None,
    years: str | None = None,
    dataset: str | None = None,
) -> tuple[list[list[Any]], list[list[Any]], list[str]]:
    """Matching abstracts, the coverage of every source and year asked, and notes (one per source read)."""
    span = parse_years(years)
    query = Search.of(condition, geography)
    rows: list[list[Any]] = []
    coverage: list[list[Any]] = []
    notes: list[str] = []
    for ds in research_index(registry, dataset):
        try:
            got = private.read_rows(lake, ds)
        except private.PrivateRefused:
            got = None
        cov = _coverage(ds, got, span)
        for y, (state, n) in cov.items():
            coverage.append([ds.id, _conference(ds), y, state, n, MEANING[state]])
        if got is None:
            continue
        notes.append(
            f"{ds.id}: {ds.entry.licence.id}; {ds.entry.caveats[0] if ds.entry.caveats else ''}".rstrip("; ")
        )
        texts = _texts(lake, registry, ds)
        for r in got.rows:
            try:
                year = int(str(r.get("year")))
            except ValueError:
                continue
            if span is not None and not span[0] <= year <= span[1]:
                continue
            key = (str(r.get("abstract_number")), str(r.get("year")))
            title = str(r.get("title") or r.get("heading") or "")
            text = " ".join([title, str(r.get("abstract_text") or ""), texts.get(key, "")]).casefold()
            how = query.match(r, text)
            if how is None:
                continue
            when, read_from, sha = _provenance(r)
            rows.append([
                ds.id, _conference(ds), year, r.get("abstract_number"), title, r.get("authors"),
                r.get("session_title") or r.get("session_type"), r.get("doi_url") or r.get("page_url"),
                r.get("poster_url"), how, r.get("condition_tags"), r.get("design_tags"), r.get("data_source_tags"),
                r.get("geography_tags"), cov.get(year, (NOT_INDEXED, 0))[0], when, read_from, sha,
            ])  # fmt: skip
    rows.sort(key=lambda r: (-int(r[2]), str(r[0]), str(r[3] or "")))
    return rows, coverage, notes


def summary_notes(condition: str, rows: Iterable[list[Any]], coverage: Iterable[list[Any]]) -> list[str]:
    rows, coverage = list(rows), list(coverage)
    if not coverage:
        return [
            "This setup has no conference-abstract source in its research index, so nothing was searched. Search "
            "PubMed (and OpenEvidence if you use it), and the IDWeek abstracts in the Open Forum Infectious "
            "Diseases supplements."
        ]
    not_complete = sorted({f"{c[1]} {c[2]} ({c[3]})" for c in coverage if c[3] != COMPLETE})
    out = [
        f"{len(rows)} abstract(s) in the research index match '{condition}'. Each is an abstract, not a "
        "peer-reviewed paper; open its link for the abstract and poster on the publisher's site.",
        "Matches use our fixed word rules (condition tags) or the words of the question in the title or text; a "
        "missing tag doesn't mean the topic is absent. Abstract text is used to match and is never shown.",
    ]
    if not_complete:
        out.append(
            "Not every year asked is fully indexed, so no row for these is not 'no match': "
            + "; ".join(not_complete)
        )
    out.append("Search PubMed too: the index holds conference abstracts, not journal papers.")
    return out
