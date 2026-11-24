"""MCP server (stdio): read-only tools and clinician helpers over the reviewed registry and the local lake.

Tool descriptions are written for the model a clinician is talking to; every refusal comes back as plain
words with a next step. Results are CSV by default (fmt="json" for structured output).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Literal

from mcp import types
from mcp.server.mcpserver import MCPServer

from health_data_registry import __version__, helpers, tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import lake_root

READ_ONLY = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
Fmt = Literal["csv", "json"]
Out = str | dict[str, Any]

INSTRUCTIONS = (
    "Observed and local infectious disease data (CDC, WHO and other public health publishers) for "
    "clinician-researchers, with reviewed per-dataset contracts. Use the IHME GBD connector for modelled burden; "
    "this server returns observed data keyed to GBD cause and location names and never calls GBD. Start with "
    "suggest_datasets or search_datasets, read describe_dataset before relying on a source, and use get_series "
    "for numbers. Cite every number with the source and retrieved_at it comes with, repeat the notes that follow "
    "a table, and never fill a number from memory. The user is a doctor: explain in plain words, one step at a "
    "time. Never ask for or accept patient information."
)


def _safe(fn: Callable[[], Out], fmt: Fmt) -> Out:
    """No stack trace ever reaches a clinician: any unexpected failure becomes a plain message."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - the whole point is that nothing escapes unworded
        return tools.refusal(e, fmt)


