"""number_title_split (hook staging): a heading printed as "<number> – <title>" becomes two columns.

Abstract and poster pages often print their number and title in one heading ("957 – Lenacapavir uptake ...").
The raw layer keeps the heading as printed; staging splits it with ``pattern`` (two groups: the number, the
title; matched whole), with runs of white space in the title made one space. When ``check_field`` is given (the
page's own "Abstract Number" field), the number must equal it.

Refused, never guessed: a heading the pattern doesn't match, an empty heading, or a number that differs from
``check_field``; the load stops (SQL ``error``) and nothing is kept. Declared cardinality: one row per row.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field, field_validator, model_validator

from health_data_registry.adapters.extensions import Extension, ExtensionRefused, StagingStep, register
from health_data_registry.source_config import Strict, safe_pattern

DEFAULT_PATTERN = r"(?s)\s*([A-Za-z0-9][A-Za-z0-9.-]*)\s+[–—-]\s+(.+?)\s*"


class Args(Strict):
    field: str = Field(min_length=1)  # the raw heading
    number_field: str = Field(min_length=1)
    title_field: str = Field(min_length=1)
    pattern: str = DEFAULT_PATTERN
    check_field: str | None = None  # a raw field the number must equal

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        return safe_pattern(v, groups=2)

    @model_validator(mode="after")
    def _names(self) -> Args:
        if len({self.field, self.number_field, self.title_field}) != 3:
            raise ValueError("field, number_field and title_field are three different names")
        return self


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def input_fields(args: Args) -> list[str]:
    return [args.field, *([args.check_field] if args.check_field else [])]


def staging(args: Args, relation: str, fields: Iterable[str]) -> StagingStep:
    fields = list(fields)
    missing = [f for f in input_fields(args) if f not in fields]
    if missing:
        raise ExtensionRefused(f"the records no longer have {', '.join(missing)}")
    made = [f for f in (args.number_field, args.title_field) if f in fields]
    if made:
        raise ExtensionRefused(f"{', '.join(made)} is already a field, so it would be overwritten")
    h, pat = _q(args.field), _lit(f"^(?:{args.pattern})$")
    bad = _lit(f"a {args.field} that isn't '<number> – <title>'")
    split = (
        f"CASE WHEN {h} IS NULL OR NOT regexp_full_match({h}, {_lit(args.pattern)}) THEN error({bad}) "
        f"ELSE regexp_extract({h}, {pat}, 1) END"
    )
    title = f"regexp_replace(regexp_extract({h}, {pat}, 2), '\\s+', ' ', 'g')"
    sql = f"(SELECT *, {split} AS {_q(args.number_field)}, {title} AS {_q(args.title_field)} FROM {relation})"  # noqa: S608 - quoted identifiers and literals
    if args.check_field:
        differs = _lit(f"a {args.number_field} that differs from {args.check_field}")
        sql = (
            f"(SELECT * FROM {sql} WHERE CASE WHEN {_q(args.number_field)} IS DISTINCT FROM "  # noqa: S608 - quoted
            f"{_q(args.check_field)} THEN error({differs}) ELSE true END)"
        )
    return StagingStep(sql=sql, fields=(*fields, args.number_field, args.title_field), rows_per_row=1)


EXTENSION = register(
    Extension(
        name="number_title_split",
        hook="staging",
        args_model=Args,
        run=staging,
        summary="A heading printed as '<number> – <title>' becomes a number and a title; a mismatch stops the load.",
        input_fields=input_fields,
    )
)
