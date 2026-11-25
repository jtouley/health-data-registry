"""A listing crawl into private stores: the generic runner for private-tier sources of type html_table, listing.

    health-data-registry crawl-listing <source> [--years 2022-2026]

A source whose terms forbid public copies (``private_store: true``) and that is read as a paginated listing of
item pages (ADR 0016: ``type: html_table``, ``shape: listing``) is crawled by the core's resumable listing runner
(listing.py) and stored only in its own private store (private.py), never in the public lake or a release:

- **One crawl, several stores.** Private sources whose ``source`` blocks are identical are one crawl: every item
  page is read once, and each source stores its own contract's columns of it (a metadata index, and the text
  kept apart from it). The crawl's state, its pace and the raw pages it read live under the private area
  (``lake/private/_crawl/``), never in the public lake.
- **One partition per year, complete or nothing.** Each year is crawled on its own, in order. A year is stored
  only when the items crawled equal the listing's own total, every item's staged row is new to its key, and the
  total is not zero; otherwise nothing of it is stored, its checkpoint is kept for the next run, and the run stops.
  Each store's coverage record (``_coverage.json``) says which years are complete, so find_prior_work never reads
  an incomplete year as "nothing presented".
- **Staged as everywhere else.** The raw item records (as printed, plus each page's link, sha256 and fetch time,
  and the listing year) go through the source's declared staging extensions and its contract's projection
  (staging.stage_entry), exactly as the lake and dbt stage them; only contract columns reach a store.
- **Crash-safe.** Item data are written before their checkpoint (listing.py); a year's rows are written whole
  (temporary file, fsync, rename) before its coverage record. A crash at any point leaves at worst a year that is
  restaged from the kept items on the next run, with identical rows.

Two item pages that stage to the same key (a duplicate number in one year) are a collision: the first is kept,
the year stays incomplete, and the maintainer decides how to key them. This never runs inside the MCP server.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from health_data_registry import private
from health_data_registry.registry import CatalogEntry, Dataset, Registry
from health_data_registry.source_config import HtmlTableConfig

CRAWL_DIR = "_crawl"
PROVENANCE = ("page_url", "page_sha256", "retrieved_at", "year")
# Stops the next run carries on from by itself; any other stop needs a maintainer (a changed page, a collision).
RESUMABLE = frozenset({"crawl_incomplete", "host_locked", "fetch_refused"})


class NotAListing(ValueError):
    """The source is not a private-tier html_table listing, so this runner does not build it."""


@dataclass
class YearRun:
    source: str
    year: int
    total: int | None
    crawled: int
    stored: int
    collisions: int
    complete: bool
    stopped: str | None = None  # why the year isn't complete, in plain words
    code: str | None = None  # crawl_incomplete, collision, empty_listing, layout_changed, host_locked, ...

    @property
    def ok(self) -> bool:
        return self.complete

    def lines(self) -> list[str]:
        out = [
            f"{self.source} {self.year}: {self.stored} stored, {self.crawled} of "
            f"{self.total if self.total is not None else '?'} item pages read"
        ]
        if self.complete:
            out.append("the year is complete")
        elif self.code in RESUMABLE:
            out.append("run the same command again to carry on")
        else:
            out.append("a maintainer needs to look before this year can go on")
        if self.stopped:
            out.append(f"stopped: {self.stopped}")
        return out


def crawl_root(lake: Path) -> Path:
    return private.private_root(lake) / CRAWL_DIR


def listing_config(entry: CatalogEntry) -> HtmlTableConfig:
    cfg = entry.source.config()
    if entry.tier != "private" or not isinstance(cfg, HtmlTableConfig) or cfg.listing is None:
        raise NotAListing(
            f"{entry.id} is not a private-store listing (type html_table, shape listing, private_store: true), "
            "so crawl-listing does not build it"
        )
    return cfg


def group(registry: Registry, ds: Dataset) -> list[Dataset]:
    """The private sources that share this source's crawl (identical source blocks), by id; the first owns it."""
    return sorted(
        (d for d in registry if d.entry.tier == "private" and d.entry.source == ds.entry.source),
        key=lambda d: d.id,
    )


