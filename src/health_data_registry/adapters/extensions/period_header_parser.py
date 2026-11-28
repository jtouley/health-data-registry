"""period_header_parser (hook staging): a printed column header becomes the period it names, in the shared projection.

A report whose column headers change from issue to issue (``Q1 (1/1-3/31)``, ``Q2_2025``, ``YTD 2026``,
``2021- 2025 Total``, ``5-Year Average``) is kept in the raw layer one record per printed cell, with the header as
printed. Staging reads the header against the catalog's ordered ``rules``: the first rule whose pattern matches the
whole header gives the period's kind, and its groups give the year, quarter and last year as printed. A rule may
take the year from a field instead (``year_field``: a fact the report prints once, such as its title year), for a
header that prints no year of its own.

It adds four fields and changes none: ``period_kind`` (the rule's kind), ``period_year``, ``period_quarter`` and
``period_year_end`` (the digits as printed, typed by the contract). A header no rule matches keeps every field empty,
and the contract's quality rule on ``period_kind`` says so: it is never guessed. One raw row makes one staged row.
It compiles to DuckDB ``regexp_extract`` (RE2, linear time), so the lake, the live read and dbt run the same SQL.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field, model_validator

from health_data_registry.adapters.extensions import Extension, ExtensionLayoutRefused, StagingStep, register
from health_data_registry.source_config import Strict, compile_pattern, safe_pattern

OUTPUTS = ("period_kind", "period_year", "period_quarter", "period_year_end")
_KIND = r"^[a-z][a-z0-9_]{1,40}$"


class Rule(Strict):
    pattern: str  # matched against the whole header, as printed
    kind: str = Field(pattern=_KIND)
    year: int | None = Field(
        default=None, ge=1, le=9
    )  # the group holding the year; unset: year_field, or none
    quarter: int | None = Field(default=None, ge=1, le=9)
    year_end: int | None = Field(default=None, ge=1, le=9)
    year_from_field: bool = False  # take the year from year_field (the header prints none)

    @model_validator(mode="after")
    def _groups(self) -> Rule:
        safe_pattern(self.pattern)
        n = compile_pattern(self.pattern).groups
        for name in ("year", "quarter", "year_end"):
            g = getattr(self, name)
            if g is not None and g > n:
                raise ValueError(f"rule {self.pattern!r}: {name} names group {g}, but the pattern has {n}")
        if self.year is not None and self.year_from_field:
            raise ValueError(
                f"rule {self.pattern!r}: the year comes from a group or from year_field, not both"
            )
        return self


class Args(Strict):
    field: str  # the raw field holding the printed header (pdf_table's long layout: column_header)
    year_field: str | None = None  # a raw field holding a year printed once for the whole report
    rules: list[Rule] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _fields(self) -> Args:
        if any(r.year_from_field for r in self.rules) and self.year_field is None:
            raise ValueError("a rule takes its year from year_field, so year_field is named")
        if {self.field, self.year_field} & set(OUTPUTS):
            raise ValueError(f"the fields read can't be named like the fields made ({', '.join(OUTPUTS)})")
        return self


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def input_fields(args: Args) -> list[str]:
    return [args.field, *([args.year_field] if args.year_field else [])]


def _case(args: Args, value: dict[int, str]) -> str:
    """CASE over the rules in order: the first whose pattern matches the whole header gives ``value[i]``."""
    header = _q(args.field)
    whens = [
        f"WHEN regexp_full_match({header}, {_lit(r.pattern)}) THEN {value[i]}"
        for i, r in enumerate(args.rules)
        if i in value
    ]
    if not whens:
        return "CAST(NULL AS VARCHAR)"
    return "CASE " + " ".join(whens) + " ELSE CAST(NULL AS VARCHAR) END"


def _group(args: Args, i: int, g: int) -> str:
    return f"NULLIF(regexp_extract({_q(args.field)}, {_lit('^(?:' + args.rules[i].pattern + ')$')}, {g}), '')"


def staging(args: Args, relation: str, fields: Iterable[str]) -> StagingStep:
    fields = list(fields)
    clash = sorted(set(fields) & set(OUTPUTS))
    if clash:
        raise ExtensionLayoutRefused(
            f"the records already have {', '.join(clash)}, which this step would make"
        )
    missing = [f for f in input_fields(args) if f not in fields]
    if missing:
        raise ExtensionLayoutRefused(f"the records no longer have {', '.join(missing)}")
    kind = {i: _lit(r.kind) for i, r in enumerate(args.rules)}
    year: dict[int, str] = {}
    quarter: dict[int, str] = {}
    year_end: dict[int, str] = {}
    for i, r in enumerate(args.rules):
        if r.year is not None:
            year[i] = _group(args, i, r.year)
        elif r.year_from_field and args.year_field:
            year[i] = f"CAST({_q(args.year_field)} AS VARCHAR)"
        if r.quarter is not None:
            quarter[i] = _group(args, i, r.quarter)
        if r.year_end is not None:
            year_end[i] = _group(args, i, r.year_end)
    made = ", ".join(
        f"{_case(args, m)} AS {_q(name)}"
        for name, m in zip(OUTPUTS, (kind, year, quarter, year_end), strict=True)
    )
    # Every identifier is quoted and every literal escaped (_q, _lit); `relation` is the caller's own relation.
    sql = " ".join(("(SELECT *,", made, "FROM", relation + ")"))
    return StagingStep(sql=sql, fields=(*fields, *OUTPUTS), rows_per_row=1)


EXTENSION = register(
    Extension(
        name="period_header_parser",
        hook="staging",
        args_model=Args,
        run=staging,
        summary="A printed column header becomes the period it names (kind, year, quarter), by ordered rules.",
        input_fields=input_fields,
    )
)
