"""Socrata (SODA 2.x) adapter, e.g. data.cdc.gov.

Pages with $limit/$offset ordered by the row id so pages never overlap or skip, and reads the column
metadata (/api/views/<id>.json) for the nightly check that a source still matches its contract.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from health_data_registry.adapters import Fetcher, Page
from health_data_registry.registry import CatalogEntry

PAGE_SIZE = 50_000


def page_url(api_url: str, *, limit: int, offset: int) -> str:
    parts = urlsplit(api_url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("$limit", "$offset", "$order")]
    query += [("$order", ":id"), ("$limit", str(limit)), ("$offset", str(offset))]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def pages(entry: CatalogEntry, fetcher: Fetcher, *, max_rows: int | None = None) -> Iterator[Page]:
    offset = 0
    while True:
        limit = PAGE_SIZE if max_rows is None else min(PAGE_SIZE, max_rows - offset)
        if limit <= 0:
            return
        raw = fetcher(page_url(entry.source.api_url, limit=limit, offset=offset))
        records = json.loads(raw.content)
        if not isinstance(records, list):
            raise ValueError(f"{entry.id}: expected a JSON array from {raw.url}")
        yield Page(fetch=raw, records=[_flat(r) for r in records])
        if len(records) < limit:
            return
        offset += len(records)


def _flat(record: dict[str, Any]) -> dict[str, Any]:
    # Socrata returns nested objects for location/url columns; keep them as JSON text, unchanged.
    return {
        k: (json.dumps(v, sort_keys=True) if isinstance(v, dict | list) else v) for k, v in record.items()
    }


def metadata_url(entry: CatalogEntry) -> str:
    return f"https://{entry.source.host}/api/views/{entry.source.dataset}.json"


def publisher_fields(entry: CatalogEntry, fetcher: Fetcher) -> set[str]:
    """The field names the publisher serves today, for the contract drift check."""
    meta = json.loads(fetcher(metadata_url(entry)).content)
    return {str(c["fieldName"]) for c in meta.get("columns", []) if not str(c["fieldName"]).startswith(":")}
