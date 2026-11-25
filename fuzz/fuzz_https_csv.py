# SPDX-License-Identifier: Apache-2.0
"""Fuzz the https_csv adapter's file reader and its wide-to-long pivot with arbitrary bytes.

Property: records() returns one dict per row (or per row and key, after the pivot), or raises ValueError, the
module's documented refusal (UnicodeDecodeError is one). Any other exception is a finding. Time and memory are
bounded by the libFuzzer options in fuzz/fuzz_https_csv.options.
"""

from __future__ import annotations

import csv
import sys
from types import SimpleNamespace

import atheris

with atheris.instrument_imports():
    from health_data_registry.adapters import https_csv
    from health_data_registry.registry import Reshape, WideToLong

# The pivot of the Census population file's catalog entry (catalog/census_county_pop.yaml, source.reshape), copied
# here so the harness needs no catalog files at run time; keep the two in step. Its non-empty expected_keys means
# a file whose years differ from the layout's is refused, so that refusal path is fuzzed too.
_PIVOT = Reshape(
    wide_to_long=WideToLong(
        keep=["SUMLEV", "REGION", "DIVISION", "STATE", "COUNTY", "STNAME", "CTYNAME"],
        stubs=["POPESTIMATE"],
        key_field="YEAR",
        key_pattern=r"\d{4}",
        expected_keys=["2020", "2021", "2022", "2023", "2024", "2025"],
    )
)
ENTRIES = [
    SimpleNamespace(id="fuzz_plain", source=SimpleNamespace(encoding="utf-8", reshape=None)),
    SimpleNamespace(id="fuzz_pivot", source=SimpleNamespace(encoding="latin-1", reshape=_PIVOT)),
]


# A known finding, filed with a reproducer as issue #60; skipped so the harness keeps finding new ones. Set to
# False when the fix lands: a field over csv.field_size_limit() escapes as csv.Error, not ValueError. Only that
# exact message, on an input long enough to hold such a field, is skipped.
KNOWN_CSV_ERROR = True


def known_csv_error(data: bytes, e: Exception) -> bool:
    """Whether an exception escaping records() is exactly issue #60."""
    return (
        KNOWN_CSV_ERROR
        and type(e) is csv.Error
        and str(e) == f"field larger than field limit ({csv.field_size_limit()})"
        and len(data) > csv.field_size_limit()
    )


class Finding(AssertionError):
    """records() returned something outside its contract."""


def TestOneInput(data: bytes) -> None:
    for entry in ENTRIES:
        try:
            recs = https_csv.records(entry, data)  # type: ignore[arg-type]
        except ValueError:
            continue
        except Exception as e:
            if known_csv_error(data, e):
                continue
            raise
        for rec in recs:
            if not isinstance(rec, dict) or not all(isinstance(v, str) for v in rec.values()):
                raise Finding(f"a record that is not text keyed by column: {rec!r}")
            if entry.source.reshape is not None and rec.get("YEAR") not in _PIVOT.wide_to_long.expected_keys:
                raise Finding(f"a pivoted record whose key is not one of the layout's: {rec.get('YEAR')!r}")


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
