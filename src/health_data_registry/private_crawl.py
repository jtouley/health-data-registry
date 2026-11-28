"""crawl-private: a private-tier listing source crawled in slices into its own private store (lake/private/<id>/).

A listing crawl of a private-tier source (the CROI abstract index) runs for days at the site's pace, so it runs in
slices: each run reads at most ``max_requests`` new item pages, resuming from the listing crawl's own checkpoints
(listing.py: data first, checkpoint last, one crawler per host, the persisted pace). After each slice, every year
read so far is written to the private store as one partition (``year=<year>``), marked complete only when the year
matched its listing's own total; a year still being read is written as incomplete, and find_prior_work says so.

One crawl fills several stores: every private-tier source whose contract says ``storedFrom: <this source>`` gets
the same pages projected onto its own contract's columns (the CROI abstract text, kept apart from its metadata),
so each page is fetched once. A store's rows carry only its contract's columns (private.write_rows refuses any
other), each from the record field its contract names (``sourceField``), plus two things the crawl knows:

- ``year``: the listing year the item was read under (the crawl checks the item's own link names the same year);
- the ``*_tags`` columns: our fixed word rules over the heading and text (research_tags.py), never a model.

Nothing here fetches on its own: every request goes through the source's listing crawl and its fetcher.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from health_data_registry import private, research_tags
from health_data_registry.adapters import Fetcher, method_digest, pages
from health_data_registry.listing import CrawlIncomplete, ListingState, _partition_name, partitions
from health_data_registry.registry import Dataset, Registry
from health_data_registry.source_config import HtmlTableConfig, ListingConfig

TAG_COLUMNS = ("condition_tags", "design_tags", "data_source_tags", "geography_tags")
TEXT_FIELDS = ("heading", "title", "abstract_text")  # the record fields the tag rules read, when present


class NotCrawlable(ValueError):
    """The source is not a private-tier listing crawled by this command."""


@dataclass
class Summary:
    source: str
    stores: list[str]
    years: dict[int, dict[str, Any]] = field(default_factory=dict)  # year -> read, listed, complete
    stopped: str | None = None  # why the crawl stopped short, in plain words (None: every year complete)

    def lines(self) -> list[str]:
        out = [f"crawl-private {self.source}: stores {', '.join(self.stores)}"]
        for year, y in sorted(self.years.items()):
            state = "the year is complete" if y["complete"] else "not complete yet"
            out.append(f"  {year}: {y['read']} abstracts read of {y['listed'] or '?'} listed; {state}")
        if self.stopped:
            out.append(f"  stopped short: {self.stopped}")
        return out


def stored_from(registry: Registry, source_id: str) -> list[Dataset]:
    """The private-tier sources filled from ``source_id``'s crawl (their contracts say ``storedFrom``)."""
    out = []
    for ds in registry:
        if ds.id == source_id or ds.entry.tier != "private":
            continue
        top = private.contract_terms(ds)
        if top.get("storedFrom") == source_id:
            out.append(ds)
    return out


def _listing(ds: Dataset) -> ListingConfig:
    if ds.entry.tier != "private":
        raise NotCrawlable(f"{ds.id} is not kept in a private store; refresh reads it")
    cfg = ds.entry.source.config()
    if not isinstance(cfg, HtmlTableConfig) or cfg.listing is None:
        raise NotCrawlable(f"{ds.id} is not a listing of item pages, so crawl-private doesn't read it")
    filled_by = private.contract_terms(ds).get("storedFrom")
    if filled_by:
        raise NotCrawlable(f"{ds.id} is filled by {filled_by}'s crawl; run crawl-private {filled_by}")
    if cfg.listing.sessions:
        raise NotCrawlable(f"{ds.id} is listed by session; a private store keeps one partition per year")
    return cfg.listing


def row_for(ds: Dataset, record: dict[str, Any], year: int) -> dict[str, Any]:
    """One store row: each contract column from the record field it names, the listing year, and our tags."""
    words = [str(record[f]) for f in TEXT_FIELDS if record.get(f)]
    tags = research_tags.tag_record(words[0] if words else "", "\n".join(words[1:]) or None)
    row: dict[str, Any] = {}
    for c in ds.contract.columns:
        if c.name == "year":
            row[c.name] = year
        elif c.name in TAG_COLUMNS:
            row[c.name] = tags[c.name] or None
        else:
            row[c.name] = record.get(c.source_field)
    return row


def crawl_private(
    registry: Registry,
    source_id: str,
    lake: Path,
    fetcher: Fetcher,
    *,
    max_requests: int | None = None,
) -> Summary:
    """Crawl one slice of ``source_id`` (at most ``max_requests`` new item pages), then write every year read so
    far to its private store and to each store filled from it."""
    ds = registry.get(source_id)
    cfg = _listing(ds)
    targets = [ds, *stored_from(registry, source_id)]
    state = ListingState(lake, ds.id)
    method = method_digest(ds.entry)
    parts = [(y, s, _partition_name(y, s)) for y, s in partitions(cfg)]
    before = sum(len(state.load(name, cfg.first_page, method).done) for _y, _s, name in parts)
    budget = None if max_requests is None else before + max_requests
    summary = Summary(source=ds.id, stores=[t.id for t in targets])
    try:
        for _page in pages(ds.entry, fetcher, max_rows=budget, lake=lake):
            pass  # the crawl keeps each item in its checkpoint folder; the stores are written from there below
    except CrawlIncomplete as e:
        summary.stopped = str(e)
    for year, _session, name in parts:
        ck = state.load(name, cfg.first_page, method)
        if not ck.done:
            continue
        records = [r for item in ck.done.values() for r in state.item(name, item)[1]]
        y = summary.years.setdefault(year, {"read": 0, "listed": 0, "complete": True})
        y["read"] += len(ck.done)
        y["listed"] += ck.total or 0
        y["complete"] = y["complete"] and ck.complete
        for target in targets:
            rows = [row_for(target, r, year) for r in records]
            private.write_rows(lake, target, f"year={year}", rows, complete=ck.complete, replace=True)
    return summary