def build_server(registry: Registry | None = None, engine: Engine | None = None) -> MCPServer:
    reg = registry or load_registry()
    eng = engine or Engine(registry=reg, lake=lake_root())
    mcp = MCPServer("health-data-registry", version=__version__, instructions=INSTRUCTIONS)

    @mcp.tool(annotations=READ_ONLY)
    def list_sources(
        condition: str | None = None, geography_level: str | None = None, fmt: Fmt = "csv"
    ) -> Out:
        """What's loaded: each source's publisher, years, places, how often it updates, when it was last fetched,
        its licence and whether its contract has been reviewed. geography_level: nation, state, county, country."""
        return _safe(
            lambda: tools.list_sources(
                reg, eng, condition=condition, geography_level=geography_level, fmt=fmt
            ),
            fmt,
        )

    @mcp.tool(annotations=READ_ONLY)
    def search_datasets(
        topic: str = "",
        id_area: str | None = None,
        geography: str | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """Datasets that fit a topic (e.g. "legionella", "syphilis in states"), ranked, each with a one-line
        reason. Filters: id_area (infectious_disease), geography level, years."""
        return _safe(
            lambda: tools.search_datasets(
                reg, topic=topic, id_area=id_area, geography=geography, year_start=year_start, year_end=year_end,
                fmt=fmt,
            ),
            fmt,
        )  # fmt: skip

    @mcp.tool(annotations=READ_ONLY)
    def describe_dataset(source_id: str, fmt: Fmt = "csv") -> Out:
        """A source explained in clinical words: what one row is, the conditions and case definitions, what each
        column means, counts vs rates, missing codes, total rows, caveats, licence and how to cite it."""
        return _safe(lambda: tools.describe_dataset(reg, source_id, eng, fmt=fmt), fmt)

    @mcp.tool(annotations=READ_ONLY)
    def suggest_datasets(question: str, fmt: Fmt = "csv") -> Out:
        """For a research question in plain words: which sources can answer it, which can't and why."""
        return _safe(lambda: tools.suggest_datasets(reg, question, fmt=fmt), fmt)

    @mcp.tool(annotations=READ_ONLY)
    def get_series(
        condition: str,
        locations: list[str] | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
        measure: str | None = None,
        sex: str | None = None,
        source_id: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """Observed values over time for a condition (e.g. "legionellosis") in places (e.g. ["Michigan", "Ohio",
        "United States"]), one row per place and year, each with its source, fetch time and case definition.
        measure: cases, deaths, rate, percent. Counts are added up; rates are shown as published, never added.
        Running totals show the year's own figure and next year's figure for it side by side; the flag column
        gives the publisher's reason for an empty value."""
        return _safe(
            lambda: tools.get_series(
                reg, eng, condition=condition, locations=locations or [], year_start=year_start, year_end=year_end,
                measure=measure, sex=sex, source_id=source_id, fmt=fmt,
            ),
            fmt,
        )  # fmt: skip

    @mcp.tool(annotations=READ_ONLY)
    def get_aligned_with_gbd(
        condition: str,
        locations: list[str] | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """The same observed rows keyed to GBD location and cause names, plus the exact IHME GBD connector call
        to run next. This server never calls GBD and never judges the two against each other."""
        return _safe(
            lambda: tools.get_aligned_with_gbd(
                reg, eng, condition=condition, locations=locations or [], year_start=year_start, year_end=year_end,
                fmt=fmt,
            ),
            fmt,
        )  # fmt: skip

    @mcp.tool(annotations=READ_ONLY)
    def get_rate(condition: str, year: int, locations: list[str] | None = None, fmt: Fmt = "csv") -> Out:
        """Crude rate per 100,000 from case counts and Census population, marked as computed here; never
        age-standardised."""
        return _safe(
            lambda: tools.get_rate(
                reg, eng, condition=condition, locations=locations or [], year=year, fmt=fmt
            ),
            fmt,
        )

    @mcp.tool(annotations=READ_ONLY)
    def get_provenance(source_id: str, year: int | None = None, fmt: Fmt = "csv") -> Out:
        """Where a source's numbers came from: the exact URLs, when they were fetched, content hashes, case
        definitions and how to cite them."""
        return _safe(lambda: tools.get_provenance(reg, eng, source_id=source_id, year=year, fmt=fmt), fmt)

    @mcp.tool(annotations=READ_ONLY)
    def list_caveats(source_id: str | None = None, condition: str | None = None, fmt: Fmt = "csv") -> Out:
        """What the data can't tell you: geography traps, suppression, provisional years, unreviewed contracts
        and facts not yet confirmed with the publisher."""
        return _safe(lambda: tools.list_caveats(reg, source_id=source_id, condition=condition, fmt=fmt), fmt)

    @mcp.tool(annotations=READ_ONLY)
    def find_prior_work(
        condition: str,
        geography: str | None = None,
        years: str | None = None,
        dataset: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """Conference abstracts, case reports and health department reports on a condition, with the datasets
        each used. (The index is not built yet; the tool says so rather than guessing.)"""
        return _safe(
            lambda: tools.find_prior_work(reg, condition=condition, geography=geography, years=years,
                                          dataset=dataset, fmt=fmt),
            fmt,
        )  # fmt: skip

    @mcp.tool(annotations=READ_ONLY)
    def query_sql(sql: str, fmt: Fmt = "csv") -> Out:
        """Escape hatch for questions the other tools don't cover: one read-only SELECT over loaded sources
        (tables are named by source_id). Writes, file reads and undeclared joins are refused; results are capped."""
        return _safe(lambda: tools.query_sql(eng, sql, fmt=fmt), fmt)

    for h in helpers.HELPERS:
        _register_helper(mcp, h)

    return mcp


def _register_helper(mcp: MCPServer, h: helpers.Helper) -> None:
    # Prompt arguments come from the function signature, so build one per helper with exactly its args.
    if not h.args:

        def no_args() -> str:
            return h.render()

        fn: Callable[..., str] = no_args
    else:

        def with_args(**values: str) -> str:
            return h.render(**values)

        sig_params = [
            inspect.Parameter(a, inspect.Parameter.KEYWORD_ONLY, default="", annotation=str) for a in h.args
        ]
        with_args.__signature__ = inspect.Signature(sig_params, return_annotation=str)  # type: ignore[attr-defined]
        with_args.__annotations__ = {**{a: str for a in h.args}, "return": str}
        fn = with_args
    fn.__name__ = h.name
    fn.__doc__ = h.description
    mcp.prompt(name=h.name, title=h.title, description=h.description)(fn)


def main() -> None:
    build_server().run("stdio")
