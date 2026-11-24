"""Harvest one conference year's DOI-published abstracts into the conference's private store.

    health-data-registry harvest-doi <conference> [--year YYYY] [--max-records N] [--no-enrich]

A conference is one catalog entry (portal ``doi``, hosting: private): ``source.conference`` is its key and
``source.supplements`` say, per year, where its abstracts are in Crossref (ISSN, volume, issue, page and DOI
patterns), whether there is one DOI per abstract or one per congress, the text policy (by licence, or link only
for a subscriber-only supplement) and whether the route is verified (an unverified year harvests one record).
Adding a conference is adding a catalog entry, its contract and semantic model; no code. One run:

1. lists the supplement from Crossref (``/journals/<ISSN>/works``, paged by cursor, kept by the supplement's
   structural filters), stopping early at ``--max-records``;
2. looks the same DOIs up in OpenAlex and Europe PMC (unless ``--no-enrich``);
3. merges the three answers into one row per DOI (adapters/doi_abstracts.merge), tags it with our fixed word
   rules (research_tags.py), and upserts the rows into ``lake/private/<source>/year=<year>.jsonl``.

Every response is kept hash-addressed in the lake (``raw/<sha256>``) and every attempt is a row in the fetch
log (``fetch_log/source=<source>/``), so each row's ``raw_sha256`` and ``source_url`` lead back to the bytes it
came from. robots.txt, each API's published rate limit and Retry-After are honoured (crawl.fetcher). Abstract
text is stored only where its licence allows (``abstract_tier``); otherwise the row is metadata and a link.

It runs from the command line, the Python API or a workflow script, never inside the MCP server.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from health_data_registry import private, research_tags
from health_data_registry.adapters import doi_abstracts as doi
from health_data_registry.registry import Dataset, Registry, Supplement
from health_data_registry.store import FetchLog, RawFetch, write_raw

Getter = Callable[[str], RawFetch]
# The columns every research-index row shares with the CROI index (croi_index.records on the CROI branch), so
# all conferences land in one index: the abstract's id, year, title, authors as published, session, links and
# our rule-based tags.
RESEARCH_INDEX_COLUMNS: tuple[str, ...] = (
    "abstract_number", "year", "title", "authors", "session_type", "session_title", "category", "page_url",
    "poster_url", "condition_tags", "design_tags", "data_source_tags", "geography_tags", "pubmed_pmid",
    "retrieved_at",
)  # fmt: skip


class HarvestRefused(ValueError):
    """The harvest can't start (unknown conference or year); the message is plain words."""


def conferences(registry: Registry) -> dict[str, Dataset]:
    """Every DOI-harvested conference in the catalog, by its key: a conference is one catalog entry."""
    return {
        ds.entry.source.conference: ds
        for ds in registry
        if ds.entry.source.portal == "doi" and ds.entry.source.conference
    }


def conference_dataset(registry: Registry, conference: str) -> Dataset:
    known = conferences(registry)
    if conference not in known:
        raise HarvestRefused(
            f"{conference!r} is not a DOI-harvested conference; choose one of {', '.join(sorted(known))}"
        )
    return known[conference]


def supplement(ds: Dataset, year: int | None) -> Supplement:
    """The catalog's supplement for ``year``, or the latest year when ``year`` is None."""
    sups = sorted(ds.entry.source.supplements, key=lambda s: s.year)
    if not sups:
        raise HarvestRefused(f"{ds.id} lists no supplements in its catalog entry")
    if year is None:
        return sups[-1]
    for s in sups:
        if s.year == year:
            return s
    years = ", ".join(str(s.year) for s in sups)
    raise HarvestRefused(f"{ds.id} has no supplement catalogued for {year}; catalogued years: {years}")


@dataclass
class Provenance:
    url: str
    sha256: str
    retrieved_at: str


@dataclass
class HarvestSummary:
    source: str
    year: int
    listed: int = 0  # DOIs Crossref listed for the supplement this run
    rows: int = 0  # rows written (one per DOI with a title)
    stored_rows: int = 0  # rows now in the year's partition (earlier runs included)
    requests: dict[str, int] = field(default_factory=dict)
    tiers: dict[str, int] = field(default_factory=dict)
    licences: dict[str, int] = field(default_factory=dict)
    crossref_total: int | None = None  # works in the publication-date window, before the supplement filters
    complete: bool = False  # every page of the window was read (no --max-records stop)
    stopped: str | None = None

    def lines(self) -> list[str]:
        reqs = ", ".join(f"{k} {v}" for k, v in sorted(self.requests.items())) or "none"
        out = [
            f"{self.source} {self.year}: {self.listed} DOIs listed, {self.rows} rows written "
            f"({self.stored_rows} in the year's store); requests: {reqs}",
            "abstract text: " + (", ".join(f"{k} {v}" for k, v in sorted(self.tiers.items())) or "none"),
            "licences: " + (", ".join(f"{k} {v}" for k, v in sorted(self.licences.items())) or "none"),
            "the supplement was read in full"
            if self.complete
            else "a sample: the supplement was not read in full",
        ]
        if self.stopped:
            out.append(f"stopped: {self.stopped}")
        return out