def years_of(entry: CatalogEntry) -> list[int]:
    cfg = listing_config(entry)
    assert cfg.listing is not None  # noqa: S101 - listing_config checked it
    return list(range(cfg.listing.years.first, cfg.listing.years.last + 1))


def one_year(entry: CatalogEntry, year: int) -> CatalogEntry:
    """The entry with its listing narrowed to one year: one partition, crawled and judged on its own."""
    cfg = listing_config(entry)
    assert cfg.listing is not None  # noqa: S101 - listing_config checked it
    listing = cfg.listing.model_copy(
        update={"years": cfg.listing.years.model_copy(update={"first": year, "last": year})}
    )
    source = entry.source.model_copy(update={"html_table": cfg.model_copy(update={"listing": listing})})
    return entry.model_copy(update={"source": source})


def raw_rows(pages: Iterable[Any], year: int) -> list[dict[str, str | None]]:
    """Each item's raw record as printed, with the page's own provenance and the listing year."""
    out = []
    for page in pages:
        f = page.fetch
        meta = {
            "page_url": f.url,
            "page_sha256": f.sha256,
            "retrieved_at": f.retrieved_at.astimezone(UTC).isoformat(timespec="seconds"),
            "year": str(year),
        }
        for rec in page.records:
            clash = sorted(set(rec) & set(meta))
            if clash:
                raise NotAListing(f"an item record already has {', '.join(clash)}, which the runner adds")
            out.append({**{k: None if v is None else str(v) for k, v in rec.items()}, **meta})
    return out


def stage_rows(ds: Dataset, rows: Sequence[dict[str, str | None]]) -> list[dict[str, Any]]:
    """The rows as this source's contract stages them (its staging extensions, then the typed projection)."""
    import duckdb

    from health_data_registry.staging import stage_entry

    present = sorted({k for r in rows for k in r})
    may_be_empty = sorted({c.source_field for c in ds.contract.columns if c.may_be_empty} - set(present))
    cols = [*present, *may_be_empty]
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE TABLE raw_items (" + ", ".join(f'"{c}" VARCHAR' for c in cols) + ")")
        if rows:
            marks = ", ".join("?" for _ in cols)
            con.executemany(
                f"INSERT INTO raw_items VALUES ({marks})",  # noqa: S608 - placeholders only
                [[r.get(c) for c in cols] for r in rows],
            )
        stage_entry(con, ds.entry, ds.contract, "raw_items", table="staged", temp=True, present=set(present))
        cur = con.execute("SELECT * FROM staged")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]
    finally:
        con.close()


