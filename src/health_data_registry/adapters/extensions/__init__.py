"""Named extensions (ADR 0016, section 3): small, pure, reviewed steps a source opts in to by name.

An extension is a step an adapter type's config can't say. Each one is registered here once with its name, the
one hook it runs at, a strict argument model and a plain-words summary, and a catalog entry chooses it by name:

    extensions:
      - {name: header_row_finder, hook: locate, args: {first_cell: "County"}}

A catalog entry naming an unknown extension, the wrong hook, a hook its type doesn't run, or arguments the model
refuses is refused when the catalog loads (``health-data-registry validate``).

The hooks and their fixed signatures (``args`` is the validated argument model):

    locate    (args, grid) -> Location                     where the data sit in a grid of cell text (a header
                                                           row); never changes a value
    parse     (args, content, url) -> list[dict[str, str]] records keyed by the publisher's own labels, every value
                                                           as printed; the core's row guard checks them. For
                                                           html_table an item page's record; for pdf_table one
                                                           record of facts the document prints once, read from
                                                           its text (UTF-8) and added to each of its rows
    staging   (args, relation, fields) -> StagingStep      SQL over the raw relation, run in the one shared
                                                           projection (lake, live read and dbt alike)

Every extension is pure: no network, file system, clock or environment (a test reads each module's imports and
runs the extension tests with sockets disabled). Extensions are named for what they do, never for a source, and
hold no source's values: those are their arguments, in the catalog.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from health_data_registry.adapters.errors import LayoutError
from health_data_registry.source_config import TYPE_HOOKS, ExtensionUse, Strict

# Hooks a type runs once: a second extension at one of them would be ignored or would read the first one's
# output as if it were the publisher's, so the catalog may name only one.
ONE_PER_SOURCE: frozenset[str] = frozenset({"locate", "staging"})


class ExtensionRefused(ValueError):
    """A catalog entry's extension choice is refused (when the catalog loads)."""


class ExtensionLayoutRefused(ExtensionRefused, LayoutError):
    """An extension's input is not laid out as its arguments describe: refused in the same plain words as any
    layout change (adapters.LayoutError), so the server and the CLI give one refusal for all."""


@dataclass(frozen=True)
class Location:
    """Where the data sit in a grid: the 0-based index of the header row."""

    header_row: int


@dataclass(frozen=True)
class StagingStep:
    """A staging extension's SQL over the raw relation, the fields it hands on, and how many rows it makes of one.

    ``rows_per_row`` is the declared cardinality (1 for a 1:1 step, n for a pivot into n keys): the core checks the
    staged row count against it at the raw-to-staging boundary, so a step that drops or repeats rows is refused.
    """

    sql: str
    fields: tuple[str, ...]
    rows_per_row: int
    # Fields the step adds to the grain (wide_to_long's key_field): the contract's primary key must include each,
    # or the staged model's one-row-per-key test would fail on good data (registry checks it at load).
    key_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class Extension:
    name: str
    hook: str
    args_model: type[Strict]
    run: Callable[..., Any]
    summary: str
    # staging only: the raw fields the step reads, from its arguments (the adapter hands these on, not the
    # contract's fields, and the raw contract lists them).
    input_fields: Callable[[Any], Sequence[str]] | None = None
    # staging only: what one raw field it reads holds, in plain words, for the generated raw-source docs
    # ("POP for YEAR 2023"), as (the staged field it lands in, the sentence); None when it is copied as it is.
    explain_input: Callable[[Any, str], tuple[str, str] | None] | None = None
    version: str = "1"  # bumped when a change to the extension changes its output (snapshot identity)


EXTENSIONS: dict[str, Extension] = {}


def register(ext: Extension) -> Extension:
    if ext.name in EXTENSIONS:
        raise ValueError(f"extension {ext.name} is registered twice")
    if ext.hook == "staging" and ext.input_fields is None:
        raise ValueError(f"staging extension {ext.name} must declare the raw fields it reads")
    EXTENSIONS[ext.name] = ext
    return ext


def check_uses(kind: str, uses: Sequence[ExtensionUse]) -> None:
    """Refuse a source's extension list unless every entry is registered, at its own hook, run by this type, with
    arguments its model accepts. Called when the catalog loads."""
    _load()
    seen: set[str] = set()
    hooks: set[str] = set()
    for use in uses:
        ext = EXTENSIONS.get(use.name)
        if ext is None:
            raise ExtensionRefused(
                f"no extension is named {use.name!r}; the registered ones are {sorted(EXTENSIONS)}"
            )
        if use.hook != ext.hook:
            raise ExtensionRefused(f"extension {use.name} runs at hook {ext.hook}, not {use.hook}")
        if use.hook not in TYPE_HOOKS[kind]:
            raise ExtensionRefused(f"type {kind} does not run {use.hook} extensions")
        if use.name in seen:
            raise ExtensionRefused(f"extension {use.name} is listed twice")
        seen.add(use.name)
        if use.hook in ONE_PER_SOURCE and use.hook in hooks:
            raise ExtensionRefused(f"a source names one {use.hook} extension at most")
        hooks.add(use.hook)
        ext.args_model.model_validate(use.args)


def resolved(uses: Sequence[ExtensionUse], hook: str) -> list[tuple[Extension, Any]]:
    """The source's extensions at one hook, in catalog order, each with its validated arguments."""
    _load()
    out = []
    for use in uses:
        if use.hook == hook:
            ext = EXTENSIONS[use.name]
            out.append((ext, ext.args_model.model_validate(use.args)))
    return out


def _load() -> None:
    """Import every extension module once, so each registers itself."""
    from health_data_registry.adapters.extensions import header_row_finder, wide_to_long

    del header_row_finder, wide_to_long  # imported only so each registers itself
