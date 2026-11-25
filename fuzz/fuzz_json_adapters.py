# SPDX-License-Identifier: Apache-2.0
"""Fuzz the JSON adapters (Socrata pages and column metadata, WHO GHO OData pages) with arbitrary bytes.

Each publisher response is untrusted. A fake fetcher answers every request with the fuzz input, so paging stops
at the adapters' own bounds (max_rows, MAX_PAGES). Property: the adapters yield pages of flat records, no more
than max_rows of them in all, or raise ValueError, their documented refusal (json.JSONDecodeError is one). Any
other exception is a finding. Time and memory are bounded by the libFuzzer options in
fuzz/fuzz_json_adapters.options.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import traceback
from collections.abc import Callable
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

# Known findings, filed with reproducers; each is skipped so the harness keeps finding new ones. Each skip matches
# only its issue's exact failure; any other failure still crashes the run. Set each to False when its fix lands.
KNOWN_WRONG_SHAPE = True  # issue #58
KNOWN_SOCRATA_OVER_MAX_ROWS = True  # issue #73

# Issue #58: valid JSON of the wrong shape escapes from these adapter functions as these exception types, with
# these messages (adapter file, function, exception type, message). A body nested past the recursion limit
# escapes from json.loads as RecursionError.
_WRONG_SHAPE: tuple[tuple[str, str, type[Exception], re.Pattern[str]], ...] = (
    ("socrata.py", "_flat", AttributeError, re.compile(r"'\w+' object has no attribute 'items'")),
    ("socrata.py", "publisher_fields", AttributeError, re.compile(r"'\w+' object has no attribute 'get'")),
    ("socrata.py", "publisher_fields", KeyError, re.compile(r"'fieldName'")),
    (
        "socrata.py",
        "publisher_fields",
        TypeError,
        re.compile(
            r"'\w+' object is not (?:iterable|subscriptable)"
            r"|list indices must be integers or slices, not str|string indices must be integers, not 'str'"
        ),
    ),
    ("who_gho.py", "_scalars", AttributeError, re.compile(r"'\w+' object has no attribute 'items'")),
    ("who_gho.py", "pages", AttributeError, re.compile(r"'\w+' object has no attribute 'get'")),
    ("who_gho.py", "publisher_fields", AttributeError, re.compile(r"'\w+' object has no attribute 'get'")),
    ("who_gho.py", "publisher_fields", KeyError, re.compile(r"0")),
    (
        "who_gho.py",
        "publisher_fields",
        TypeError,
        re.compile(r"'\w+' object is not (?:iterable|subscriptable)|unhashable type: '(?:dict|list)'"),
    ),
)


class Finding(AssertionError):
    """An adapter returned something outside its contract."""


class OverMaxRows(Finding):
    """An adapter yielded more records than max_rows."""


def _check(pages: object) -> None:
    total = 0
    for page in pages:  # type: ignore[attr-defined]
        for rec in page.records:
            if not isinstance(rec, dict) or any(isinstance(v, dict | list) for v in rec.values()):
                raise Finding(f"a record that is not flat: {rec!r}")
        total += len(page.records)
        if total > MAX_ROWS:
            raise OverMaxRows(f"{total} records yielded with max_rows={MAX_ROWS}")


def _json_shape(data: bytes) -> str:
    """'json' when the body parses, 'deep' when json.loads hits the recursion limit, else 'other'."""
    try:
        json.loads(data)
    except RecursionError:
        return "deep"
    except ValueError:
        return "other"
    return "json"


def known_adapter_error(adapter: str, data: bytes, e: Exception) -> bool:
    """Whether an exception escaping one adapter call is exactly one of the filed, unfixed findings."""
    if KNOWN_SOCRATA_OVER_MAX_ROWS and type(e) is OverMaxRows and adapter == "socrata.pages":
        return True  # issue #73
    if not KNOWN_WRONG_SHAPE:
        return False
    if type(e) is RecursionError:
        return _json_shape(data) == "deep"  # issue #58
    frames = traceback.extract_tb(e.__traceback__)
    filename = frames[-1].filename.replace("\\", "/").rsplit("/", 1)[-1]
    # The function that raised: on Python 3.11 (the fuzz image) a comprehension is a frame of its own
    # ("<setcomp>", "<listcomp>"), which 3.12 inlines, so step out of those within the same file.
    named = [f for f in frames if f.filename == frames[-1].filename and not f.name.startswith("<")]
    if not named or _json_shape(data) != "json":
        return False
    return any(
        filename == f and named[-1].name == fn and type(e) is kind and pattern.fullmatch(str(e))
        for f, fn, kind, pattern in _WRONG_SHAPE
    )


def TestOneInput(data: bytes) -> None:
    raw = RawFetch(url="https://data.example.gov/x", content=data, sha256=hashlib.sha256(data).hexdigest(),
                   retrieved_at=_WHEN)  # fmt: skip

    def fetch(_url: str) -> RawFetch:
        return raw

    runs: dict[str, Callable[[], object]] = {
        "socrata.pages": lambda: _check(socrata.pages(ENTRY, fetch, max_rows=MAX_ROWS)),  # type: ignore[arg-type]
        "socrata.publisher_fields": lambda: socrata.publisher_fields(ENTRY, fetch),  # type: ignore[arg-type]
        "who_gho.pages": lambda: _check(who_gho.pages(ENTRY, fetch, max_rows=MAX_ROWS)),  # type: ignore[arg-type]
        "who_gho.publisher_fields": lambda: who_gho.publisher_fields(ENTRY, fetch),  # type: ignore[arg-type]
    }
    for adapter, run in runs.items():
        try:
            run()
        except ValueError:
            continue
        except Exception as e:
            if known_adapter_error(adapter, data, e):
                continue
            raise


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
