"""Harvest one conference year's DOI-published abstracts into the conference's private store.

    health-data-registry harvest-doi <conference> [--year YYYY] [--max-records N] [--no-enrich]

A conference is one catalog entry (portal ``doi``, ``private_store: true``): ``source.conference`` is its key and
``source.supplements`` say, per year, where its abstracts are in Crossref (ISSN, volume, issue, page and DOI
patterns), whether there is one DOI per abstract or one per congress, the text policy (by licence, or link only
for a subscriber-only supplement) and whether the route is verified (an unverified year harvests one record).
Adding a conference is adding a catalog entry, its contract and semantic model; no code. One run:

1. lists the supplement from Crossref (``/journals/<ISSN>/works``, paged by cursor, kept by the supplement's
   structural filters), stopping early at ``--max-records``;
2. looks the same DOIs up in OpenAlex and Europe PMC (unless ``--no-enrich``);
3. merges the three answers into one row per DOI (adapters/doi_abstracts.merge), tags it with our fixed word
   rules (research_tags.py), and writes the rows into ``lake/private/<source>/year=<year>.jsonl``: a run that
   read the whole supplement replaces the year's rows (a DOI that left the supplement goes); a sample upserts.
   When OpenAlex or Europe PMC couldn't be read, a stored row keeps what that API gave it on an earlier run
   (a missing value never replaces a present one), so a degraded run never makes the store worse.

Every response is kept hash-addressed in the source's private area (``lake/private/<source>/raw/<sha256>``,
owner-only, never the shared ``lake/raw/``: a response can carry text whose licence keeps it private), and every
attempt is a row in the fetch log (``fetch_log/source=<source>/``). Each row records the provenance of every API
that answered for it: ``source_url``/``raw_sha256``/``retrieved_at`` for the Crossref page that listed it, and
``openalex_*`` and ``europepmc_*`` for the enriching answers, so each value leads back to the bytes it came from.
robots.txt, each API's published rate limit and Retry-After are honoured (crawl.fetcher), and the time spent
waiting out crawl delays is capped (CRAWL_DELAY_BUDGET_S). Abstract text is stored only where its licence allows
(``abstract_tier``); otherwise the row is metadata and a link.

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
from health_data_registry.fetch import FetchRefused
from health_data_registry.registry import (
    Attribution,
    AttributionMissing,
    Dataset,
    Registry,
    Supplement,
    require_attribution,
)
from health_data_registry.store import FetchLog, RawFetch

Getter = Callable[[str], RawFetch]
# The most one harvest waits in total for crawl delays and rate limits: the largest catalogued supplement (about
# 2,500 IDWeek abstracts) is about 100 Europe PMC batches at robots.txt's 10 s, plus Crossref's pages and OpenAlex
# batches; an hour leaves room for that and stops a run that a huge Crawl-delay would otherwise stall.
CRAWL_DELAY_BUDGET_S = 3600.0
ENRICHERS: tuple[doi.ApiName, ...] = ("openalex", "europepmc")
# Per enriching API, the row columns that record the answer a row's values came from (hard rule 8).
PROVENANCE_COLUMNS: dict[str, tuple[str, str, str]] = {
    api: (f"{api}_source_url", f"{api}_raw_sha256", f"{api}_retrieved_at") for api in ENRICHERS
}
# What a stored row keeps from an enriching API that couldn't be read this run.
_TEXT_COLUMNS = ("abstract_text", "abstract_tier", "abstract_licence")
_ID_COLUMNS = ("pubmed_pmid", "pmcid")
# The columns every research-index row shares with the CROI index (croi_index.records on the CROI branch), so
# all conferences land in one index: the abstract's id, year, title, authors as published, session, links and
# our rule-based tags.
RESEARCH_INDEX_COLUMNS: tuple[str, ...] = (
    "abstract_number", "year", "title", "authors", "session_type", "session_title", "category", "page_url",
    "poster_url", "condition_tags", "design_tags", "data_source_tags", "geography_tags", "pubmed_pmid",
    "retrieved_at",
)  # fmt: skip


# The row columns that carry each attribution field a CC BY or CC0 abstract must keep to be shared publicly.
ROW_ATTRIBUTION = Attribution(
    authors="authors", title="title", journal="journal", doi_url="doi_url", licence="abstract_licence"
)


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
    skipped: list[str] = field(default_factory=list)  # an enriching API that couldn't be read, in plain words

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
        out += [f"note: {s}" for s in self.skipped]
        return out


class _Client:
    """Wraps the getter: keeps every response's bytes in the source's private area and counts requests per API."""

    def __init__(self, get: Getter, lake: Path, ds: Dataset, summary: HarvestSummary) -> None:
        self.get, self.lake, self.ds, self.summary = get, lake, ds, summary

    def __call__(self, api: str, url: str) -> tuple[bytes, Provenance]:
        self.summary.requests[api] = self.summary.requests.get(api, 0) + 1
        r = self.get(url)
        private.write_raw_private(self.lake, self.ds, [r])
        when = r.retrieved_at.astimezone(UTC).isoformat(timespec="seconds")
        return r.content, Provenance(url=r.url, sha256=r.sha256, retrieved_at=when)


def _batches(items: Sequence[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def row(
    a: doi.Abstract,
    *,
    sup: Supplement,
    prov: Provenance,
    conference_name: str,
    enriched: dict[str, Provenance] | None = None,
) -> dict[str, Any]:
    """The research-index row for one merged abstract (the contract's columns, nothing else).

    ``prov`` is the Crossref page that listed the DOI; ``enriched`` the OpenAlex and Europe PMC answers that gave
    a record for it, each recorded in its own provenance columns.

    A CC BY or CC0 abstract is public only with full attribution (registry.require_attribution); one that lacks
    any attribution field is refused the public tier and its text stays private.
    """
    tier: doi.Tier = "link_only" if sup.text_policy == "link_only" else a.tier
    link = f"https://doi.org/{a.doi}"
    out = {
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
    for api, (url_col, sha_col, when_col) in PROVENANCE_COLUMNS.items():
        got = (enriched or {}).get(api) if api in a.apis else None
        out[url_col] = got.url if got else None
        out[sha_col] = got.sha256 if got else None
        out[when_col] = got.retrieved_at if got else None
    if tier == "public":
        try:
            require_attribution(out, ROW_ATTRIBUTION)
        except AttributionMissing:
            out["abstract_tier"] = "private"
    return out


def keep_stored(
    new: dict[str, Any], old: dict[str, Any] | None, *, sup: Supplement, missed: set[str]
) -> dict[str, Any]:
    """A new row, with what an API that couldn't be read this run (``missed``) gave the stored row kept: its
    provenance, the PubMed ids and, unless the year is link only, the text with its tier and licence (taken
    together, never mixed). A value the new row has always wins."""
    if old is None or not missed:
        return new
    out = dict(new)
    kept = [api for api in ENRICHERS if api in missed and old.get(PROVENANCE_COLUMNS[api][0])]
    for api in kept:
        for c in PROVENANCE_COLUMNS[api]:
            out[c] = out.get(c) or old.get(c)
    for c in _ID_COLUMNS:
        out[c] = out.get(c) or old.get(c)
    if (
        out.get("abstract_text") is None
        and old.get("abstract_text")
        and sup.text_policy != "link_only"
        and old.get("abstract_tier") in ("public", "private")
    ):
        for c in _TEXT_COLUMNS:
            out[c] = old.get(c)
        out.update(research_tags.tag_record(str(out["title"]), str(out["abstract_text"])))
    apis = [a for a in doi.API_ORDER if a in str(out.get("metadata_sources") or "").split("; ") or a in kept]
    out["metadata_sources"] = "; ".join(apis)
    out["metadata_terms"] = "; ".join(doi.METADATA_TERMS[a] for a in apis)
    return out


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
        # Only this source's hosts: OpenAlex and Europe PMC are reachable from this fetcher alone.
        hosts = ds.entry.source.hosts
        robots = Robots(hosts, budget_s=CRAWL_DELAY_BUDGET_S, api_intervals=doi.MIN_INTERVAL_S)
        get = fetcher(registry, FetchLog(lake, ds.id), robots=robots, hosts=hosts)
    client = _Client(get, lake, ds, summary)

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
    enriched: dict[str, dict[str, Provenance]] = {}  # DOI -> API -> the answer that gave its record
    missed: set[str] = set() if enrich else set(ENRICHERS)
    if enrich and dois:
        # Crossref alone makes a row; an API that adds to it and can't be read now is noted, not fatal.
        enrichers = (
            ("openalex", "OpenAlex", doi.OPENALEX_BATCH, doi.openalex_url, doi.parse_openalex),
            ("europepmc", "Europe PMC", doi.EUROPEPMC_BATCH, doi.europepmc_url, doi.parse_europepmc),
        )
        for api, label, size, url_of, parse in enrichers:
            try:
                for batch in _batches(dois, size):
                    content, prov = client(api, url_of(batch))
                    for r in parse(content):
                        if r.doi in origin:
                            found.append(r)
                            enriched.setdefault(r.doi, {})[api] = prov
            except (FetchRefused, RuntimeError, doi.ApiLayoutError) as e:
                why = e.message if isinstance(e, FetchRefused) else str(e)
                missed.add(api)
                summary.skipped.append(
                    f"{label} could not be read ({why}); stored rows keep what it gave them before"
                )

    # 3. One row per DOI, into the private store.
    merged = doi.merge(found)
    name = ds.entry.source.conference_name or ds.entry.title
    partition = f"year={sup.year}"
    stored = private.stored_rows(lake, ds, partition) if missed else {}
    out = [
        keep_stored(
            row(a, sup=sup, prov=origin[d], conference_name=name, enriched=enriched.get(d)),
            stored.get((d,)),
            sup=sup,
            missed=missed,
        )
        for d, a in merged.items()
    ]
    for written in out:
        summary.tiers[written["abstract_tier"]] = summary.tiers.get(written["abstract_tier"], 0) + 1
        lic = written["abstract_licence"] or "none given"
        summary.licences[lic] = summary.licences.get(lic, 0) + 1
    summary.rows = len(out)
    if out:
        # A run that read the whole supplement replaces the year's rows; a sample only upserts.
        summary.stored_rows = private.write_rows(
            lake, ds, partition, out, complete=summary.complete, replace=summary.complete
        )
    return summary
