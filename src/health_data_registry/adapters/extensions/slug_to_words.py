"""slug_to_words (hook staging): a value written as a slug ("hiv-prevention") is shown as words ("hiv prevention").

Some pages carry a label only in a class or a link, as a slug. The raw layer keeps the slug as written; staging
shows it with each ``separator`` read as a space, for the named fields only. No other character changes; an
empty value stays empty. Declared cardinality: one row per row.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import Field

from health_data_registry.adapters.extensions import Extension, ExtensionRefused, StagingStep, register
from health_data_registry.source_config import Strict


class Args(Strict):
    fields: list[str] = Field(min_length=1, max_length=20)
    separator: str = Field(default="-", min_length=1, max_length=1)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def input_fields(args: Args) -> list[str]:
    return list(args.fields)


def staging(args: Args, relation: str, fields: Iterable[str]) -> StagingStep:
    fields = list(fields)
    missing = [f for f in args.fields if f not in fields]
    if missing:
        raise ExtensionRefused(f"the records no longer have {', '.join(missing)}")
    swaps = ", ".join(f"replace({_q(f)}, {_lit(args.separator)}, ' ') AS {_q(f)}" for f in args.fields)
    return StagingStep(
        sql=f"(SELECT * REPLACE ({swaps}) FROM {relation})",  # noqa: S608 - quoted identifiers
        fields=tuple(fields),
        rows_per_row=1,
    )


EXTENSION = register(
    Extension(
        name="slug_to_words",
        hook="staging",
        args_model=Args,
        run=staging,
        summary="A slug value ('hiv-prevention') is shown as words ('hiv prevention'); nothing else changes.",
        input_fields=input_fields,
    )
)
