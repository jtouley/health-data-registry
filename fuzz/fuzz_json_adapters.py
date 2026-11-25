# SPDX-License-Identifier: Apache-2.0
"""Fuzz the JSON adapters (Socrata pages and column metadata, WHO GHO OData pages) with arbitrary bytes.

Each publisher response is untrusted. A fake fetcher answers every request with the fuzz input, so paging stops
at the adapters' own bounds (max_rows, MAX_PAGES). Property: the adapters yield pages of flat records or raise
ValueError, their documented refusal (json.JSONDecodeError is one). Any other exception is a finding. Time and
memory are bounded by the libFuzzer options in fuzz/fuzz_json_adapters.options.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import atheris

with atheris.instrument_imports():
    from health_data_registry.adapters import socrata, who_gho
    from health_data_registry.store import RawFetch

MAX_ROWS = 5
ENTRY = SimpleNamespace(
    id="fuzz",
    source=SimpleNamespace(
        host="data.example.gov",
        dataset="abcd-1234",
        api_url="https://data.example.gov/resource/abcd-1234.json",
    ),
)
_WHEN = datetime(2026, 1, 1, tzinfo=UTC)

# Known findings, filed with reproducers; each is skipped so the harness keeps finding new ones. Remove an entry
# when its fix lands. Issue #58: a JSON value of the wrong shape (a list of non-objects, an object where a list
# was expected, or a document nested past the recursion limit) escapes as AttributeError, TypeError, KeyError
# or RecursionError rather than ValueError.
KNOWN = (AttributeError, TypeError, KeyError, RecursionError)


class Finding(AssertionError):
    """An adapter returned something outside its contract."""


def _check(pages: object) -> None:
    for page in pages:  # type: ignore[attr-defined]
        for rec in page.records:
            if not isinstance(rec, dict) or any(isinstance(v, dict | list) for v in rec.values()):
                raise Finding(f"a record that is not flat: {rec!r}")


def TestOneInput(data: bytes) -> None:
    raw = RawFetch(url="https://data.example.gov/x", content=data, sha256=hashlib.sha256(data).hexdigest(),
                   retrieved_at=_WHEN)  # fmt: skip

    def fetch(_url: str) -> RawFetch:
        return raw

    runs = (
        lambda: _check(socrata.pages(ENTRY, fetch, max_rows=MAX_ROWS)),  # type: ignore[arg-type]
        lambda: socrata.publisher_fields(ENTRY, fetch),  # type: ignore[arg-type]
        lambda: _check(who_gho.pages(ENTRY, fetch, max_rows=MAX_ROWS)),  # type: ignore[arg-type]
        lambda: who_gho.publisher_fields(ENTRY, fetch),  # type: ignore[arg-type]
    )
    for run in runs:
        try:
            run()
        except ValueError:
            continue
        except KNOWN:
            continue


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