class _Client:
    """Wraps the getter: keeps every response's bytes in the lake and counts requests per API."""

    def __init__(self, get: Getter, lake: Path, summary: HarvestSummary) -> None:
        self.get, self.lake, self.summary = get, lake, summary

    def __call__(self, api: str, url: str) -> tuple[bytes, Provenance]:
        self.summary.requests[api] = self.summary.requests.get(api, 0) + 1
        r = self.get(url)
        write_raw(self.lake, [r])
        when = r.retrieved_at.astimezone(UTC).isoformat(timespec="seconds")
        return r.content, Provenance(url=r.url, sha256=r.sha256, retrieved_at=when)


def _batches(items: Sequence[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def row(a: doi.Abstract, *, sup: Supplement, prov: Provenance, conference_name: str) -> dict[str, Any]:
    """The research-index row for one merged abstract (the contract's columns, nothing else)."""
    tier: doi.Tier = "link_only" if sup.text_policy == "link_only" else a.tier
    link = f"https://doi.org/{a.doi}"
    return {
        "doi": a.doi,
        "conference": conference_name,
        "year": sup.year,
        "granularity": sup.granularity,
        # A congress-level record is the whole abstract book: it has no abstract number.
        "abstract_number": a.abstract_number if sup.granularity == "abstract" else None,
        "title": a.title,
        "authors": a.authors,
        "session_type": None,
        "session_title": None,
        "category": None,
        "page_url": link,
        "poster_url": None,
        "journal": a.journal,
        "volume": a.volume,
        "issue": a.issue,
        "pages": a.pages,
        "published": a.published,
        "doi_url": link,
        **research_tags.tag_record(a.title, a.abstract),
        "abstract_text": a.abstract if tier != "link_only" else None,
        "abstract_tier": tier,
        "abstract_licence": a.licence,
        "metadata_sources": "; ".join(a.apis),
        "metadata_terms": "; ".join(doi.METADATA_TERMS[api] for api in a.apis),
        "pubmed_pmid": a.pmid,
        "pmcid": a.pmcid,
        "source_url": prov.url,
        "raw_sha256": prov.sha256,
        "retrieved_at": prov.retrieved_at,
    }


def harvest(
    registry: Registry,
    lake: Path,
    conference: str,
    *,
    year: int | None = None,
    max_records: int | None = None,
    enrich: bool = True,
    get: Getter | None = None,
) -> HarvestSummary:
    """Harvest one conference year into its private store. See the module doc."""
    from health_data_registry.crawl import Robots, fetcher

    ds = conference_dataset(registry, conference)
    private.require_private(ds)
    sup = supplement(ds, year)
    if max_records is not None and max_records < 1:
        raise HarvestRefused("--max-records must be at least 1")
    if not sup.verified:
        max_records = 1  # an unverified route is checked with one record, never harvested in full
    summary = HarvestSummary(source=ds.id, year=sup.year)
    if get is None:
        robots = Robots(registry.hosts, budget_s=float("inf"), min_interval_s=doi.MIN_INTERVAL_S)
        get = fetcher(registry, FetchLog(lake, ds.id), robots=robots)
    client = _Client(get, lake, summary)

    # 1. Crossref lists the supplement.
    listed: list[doi.ApiRecord] = []
    origin: dict[str, Provenance] = {}
    cursor: str | None = "*"
    rows = doi.CROSSREF_ROWS if max_records is None else min(doi.CROSSREF_ROWS, max(max_records * 20, 100))
    while cursor is not None:
        content, prov = client("crossref", doi.crossref_url(sup, cursor, rows))
        page = doi.parse_crossref(content, sup)
        summary.crossref_total = page.total
        for rec in page.records:
            if rec.doi not in origin:
                origin[rec.doi] = prov
                listed.append(rec)
        if max_records is not None and len(origin) >= max_records:
            listed = [r for r in listed if r.doi in set(list(origin)[:max_records])]
            origin = {k: origin[k] for k in list(origin)[:max_records]}
            break
        cursor = page.next_cursor
    else:
        summary.complete = True
    dois = list(origin)
    summary.listed = len(dois)

    # 2. OpenAlex and Europe PMC for the same DOIs.
    found: list[doi.ApiRecord] = list(listed)
    if enrich and dois:
        for batch in _batches(dois, doi.OPENALEX_BATCH):
            content, _ = client("openalex", doi.openalex_url(batch))
            found += [r for r in doi.parse_openalex(content) if r.doi in origin]
        for batch in _batches(dois, doi.EUROPEPMC_BATCH):
            content, _ = client("europepmc", doi.europepmc_url(batch))
            found += [r for r in doi.parse_europepmc(content) if r.doi in origin]

    # 3. One row per DOI, into the private store.
    merged = doi.merge(found)
    name = ds.entry.source.conference_name or ds.entry.title
    out = [row(a, sup=sup, prov=origin[d], conference_name=name) for d, a in merged.items()]
    for r in out:
        summary.tiers[r["abstract_tier"]] = summary.tiers.get(r["abstract_tier"], 0) + 1
        lic = r["abstract_licence"] or "none given"
        summary.licences[lic] = summary.licences.get(lic, 0) + 1
    summary.rows = len(out)
    if out:
        summary.stored_rows = private.write_rows(lake, ds, f"year={sup.year}", out)
    return summary
