"""Live sources: restricted-licence data (e.g. WHO GHO, under WHO's dataset terms) served per request, never stored.

Share-alike terms would attach to anything derived from these sources, so nothing derived from them is ever
written to disk: no lake snapshot, no Parquet, no DuckDB file, no cache of the normalised table. The server
fetches the publisher's response over HTTPS when a question needs it, keeps the parsed records in memory for at
most LIVE_TTL_S seconds (a short-lived in-memory cache, so one question's several reads hit the publisher
once), and normalises them into an in-memory DuckDB table with the same contract-driven projection as the lake.
Every answer that uses them carries the publisher's attribution, the licence and its terms URL.

A dashboard read with the browser (``source.browser``) costs far more: one page load per catalogued view, at
least 10 s apart (crawl.Robots), so a six-view dashboard takes a few minutes to read. Its parsed records are kept
in memory for DASHBOARD_TTL_S instead, still never on disk, and every answer says so in its provenance notes, with
the time of the read. Only the first question in that time waits for the dashboard.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC
from typing import Any

import duckdb

from health_data_registry.registry import Dataset
from health_data_registry.staging import Staged, stage_checked
from health_data_registry.store import Manifest, RawFetch

LIVE_TTL_S = 600.0  # how long parsed publisher records stay in memory; never written to disk
# How long a browser-read dashboard's records stay in memory (never on disk): its read takes minutes, its data
# change daily at most (weekly, annual or archived for the four Michigan dashboards).
DASHBOARD_TTL_S = 3600.0


def ttl_s(ds: Dataset) -> float:
    """How long this source's live records are kept in memory."""
    return DASHBOARD_TTL_S if ds.entry.source.browser else LIVE_TTL_S


def kept_note(ds: Dataset, retrieved_at: str) -> str:
    """The provenance note that says how long a live read is reused, in plain words."""
    minutes = round(ttl_s(ds) / 60)
    why = (
        "a dashboard takes a few minutes to read, so "
        if ds.entry.source.browser
        else "so one question's several lookups read the publisher once, "
    )
    return (
        f"Read live from the publisher at {retrieved_at[:16].replace('T', ' ')} UTC; {why}this read is kept in this "
        f"server's memory (never on disk) for up to {minutes} minutes and reused by answers in that time."
    )


def is_live(ds: Dataset) -> bool:
    """A source whose terms forbid redistribution is served live only."""
    return not ds.entry.licence.redistribution


def attribution(ds: Dataset) -> str:
    e = ds.entry
    return (
        f"Data from {e.publisher}, used under {_licence_name(e.licence.id)} ({e.licence.terms_url}); fetched live "
        f"from the publisher for this answer and not stored. Cite as: {e.citation}"
    )


def _licence_name(licence_id: str) -> str:
    return {
        "CC-BY-NC-SA-3.0-IGO": "CC BY-NC-SA 3.0 IGO",
        "WHO-DATASET-TERMS": "WHO's Terms and Conditions for datasets",
    }.get(licence_id.upper(), licence_id)


@dataclass(frozen=True)
class LiveTable:
    records: list[dict[str, Any]]
    manifest: Manifest
    fetched: float  # time.monotonic() when fetched
    dashboard: bool = False  # read with the browser: kept for DASHBOARD_TTL_S rather than LIVE_TTL_S

    def fresh(self, now: float | None = None) -> bool:
        ttl = DASHBOARD_TTL_S if self.dashboard else LIVE_TTL_S
        return ((now if now is not None else time.monotonic()) - self.fetched) < ttl


def fetch_live(ds: Dataset, get: Any) -> LiveTable:
    """Page through the publisher in memory and describe the result; nothing touches the disk."""
    from health_data_registry.adapters import pages
    from health_data_registry.crawl import override_notes

    fields = sorted({c.source_field for c in ds.contract.columns})
    records: list[dict[str, Any]] = []
    fetches: list[RawFetch] = []
    layouts: dict[str, str] = {}
    for page in pages(ds.entry, get, fields=fields):
        fetches.append(page.fetch)
        layouts.update(page.layout)
        records += [{k: (None if v is None else str(v)) for k, v in r.items()} for r in page.records]
    if not fetches:
        raise ValueError(f"{ds.id}: the publisher returned nothing")
    retrieved = max(f.retrieved_at for f in fetches).astimezone(UTC)
    digest = hashlib.sha256("".join(f.sha256 for f in fetches).encode()).hexdigest()[:12]
    m = Manifest(
        dataset=ds.id,
        snapshot_id=f"live-{retrieved:%Y%m%dT%H%M%SZ}-{digest}",
        contract_version=ds.contract.version,
        semantic_version=ds.semantic.version,
        catalog_status=ds.entry.status,
        retrieved_at=retrieved.isoformat(),
        source_urls=[f.url for f in fetches],
        raw_sha256=[f.sha256 for f in fetches],
        row_count=len(records),
        licence=ds.entry.licence.id,
        redistribution=False,
        partial=False,
        origin="publisher",
        notes=[attribution(ds), kept_note(ds, retrieved.isoformat()), *override_notes(get)],
        layouts=layouts,
    )
    return LiveTable(records=records, manifest=m, fetched=time.monotonic(), dashboard=ds.entry.source.browser)


def load_live(con: duckdb.DuckDBPyConnection, ds: Dataset, records: list[dict[str, Any]]) -> Staged:
    """Stage records into an in-memory silver table named after the dataset (the same step as the lake).

    Refuses a file-backed connection: the source's terms forbid keeping a copy (ADR 0009), so this checks the
    connection itself rather than trusting the caller. A table that can't be normalised leaves nothing behind.

    The checks are the lake's, on purpose: an empty required value (a row with no country or year) or a declared
    field that no record carries refuses the whole source for this session, as it would a stored load. Answering
    from the other rows would drop rows from totals without saying so (hard rules 1 and 2); the refusal tells the
    clinician the source is unavailable instead.
    """
    where = con.execute(
        "SELECT path FROM duckdb_databases() WHERE database_name = current_database()"
    ).fetchone()
    if where is None or where[0] is not None:
        raise ValueError(f"{ds.id} is never written to disk: load_live needs an in-memory connection")
    fields = sorted({c.source_field for c in ds.contract.columns})
    raw = f"_live_raw_{ds.id}"
    con.execute("SET max_temp_directory_size = '0B'")  # never spill WHO-derived rows to disk

    def q(ident: str) -> str:
        return '"' + ident.replace('"', '""') + '"'

    con.execute(f"CREATE TEMP TABLE {q(raw)} ({', '.join(q(f) + ' VARCHAR' for f in fields)})")
    try:
        if records:
            marks = ", ".join("?" for _ in fields)
            con.executemany(
                f"INSERT INTO {q(raw)} VALUES ({marks})",  # noqa: S608 - quoted contract identifiers, bound values
                [[r.get(f) for f in fields] for r in records],
            )
        present = {k for r in records for k in r} or None
        return stage_checked(con, ds.contract, q(raw), table=ds.id, temp=True, present=present)

    finally:
        con.execute(f"DROP TABLE IF EXISTS {q(raw)}")
