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

# The shape of the Census population file's catalog entry (catalog/census_county_pop.yaml), written out here so
# the harness needs no catalog files at run time. Only the fields the adapter reads are set.
_PIVOT = Reshape(
    wide_to_long=WideToLong(
        keep=["STATE", "NAME"], stubs=["POPESTIMATE", "DEATHS"], key_field="YEAR", expected_keys=[]
    )
)
ENTRIES = [
    SimpleNamespace(id="fuzz_plain", source=SimpleNamespace(encoding="utf-8", reshape=None)),
    SimpleNamespace(id="fuzz_pivot", source=SimpleNamespace(encoding="latin-1", reshape=_PIVOT)),
]


# A known finding, filed with a reproducer as issue #60; skipped so the harness keeps finding new ones. Set to
# False when the fix lands: a field over csv.field_size_limit() escapes as csv.Error, not ValueError.
KNOWN_CSV_ERROR = True


class Finding(AssertionError):
    """records() returned something outside its contract."""


def TestOneInput(data: bytes) -> None:
    for entry in ENTRIES:
        try:
            recs = https_csv.records(entry, data)  # type: ignore[arg-type]
        except ValueError:
            continue
        except csv.Error as e:
            if KNOWN_CSV_ERROR and "field larger than field limit" in str(e):
                continue
            raise
        for rec in recs:
            if not isinstance(rec, dict) or not all(isinstance(v, str) for v in rec.values()):
                raise Finding(f"a record that is not text keyed by column: {rec!r}")
            if entry.source.reshape is not None and "YEAR" not in rec:
                raise Finding("a pivoted record without its key")


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
