"""Fetch one catalogued source from its publisher into a lake: every attempt logged, raw bytes kept, one snapshot.

Used by `health-data-registry refresh` / `fetch` (the maintainers' lake) and by the server for sources whose
nothing else: a source whose licence forbids redistribution (WHO GHO) is never written to any lake; the server
reads it live (live.py).
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from health_data_registry.registry import Dataset, Registry
from health_data_registry.store import FetchLog, Manifest, RawFetch, build_snapshot, write_ndjson, write_raw

RETRIES = 4
FETCH_TIMEOUT_S = 180.0

Getter = Callable[[str], RawFetch]


class LiveOnly(ValueError):
    """The source's terms forbid storing it; the server reads it live instead."""


class PrivateTier(LiveOnly):
    """The source is kept in its own private store, built by its own command (e.g. index-croi)."""


def fetcher(
    registry: Registry, log: FetchLog | None = None, *, sleep: Callable[[float], None] = time.sleep
) -> Getter:
    """A getter for catalogued publisher URLs that retries slow publishers and logs every attempt."""
    from health_data_registry.fetch import FetchRefused, fetch

    hosts = registry.hosts

    def get(url: str) -> RawFetch:
        for attempt in range(RETRIES):
            when = datetime.now(UTC)
            try:
                r = fetch(
                    url, allowed_hosts=hosts, headers={"Accept": "application/json"}, timeout=FETCH_TIMEOUT_S
                )
            except FetchRefused as e:
                if log is not None:
                    log.record(url, ok=False, when=when, error=str(e))
                # A slow or briefly unreachable publisher is retried; a refused URL or host never is.
                if e.code not in ("timeout", "connect_failed") or attempt == RETRIES - 1:
                    raise
                sleep(2 ** (attempt + 1))
                continue
            if r.status != 200:
                msg = f"the publisher answered with status {r.status} instead of the data"
                if log is not None:
                    log.record(url, ok=False, when=when, http_status=r.status, size=len(r.content), error=msg)
                raise RuntimeError(f"{url}: {msg}")
            if log is not None:
                log.record(
                    url, ok=True, when=when, http_status=r.status, size=len(r.content), sha256=r.sha256
                )
            return RawFetch(url=r.final_url, content=r.content, sha256=r.sha256, retrieved_at=r.retrieved_at)
        raise RuntimeError(f"{url}: no response")  # pragma: no cover - the loop returns or raises

    return get


def refresh_dataset(ds: Dataset, lake: Path, get: Getter, *, max_rows: int | None = None) -> Manifest:
    """Page through one source, keep its raw bytes, and build its snapshot (partial when max_rows is set)."""
    from health_data_registry.adapters import pages

    if ds.entry.tier == "private":
        raise PrivateTier(
            f"{ds.id} is kept only in its private store (lake/private/{ds.id}/), never in the public lake; "
            + (
                "build it with `health-data-registry index-croi --year <year>`"
                if ds.entry.source.portal == "croi"
                else "it has no crawl command yet"
            )
        )
    if not ds.entry.licence.redistribution:
        raise LiveOnly(
            f"{ds.id} is read live from {ds.entry.publisher} only: its terms ({ds.entry.licence.id}) attach to "
            "anything derived, so no copy is ever stored"
        )
    lake.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        fetches, files = [], []
        fields = sorted({c.source_field for c in ds.contract.columns})
        for i, page in enumerate(pages(ds.entry, get, max_rows=max_rows, fields=fields)):
            write_raw(lake, [page.fetch])  # keep the bytes on disk now, not in memory
            f = page.fetch
            fetches.append(RawFetch(url=f.url, content=b"", sha256=f.sha256, retrieved_at=f.retrieved_at))
            nd = Path(tmp) / f"page-{i:05d}.ndjson"
            write_ndjson(nd, page.records)
            files.append(nd)
        return build_snapshot(lake, ds, fetches, files, partial=max_rows is not None)
