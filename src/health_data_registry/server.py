"""MCP server (stdio here; Streamable HTTP in hosted.py): read-only tools and clinician helpers over the reviewed registry and the local lake.

Tool descriptions are written for the model a clinician is talking to; every refusal comes back as plain
words with a next step. Results are CSV by default (fmt="json" for structured output).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Literal

from mcp import types
from mcp.server.mcpserver import MCPServer

from health_data_registry import __version__, analysis, helpers, publication, tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import lake_root

READ_ONLY = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
Fmt = Literal["csv", "json"]
Out = str | dict[str, Any]

INSTRUCTIONS = """\
Observed infectious disease data from CDC, WHO and other public publishers, for clinician-researchers; modelled \
burden (deaths, DALYs, YLLs, modelled incidence) is the IHME GBD connector's job. This server never calls GBD and \
never says whether GBD or local data is right.

━━━ TOOL ROUTING ━━━
Intent → tool. Each tool's own description carries its arguments and caveats.
  which sources could answer a question in plain words: suggest_datasets(question=...)
  find sources on a topic, place level or years: search_datasets(topic=..., geography=..., year_start=...)
  what one source holds, its condition labels and allowed filter values: describe_dataset(source_id) \
(read condition_labels and dimension_values before filtering)
  what is loaded and how fresh: list_sources(condition=..., geography_level=...)
  counts or published values by place and year: get_series(condition=<a label from describe_dataset>, \
locations=[...], year_start=..., year_end=..., source_id=...)
  how common per 100,000 here: get_rate(condition=..., year=..., locations=[...])
  higher here than there, or now than then: compare_rates(condition=..., location=..., year=..., \
other_location=... | other_year=...)
  is it rising, annual percent change: get_trend(condition=..., location=..., year_start=..., year_end=...)
  does age explain a difference: age_adjust(condition=..., location=..., year=...)
  is this week unusual: check_signal(condition=..., location=..., year=..., week=...)
  observed rows keyed to GBD names, with the GBD connector call to run next: get_aligned_with_gbd(condition=..., \
locations=[...])
  where a number came from (URL, retrieved_at, sha256, version, citation): get_provenance(source_id)
  what the data can't tell you: list_caveats(source_id=..., condition=...)
  published papers and abstracts: find_prior_work(condition=...)
  last resort, only when no tool above fits: query_sql(sql=<one read-only SELECT>)

━━━ WORDS THAT CHANGE THE NUMBER ━━━
  "so far this year", "year to date": a running total read at ONE week (the latest). Don't add weeks or running \
totals together yourself: weekly counts undercount the year, and a tool that adds them says so.
  "this year" for a running total is provisional; say so.
  clinical vs screening, confirmed vs probable, or any two condition labels: different series; keep them apart, \
and if a figure across them is asked for, give the tool's note with it (C. auris screening: screening detections, \
not incidence).
  rates, percents and estimates: don't add them up yourself; a tool that works out such a figure says what it \
can't mean.
  a county or city: pass it as a location; the place resolver matches it to the source's own place labels, and a \
source that stops at the state declines with nearest_served (for example "Travis County, Texas" → Texas).
  a GBD cause or a modelled measure (DALYs, YLLs, modelled deaths): use the IHME GBD connector; this server \
names the GBD cause in outside_registry only.

━━━ WHEN A TOOL DECLINES ━━━
  Follow its next_step. did_you_mean lists close spellings of what was asked: check one means what was meant \
before using it. nearest_served is the closest thing served here, not the same thing: say so. served_elsewhere \
is the publisher's own page; filter_matched_nothing lists the values that do exist.

The user is a doctor: plain words, one step at a time. Never ask for or accept patient information.
Every number comes from a tool response, with its source and caveats. If no tool returned a number, there is no \
number.
"""


PAGE_ARGS = "page: 1-based page of a long answer; pin: copy it from the previous page's Next line."


def _safe(fn: Callable[[], Out], fmt: Fmt) -> Out:
    """No stack trace ever reaches a clinician: any unexpected failure becomes a plain message."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - the whole point is that nothing escapes unworded
        return tools.refusal(e, fmt)


# Runs one tool's body and returns its answer, or a plain-words refusal; it never raises. The hosted server's guard
# (hosted.CallGuard) adds one deadline per call, one engine user at a time, the result-size cap and counters.
Guard = Callable[[str, Callable[[], Out], Fmt], Out]