def _first_per_key(ds: Dataset, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    keys = private.primary_key(ds)
    kept: dict[tuple[str, ...], dict[str, Any]] = {}
    for r in rows:
        kept.setdefault(tuple(str(r[k]) for k in keys), r)
    return list(kept.values()), len(rows) - len(kept)


def crawl_into_private(
    registry: Registry,
    lake: Path,
    source_id: str,
    years: Iterable[int] | None = None,
    *,
    get: Any = None,
) -> list[YearRun]:
    """Crawl (or resume) a private listing source year by year, stopping at the first year that isn't complete."""
    from health_data_registry.access import CrawlStopped, ScopeRefused
    from health_data_registry.adapters import LayoutError, pages
    from health_data_registry.adapters.extensions import ExtensionRefused
    from health_data_registry.fetch import FetchRefused
    from health_data_registry.listing import CrawlIncomplete, ListingState
    from health_data_registry.staging import StagingRefused

    ds = registry.get(source_id)
    private.require_private(ds)
    listing_config(ds.entry)
    members = group(registry, ds)
    owner = members[0]
    allowed = years_of(owner.entry)
    wanted = sorted(set(allowed if years is None else years))
    outside = [y for y in wanted if y not in allowed]
    if outside:
        raise ValueError(
            f"{source_id} lists the years {allowed[0]}-{allowed[-1]}; {', '.join(map(str, outside))} is outside them"
        )
    if get is None:
        from health_data_registry.crawl import fetcher
        from health_data_registry.store import FetchLog

        get = fetcher(registry, FetchLog(lake, owner.id))
    runs: list[YearRun] = []
    for year in wanted:
        partition = f"year={year}"
        entry = one_year(owner.entry, year)
        try:
            got = list(pages(entry, get, lake=crawl_root(lake)))
        except CrawlIncomplete as e:
            runs.append(_incomplete(lake, members, owner, source_id, year, str(e), e.code))
            break
        except CrawlStopped as e:
            runs.append(_incomplete(lake, members, owner, source_id, year, str(e), e.code))
            break
        except (LayoutError, ExtensionRefused, ScopeRefused) as e:
            runs.append(_incomplete(lake, members, owner, source_id, year, str(e), "layout_changed"))
            break
        except FetchRefused as e:
            where = f" (it pointed to {e.location})" if getattr(e, "location", None) else ""
            runs.append(
                _incomplete(lake, members, owner, source_id, year, f"{e.message}{where}", "fetch_refused")
            )
            break
        ck = ListingState(crawl_root(lake), owner.id).load(str(year), 1)
        raw = raw_rows(got, year)
        try:
            staged = {d.id: stage_rows(d, raw) if raw else [] for d in members}
        except (StagingRefused, ExtensionRefused) as e:
            runs.append(_incomplete(lake, members, owner, source_id, year, str(e), "layout_changed"))
            break
        except (
            Exception
        ) as e:  # a staging step's own refusal (SQL error()): the page isn't laid out as declared
            import duckdb

            if not isinstance(e, duckdb.Error):
                raise
            why = str(e).split("Error: ", 1)[-1].strip()
            runs.append(_incomplete(lake, members, owner, source_id, year, why, "layout_changed"))
            break
        total = ck.total
        collisions = 0
        stored = 0
        for d in members:
            rows, dupes = _first_per_key(d, staged[d.id])
            collisions = max(collisions, dupes)
            stored = len(rows)
            complete = total is not None and total > 0 and dupes == 0 and len(rows) == total
            private.write_rows(lake, d, partition, rows, complete=complete, replace=True)  # data first
        complete = total is not None and total > 0 and collisions == 0 and stored == total
        coverage = {"total": total, "done": len(got), "stored": stored, "collisions": collisions,
                    "complete": complete}  # fmt: skip
        for d in members:
            private.write_coverage(lake, d, {partition: coverage})  # coverage last
        if complete:
            runs.append(YearRun(source_id, year, total, len(got), stored, 0, True))
            continue
        why, code = (
            (f"{collisions} item page(s) share a key with another page, so the first was kept; the maintainer "
             "decides how to key them before this year can be complete", "collision")
            if collisions
            else ("the listing says 0 results for this year", "empty_listing")
            if not total
            else (f"{stored} rows were stored against the listing's {total}", "crawl_incomplete")
        )  # fmt: skip
        runs.append(YearRun(source_id, year, total, len(got), stored, collisions, False, why, code))
        break
    return runs


def _incomplete(
    lake: Path, members: list[Dataset], owner: Dataset, source_id: str, year: int, why: str, code: str
) -> YearRun:
    """A year that stopped before it was complete: its checkpoint kept, nothing stored, coverage says so."""
    from health_data_registry.adapters import LayoutError
    from health_data_registry.listing import ListingState

    partition = f"year={year}"
    try:
        ck = ListingState(crawl_root(lake), owner.id).load(str(year), 1)
        total, crawled = ck.total, len(ck.done)
    except (LayoutError, ValueError, OSError):
        total, crawled = None, 0
    held = len(private.stored_rows(lake, owner, partition))
    if total is not None or crawled:
        coverage = {"total": total, "done": crawled, "stored": held, "collisions": 0, "complete": False}
        for d in members:
            private.write_coverage(lake, d, {partition: coverage})
    return YearRun(source_id, year, total, crawled, held, 0, False, why, code)
