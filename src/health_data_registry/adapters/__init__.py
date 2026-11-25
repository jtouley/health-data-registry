"""Source adapters: one per publisher portal. Each turns a catalog entry into raw fetches and records.

Adapters never change a value. They page through the publisher's API, keep every response's bytes for
the raw store, and hand back records keyed by the publisher's own field names; the contract does the rest.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from health_data_registry.registry import CatalogEntry
from health_data_registry.store import RawFetch

Fetcher = Callable[[str], RawFetch]


@dataclass(frozen=True)
class Page:
    fetch: RawFetch
    records: list[dict[str, Any]]
    # The sha256 of each table's column list this page was read from (Tableau worksheets), for the manifest.
    layout: dict[str, str] = field(default_factory=dict)


def pages(
    entry: CatalogEntry, fetcher: Fetcher, *, max_rows: int | None = None, fields: list[str] | None = None
) -> Iterator[Page]:
    """Every page of a source, each checked by the row-level guard before it is handed on (hard rule 4).

    The Tableau adapter checks each worksheet itself, by its captions, before it projects anything; every other
    portal's page is checked here on the field names and values it carries.
    """
    from health_data_registry.adapters.row_guard import check_grain

    for page in _pages(entry, fetcher, max_rows=max_rows, fields=fields):
        if entry.source.portal != "tableau":
            names = list(dict.fromkeys(k for r in page.records for k in r))
            check_grain(f"{entry.id} ({entry.source.host})", names, page.records)
        yield page


def _pages(
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
    elif entry.source.portal == "https_xlsx":
        from health_data_registry.adapters import https_xlsx

        yield from https_xlsx.pages(entry, fetcher, max_rows=max_rows, fields=fields)
    elif entry.source.portal == "tableau":
        from health_data_registry.adapters import tableau

        yield from tableau.pages(entry, fetcher, max_rows=max_rows, fields=fields)
    else:  # pragma: no cover - the catalog model forbids other portals
        raise ValueError(f"no adapter for portal {entry.source.portal}")


def publisher_fields(entry: CatalogEntry, fetcher: Fetcher) -> set[str]:
    """The field names the publisher serves today, for the contract drift check (any portal)."""
    from health_data_registry.adapters import https_csv, https_xlsx, socrata, tableau, who_gho

    if entry.source.portal == "tableau":
        return tableau.publisher_fields(entry, fetcher)
    if entry.source.portal == "socrata":
        return socrata.publisher_fields(entry, fetcher)
    if entry.source.portal == "who_gho":
        return who_gho.publisher_fields(entry, fetcher)
    if entry.source.portal == "https_xlsx":
        return https_xlsx.publisher_fields(entry, fetcher)
    return https_csv.publisher_fields(entry, fetcher)