def build_server(
    registry: Registry | None = None, engine: Engine | None = None, *, guard: Guard | None = None
) -> MCPServer:
    reg = registry or load_registry()
    eng = engine or Engine(registry=reg, lake=lake_root())
    mcp = MCPServer("health-data-registry", version=__version__, instructions=INSTRUCTIONS)

    def safe(tool: str, fn: Callable[[], Out], fmt: Fmt) -> Out:
        return guard(tool, fn, fmt) if guard is not None else _safe(fn, fmt)

    def paged_tool(fn: Callable[..., Out]) -> Callable[..., Out]:
        """A tool that returns rows: its description says how long answers come in pages."""
        doc = inspect.cleandoc(fn.__doc__ or "")
        return mcp.tool(annotations=READ_ONLY, description=f"{doc}\n\n{PAGE_ARGS} {helpers.TOOL_PAGING}")(fn)

    @mcp.tool(annotations=READ_ONLY)
    def list_sources(
        condition: str | None = None, geography_level: str | None = None, fmt: Fmt = "csv"
    ) -> Out:
        """What's loaded: each source's publisher, years, places, how often it updates, when it was last fetched,
        its licence and whether its contract has been reviewed. geography_level: nation, state, county, country."""
        return safe(
            "list_sources",
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
        return safe(
            "search_datasets",
            lambda: tools.search_datasets(
                reg, topic=topic, id_area=id_area, geography=geography, year_start=year_start, year_end=year_end,
                engine=eng, fmt=fmt,
            ),
            fmt,
        )  # fmt: skip

    @mcp.tool(annotations=READ_ONLY)
    def describe_dataset(source_id: str, fmt: Fmt = "csv") -> Out:
        """A source explained in clinical words: what one row is, the conditions and case definitions, what each
        column means, counts vs rates, missing codes, total rows, caveats, licence and how to cite it."""
        return safe("describe_dataset", lambda: tools.describe_dataset(reg, source_id, eng, fmt=fmt), fmt)

    @mcp.tool(annotations=READ_ONLY)
    def suggest_datasets(question: str, fmt: Fmt = "csv") -> Out:
        """For a research question in plain words: which sources can answer it, which can't and why."""
        return safe(
            "suggest_datasets", lambda: tools.suggest_datasets(reg, question, engine=eng, fmt=fmt), fmt
        )

    @paged_tool
    def get_series(
        condition: str,
        locations: list[str] | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
        measure: str | None = None,
        sex: str | None = None,
        source_id: str | None = None,
        page: int | None = None,
        pin: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """Observed values over time for a condition (e.g. "legionellosis") in places (e.g. ["Michigan", "Ohio",
        "United States"]), one row per place and year, each with its source, fetch time and case definition.
        measure: cases, deaths, rate, percent. Counts are added up; rates are shown as published, never added.
        Running totals give two observations per year: year_end_ytd (provisional) and
        subsequent_year_restatement (unverified, not a final count). The flag column gives the publisher's
        reason for an empty value."""
        return safe(
            "get_series",
            lambda: tools.get_series(
                reg, eng, condition=condition, locations=locations or [], year_start=year_start, year_end=year_end,
                measure=measure, sex=sex, source_id=source_id, page=page, pin=pin, fmt=fmt,
            ),
            fmt,
        )  # fmt: skip

    @paged_tool
    def get_aligned_with_gbd(
        condition: str,
        locations: list[str] | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
        page: int | None = None,
        pin: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """The same observed rows keyed to GBD location and cause names, plus the exact IHME GBD connector call
        to run next. This server never calls GBD and never judges the two against each other."""
        return safe(
            "get_aligned_with_gbd",
            lambda: tools.get_aligned_with_gbd(
                reg, eng, condition=condition, locations=locations or [], year_start=year_start, year_end=year_end,
                page=page, pin=pin, fmt=fmt,
            ),
            fmt,
        )  # fmt: skip

    @paged_tool
    def get_rate(
        condition: str,
        year: int,
        locations: list[str] | None = None,
        page: int | None = None,
        pin: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """How common is it here? Crude rate per 100,000 (cases / Census population) with an exact Poisson 95%
        interval, computed here and never age-standardised. Suppressed counts stay empty, never zero; the source's
        own unreliable-rate rule is applied when its contract records one."""
        return safe(
            "get_rate",
            lambda: analysis.get_rate(
                reg,
                eng,
                condition=condition,
                locations=locations or [],
                year=year,
                page=page,
                pin=pin,
                fmt=fmt,
            ),
            fmt,
        )

    @paged_tool
    def compare_rates(
        condition: str,
        location: str,
        year: int,
        other_location: str | None = None,
        other_year: int | None = None,
        page: int | None = None,
        pin: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """Is it higher here than there, or now than then? Rate ratio and rate difference with 95% intervals,
        between two places in one year (other_location) or one place in two years (other_year)."""
        return safe(
            "compare_rates",
            lambda: analysis.compare_rates(
                reg, eng, condition=condition, location=location, year=year, other_location=other_location,
                other_year=other_year, page=page, pin=pin, fmt=fmt,
            ),
            fmt,
        )  # fmt: skip

    @paged_tool
    def get_trend(
        condition: str,
        location: str,
        year_start: int | None = None,
        year_end: int | None = None,
        include_provisional: bool = False,
        fit_on: Literal["rates", "counts"] = "rates",
        page: int | None = None,
        pin: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """Is it rising? Annual percent change with a 95% interval from a Poisson regression on year. Split where
        the case definition changed; provisional years are left out unless include_provisional is true; needs at
        least 3 usable years. fit_on "rates" (default) needs Census population for every year and refuses
        otherwise; "counts" is a trend in case counts that ignores population change. Rates from counts that are
        not incidence (C. auris screening) are fitted when asked and labelled so, with the clinical trend beside."""
        return safe(
            "get_trend",
            lambda: analysis.get_trend(
                reg, eng, condition=condition, location=location, year_start=year_start, year_end=year_end,
                include_provisional=include_provisional, fit_on=fit_on, page=page, pin=pin, fmt=fmt,
            ),
            fmt,
        )  # fmt: skip

    @paged_tool
    def age_adjust(
        condition: str,
        location: str,
        year: int,
        page: int | None = None,
        pin: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """Does age explain the difference? Rate directly standardised to the 2000 US standard population, with
        the gamma (Fay-Feuer) interval NCHS uses. Runs only when case counts and populations exist for the same
        age groups; never comparable with GBD's age-standardised rates."""
        return safe(
            "age_adjust",
            lambda: analysis.age_adjust(
                reg, eng, condition=condition, location=location, year=year, page=page, pin=pin, fmt=fmt
            ),
            fmt,
        )

    @paged_tool
    def check_signal(
        condition: str,
        location: str,
        year: int,
        week: int,
        page: int | None = None,
        pin: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """Is this week unusual? The last 4 weeks' cases against the same, preceding and following 4 weeks of the
        past 5 years, in the style of CDC's historical limits for notifiable diseases. A signal is not an outbreak.
        A week with no number (CDC's '-', read as not reported) is never counted as zero: the 4-week totals add the
        weeks that have a number and name the ones that don't, and the signal says when the current total may be
        too low."""
        return safe(
            "check_signal",
            lambda: analysis.check_signal(
                reg,
                eng,
                condition=condition,
                location=location,
                year=year,
                week=week,
                page=page,
                pin=pin,
                fmt=fmt,
            ),
            fmt,
        )

    @paged_tool
    def get_provenance(
        source_id: str,
        year: int | None = None,
        page: int | None = None,
        pin: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """Where a source's numbers came from: the exact URLs, when they were fetched, content hashes, case
        definitions and how to cite them."""
        return safe(
            "get_provenance",
            lambda: tools.get_provenance(
                reg, eng, source_id=source_id, year=year, page=page, pin=pin, fmt=fmt
            ),
            fmt,
        )

    @mcp.tool(annotations=READ_ONLY)
    def list_caveats(source_id: str | None = None, condition: str | None = None, fmt: Fmt = "csv") -> Out:
        """What the data can't tell you: geography traps, suppression, provisional years, unreviewed contracts
        and facts not yet confirmed with the publisher."""
        return safe(
            "list_caveats",
            lambda: tools.list_caveats(reg, source_id=source_id, condition=condition, fmt=fmt),
            fmt,
        )

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
        return safe(
            "find_prior_work",
            lambda: tools.find_prior_work(reg, condition=condition, geography=geography, years=years,
                                          dataset=dataset, fmt=fmt),
            fmt,
        )  # fmt: skip

    @mcp.tool(annotations=READ_ONLY)
    def get_publication_outlook(
        conference: str | None = None,
        design: str | None = None,
        data_sources: list[str] | None = None,
        condition: str | None = None,
        geography: str | None = None,
        fmt: Fmt = "csv",
    ) -> Out:
        """How often presented abstracts at a conference (e.g. "IDWeek", "CROI") became full papers: one row per
        stage from idea to paper, each with its source, n and 95% interval, or "unknown". Base rate only: never a
        probability for this idea, never one number multiplied across stages, never a rate borrowed from another
        meeting. Without a conference it lists every conference side by side. design, data_sources, condition
        and geography are recorded but not used yet."""
        return safe(
            "get_publication_outlook",
            lambda: publication.get_publication_outlook(
                conference=conference, design=design, data_sources=data_sources, condition=condition,
                geography=geography, fmt=fmt,
            ),
            fmt,
        )  # fmt: skip

    @paged_tool
    def query_sql(sql: str, page: int | None = None, pin: str | None = None, fmt: Fmt = "csv") -> Out:
        """Escape hatch for questions the other tools don't cover: one read-only SELECT over loaded sources
        (tables are named by source_id). Writes, file reads and undeclared joins are refused; results are capped."""
        return safe("query_sql", lambda: tools.query_sql(eng, sql, page=page, pin=pin, fmt=fmt), fmt)

    for h in helpers.PROMPTS:
        _register_helper(mcp, h)

    return mcp


def _register_helper(mcp: MCPServer, h: helpers.Workflow) -> None:
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
    fn.__name__ = h.prompt_name
    fn.__doc__ = h.description
    mcp.prompt(name=h.prompt_name, title=h.title, description=h.description)(fn)


def main() -> None:
    build_server().run("stdio")
