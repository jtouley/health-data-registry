"""Source adapters: one per publisher portal. Each turns a catalog entry into raw fetches and records.

Adapters never change a value. They page through the publisher's API, keep every response's bytes for
the raw store, and hand back records keyed by the publisher's own field names; the contract does the rest.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from health_data_registry.registry import CatalogEntry
from health_data_registry.store import RawFetch

Fetcher = Callable[[str], RawFetch]


@dataclass(frozen=True)
class Page:
    fetch: RawFetch
    records: list[dict[str, Any]]


def pages(
    entry: CatalogEntry, fetcher: Fetcher, *, max_rows: int | None = None, fields: list[str] | None = None
) -> Iterator[Page]:
    if entry.source.portal == "socrata":
        from health_data_registry.adapters import socrata

        yield from socrata.pages(entry, fetcher, max_rows=max_rows, fields=fields)
    elif entry.source.portal == "who_gho":
        from health_data_registry.adapters import who_gho

        yield from who_gho.pages(entry, fetcher, max_rows=max_rows)
    elif entry.source.portal == "https_csv":
        from health_data_registry.adapters import https_csv

        yield from https_csv.pages(entry, fetcher, max_rows=max_rows, fields=fields)
    else:  # pragma: no cover - the catalog model forbids other portals
        raise ValueError(f"no adapter for portal {entry.source.portal}")


def publisher_fields(entry: CatalogEntry, fetcher: Fetcher) -> set[str]:
    """The field names the publisher serves today, for the contract drift check (any portal)."""
    from health_data_registry.adapters import https_csv, socrata, who_gho

    mods = {"socrata": socrata, "who_gho": who_gho, "https_csv": https_csv}
    return mods[entry.source.portal].publisher_fields(entry, fetcher)
