"""wide_to_long (hook staging): columns named <stub><key> become one row per key, in the shared projection.

A wide file (POPESTIMATE2020 ... POPESTIMATE2025) is kept in the raw layer as the publisher wrote it; staging
unpivots it into one row per original row and key, with ``key_field`` holding the key text and each stub's column
holding that key's value exactly as published. It compiles to standard ``UNPIVOT INCLUDE NULLS`` (DuckDB), so the lake,
the live read and the generated dbt staging model run the same SQL. No value is computed, changed or dropped.

Refused: a stub missing a key, or a column that looks like ``<stub><key>`` for a key ``expected_keys`` doesn't list
(a renamed, missing or extra year). Declared cardinality: one input row makes ``len(expected_keys)`` rows.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field, field_validator, model_validator

from health_data_registry.adapters.extensions import Extension, ExtensionRefused, StagingStep, register
from health_data_registry.source_config import Strict, match_whole, safe_pattern


class Args(Strict):
    keep: list[str] = Field(min_length=1)
    stubs: list[str] = Field(min_length=1)
    key_field: str
    key_pattern: str = r"\d{4}"
    expected_keys: list[str] = Field(min_length=1, max_length=200)  # from the codebook's file layout

    @field_validator("key_pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        return safe_pattern(v)

    @model_validator(mode="after")
    def _keys(self) -> Args:
        if any(not match_whole(self.key_pattern, k) for k in self.expected_keys):
            raise ValueError("every expected key must match key_pattern")
        if len(set(self.expected_keys)) != len(self.expected_keys):
            raise ValueError("expected_keys lists each key once")
        if self.key_field in self.keep or self.key_field in self.stubs:
            raise ValueError(f"key_field {self.key_field!r} would overwrite a kept column or a stub")
        return self


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def input_fields(args: Args) -> list[str]:
    return [*args.keep, *(f"{stub}{k}" for k in args.expected_keys for stub in args.stubs)]


def check_fields(args: Args, present: Iterable[str]) -> None:
    """Refuse a layout this pivot would read wrongly: a missing input column, or a <stub><key> column for a key
    the codebook doesn't list."""
    have = set(present)
    missing = [f for f in input_fields(args) if f not in have]
    if missing:
        raise ExtensionRefused(f"the file no longer has the columns {', '.join(missing)}")
    extra = sorted(
        f
        for f in have
        for stub in args.stubs
        if f.startswith(stub)
        and match_whole(args.key_pattern, f[len(stub) :])
        and f[len(stub) :] not in args.expected_keys
    )
    if extra:
        raise ExtensionRefused(
            f"the file has {', '.join(extra)}, for keys its layout doesn't list ({', '.join(args.expected_keys)}); "
            "a renamed or extra column is not pivoted"
        )


def staging(args: Args, relation: str, fields: Iterable[str]) -> StagingStep:
    fields = list(fields)
    check_fields(args, fields)
    on = ", ".join(
        "(" + ", ".join(_q(s + k) for s in args.stubs) + f") AS {_lit(k)}" for k in args.expected_keys
    )
    values = ", ".join(_q(s) for s in args.stubs)
    sql = (
        f"(SELECT * FROM (SELECT * FROM {relation}) UNPIVOT INCLUDE NULLS "  # noqa: S608 - quoted identifiers
        f"(({values}) FOR {_q(args.key_field)} IN ({on})))"
    )
    wide = {f"{s}{k}" for k in args.expected_keys for s in args.stubs}
    kept = [f for f in fields if f not in wide]
    return StagingStep(
        sql=sql, fields=(*kept, args.key_field, *args.stubs), rows_per_row=len(args.expected_keys)
    )


EXTENSION = register(
    Extension(
        name="wide_to_long",
        hook="staging",
        args_model=Args,
        run=staging,
        summary="Columns named <stub><key> become one row per key; every value is copied as published.",
        input_fields=input_fields,
    )
)
