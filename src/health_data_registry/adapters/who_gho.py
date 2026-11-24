"""WHO Global Health Observatory OData adapter.

WHO data is used under WHO's Terms and Conditions for datasets (public health purposes only): it is read live
from WHO for each question, held in memory only (live.py), never written to any lake and never published
(catalog licence.redistribution: false).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from health_data_registry.adapters import Fetcher, Page
from health_data_registry.registry import CatalogEntry

MAX_PAGES = 200


def pages(entry: CatalogEntry, fetcher: Fetcher, *, max_rows: int | None = None) -> Iterator[Page]:
    url: str | None = entry.source.api_url
    seen = 0
    for _ in range(MAX_PAGES):
        if url is None:
            return
        raw = fetcher(url)
        doc = json.loads(raw.content)
        values = doc.get("value")
        if not isinstance(values, list):
            raise ValueError(f"{entry.id}: expected an OData 'value' array from {raw.url}")
        records = [_scalars(v) for v in values]
        if max_rows is not None:
            records = records[: max(0, max_rows - seen)]
        seen += len(records)
        yield Page(fetch=raw, records=records)
        if max_rows is not None and seen >= max_rows:
            return
        nxt = doc.get("@odata.nextLink")
        url = str(nxt) if nxt else None
    raise ValueError(f"{entry.id}: more than {MAX_PAGES} pages; refusing to continue")


def _scalars(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if not isinstance(v, dict | list)}


def publisher_fields(entry: CatalogEntry, fetcher: Fetcher) -> set[str]:
    sep = "&" if "?" in entry.source.api_url else "?"
    doc = json.loads(fetcher(f"{entry.source.api_url}{sep}$top=1").content)
    values = doc.get("value") or []
    return set(values[0]) if values else set()
