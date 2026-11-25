"""Live sources: restricted-licence data (e.g. WHO GHO, under WHO's dataset terms) served per request, never stored.

Share-alike terms would attach to anything derived from these sources, so nothing derived from them is ever
written to disk: no lake snapshot, no Parquet, no DuckDB file, no cache of the normalised table. The server
fetches the publisher's response over HTTPS when a question needs it, keeps the parsed records in memory for at
most LIVE_TTL_S seconds (a short-lived in-memory cache, so one question's several reads hit the publisher
once), and normalises them into an in-memory DuckDB table with the same contract-driven projection as the lake.
Every answer that uses them carries the publisher's attribution, the licence and its terms URL.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC
from typing import Any

import duckdb

from health_data_registry.registry import Dataset
from health_data_registry.store import Manifest, RawFetch, select_list

LIVE_TTL_S = 600.0  # how long parsed publisher records stay in memory; never written to disk


def is_live(ds: Dataset) -> bool:
    """A source whose terms forbid redistribution is served live, unless it is kept in its own private store
    (``hosting: private``, read through private.read_rows)."""
    return ds.entry.tier == "live"


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

    def fresh(self, now: float | None = None) -> bool:
        return ((now if now is not None else time.monotonic()) - self.fetched) < LIVE_TTL_S


def fetch_live(ds: Dataset, get: Any) -> LiveTable:
    """Page through the publisher in memory and describe the result; nothing touches the disk."""
    from health_data_registry.adapters import pages

    fields = sorted({c.source_field for c in ds.contract.columns})
    records: list[dict[str, Any]] = []
    fetches: list[RawFetch] = []
    for page in pages(ds.entry, get, fields=fields):
        fetches.append(page.fetch)
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
        notes=[attribution(ds)],
    )
    return LiveTable(records=records, manifest=m, fetched=time.monotonic())


def load_live(con: duckdb.DuckDBPyConnection, ds: Dataset, records: list[dict[str, Any]]) -> None:
    """Normalise records into an in-memory table named after the dataset.

    Refuses a file-backed connection: the source's terms forbid keeping a copy (ADR 0009), so this checks the
    connection itself rather than trusting the caller. A table that can't be normalised leaves nothing behind.
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
        con.execute(f"CREATE TEMP TABLE {q(ds.id)} AS SELECT\n  {select_list(ds.contract)}\nFROM {q(raw)}")  # noqa: S608 - quoted contract identifiers
    finally:
        con.execute(f"DROP TABLE IF EXISTS {q(raw)}")
