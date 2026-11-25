"""rule_tags (hook staging): our own tags from fixed word rules over an item's text, never a model's reading.

Each output column holds every tag of one rule set (research_tags.RULE_SETS: conditions, designs, data sources,
geography) whose rule matches the item's ``text_fields`` (joined by line breaks), sorted and joined with '; ', or
'' when none match. The rules are research_tags' own, the same ones the DOI harvest applies in Python, compiled
here for RE2 so the lake, the private stores and dbt tag alike (ADR 0006: a tag is a hint, not a stored fact
about the study). Declared cardinality: one row per row.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import Field, model_validator

from health_data_registry.adapters.extensions import Extension, ExtensionRefused, StagingStep, register
from health_data_registry.research_tags import RULE_SETS, pattern
from health_data_registry.source_config import Strict

RuleSet = Literal["conditions", "designs", "data_sources", "geography"]


class Args(Strict):
    text_fields: list[str] = Field(min_length=1, max_length=10)
    columns: dict[str, RuleSet] = Field(min_length=1, max_length=10)  # output column -> rule set

    @model_validator(mode="after")
    def _names(self) -> Args:
        if set(self.columns) & set(self.text_fields):
            raise ValueError("an output column can't be one of the text fields")
        return self


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def input_fields(args: Args) -> list[str]:
    return list(args.text_fields)


def column_sql(words: str, rule_set: str) -> str:
    rules, keep_case = RULE_SETS[rule_set]
    hits = ", ".join(
        f"CASE WHEN regexp_matches({words}, {_lit(pattern(pats, case_sensitive=tag in keep_case))}) "
        f"THEN {_lit(tag)} END"
        for tag, pats in rules.items()
    )
    return f"array_to_string(list_sort(list_filter([{hits}], lambda x: x IS NOT NULL)), '; ')"


def staging(args: Args, relation: str, fields: Iterable[str]) -> StagingStep:
    fields = list(fields)
    missing = [f for f in args.text_fields if f not in fields]
    if missing:
        raise ExtensionRefused(f"the records no longer have {', '.join(missing)}")
    made = [c for c in args.columns if c in fields]
    if made:
        raise ExtensionRefused(f"{', '.join(made)} is already a field, so it would be overwritten")
    words = " || chr(10) || ".join(f"coalesce({_q(f)}, '')" for f in args.text_fields)
    cols = ", ".join(f"{column_sql(f'({words})', rs)} AS {_q(c)}" for c, rs in args.columns.items())
    return StagingStep(
        sql=f"(SELECT *, {cols} FROM {relation})",  # noqa: S608 - quoted identifiers and literals
        fields=(*fields, *args.columns),
        rows_per_row=1,
    )


EXTENSION = register(
    Extension(
        name="rule_tags",
        hook="staging",
        args_model=Args,
        run=staging,
        summary="Tags from research_tags' fixed word rules over the named text fields; no model, no value changed.",
        input_fields=input_fields,
    )
)
