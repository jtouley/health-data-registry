"""The read path: attach snapshots read-only, compile metrics to SQL, run guarded SQL, wrap every answer.

Every result carries the table, the SQL that produced it, the source and its version, and a one-line method,
so a clinician (or a grader) can check the number without trusting the model that relayed it.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from health_data_registry import deadline
from health_data_registry.registry import Column, Dataset, Metric, Registry, label_key
from health_data_registry.sql_guard import GuardedQuery, guard_sql, harden, run_guarded
from health_data_registry.store import Manifest, latest_snapshot, snapshot_dir

MAX_ROWS = 5000
MAX_PINNED = 2  # engines kept on older pinned versions, so the next page of an answer needn't rebuild one
LOADED_VALUES_TIMEOUT_S = 5.0  # describe_dataset never waits longer than this for one column's values
FilterValue = str | int | float | bool | Sequence[str | int | float | bool]


# What to do next after each kind of decline, in plain words. Every refusal carries one (DEFAULT_NEXT_STEP when
# its code has none of its own), so a clinician is never left at a dead end.
DEFAULT_NEXT_STEP = "Ask suggest_datasets with your whole question to see which sources can answer it."
NEXT_STEPS: dict[str, str] = {
    "unknown_condition": "Open describe_dataset for a source search_datasets lists, pick one of its condition "
    "labels, and ask again with that label.",
    "filter_matched_nothing": "Ask again with one of the values listed as available.",
    "no_snapshot": "Run the Something's wrong helper, which walks you through loading this source again.",
    "no_place_breakdown": "Ask again without places, or ask which sources report by state.",
    "too_many_rows": "Ask again for fewer places or a shorter span of years.",
    "no_sex_split": "Ask again without sex.",
    "index_not_built": "Search PubMed for the condition and place in the meantime.",
    "no_denominator": "Use get_series for the counts, or a rate the source publishes (describe_dataset says).",
    "ambiguous_condition": "Ask again with the full label of one series as the condition.",
    "nothing_to_compare": "Ask again with other_location or other_year.",
    "no_rate": "Use get_rate to see which places and years have both a count and a population.",
    "too_few_points": "Ask for a longer span of years, or use get_series to read the counts.",
    "would_combine_rates": "Use describe_dataset to see the filters, and ask for one published group at a time.",
    "publisher_unreachable": "Try again in a few minutes.",
    "publisher_bad_table": "Try again later; if it keeps happening, run the Something's wrong helper.",
    "needs_single_value": "Ask for the year's total, or for one week.",
    "not_a_human_count": "Use get_series to read these counts as published; they are not cases in people, so "
    "there is no rate per 100,000 people.",
    "bad_page": "Ask for page=1, or leave page out.",
    "no_such_page": "Ask for a page the answer has, or leave page out for page 1.",
    "bad_pin": "Copy the Next line from the previous page exactly, or ask again without page and pin.",
    "page_data_changed": "Ask the same question again without page and pin, and set the older pages aside.",
    "page_rows_changed": "Ask the same question again without page and pin, and set the older pages aside.",
    "too_slow": "Try again in a few minutes, or narrow the question: one condition, fewer places or fewer years.",
    "busy": "Try again in a minute.",
    "too_large": "Ask again for fewer columns or one measure.",
}


def next_step_for(code: str | None) -> str:
    return NEXT_STEPS.get(code or "", DEFAULT_NEXT_STEP)


class QueryRefused(ValueError):
    """The question can't be answered this way; the message says why in plain words, next_step what to do."""

    def __init__(self, code: str, message: str, next_step: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.next_step = next_step or next_step_for(code)


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _single(value: Any) -> bool:
    """A filter that pins a dimension to exactly one value."""
    if value is None:
        return False
    return len(value) == 1 if isinstance(value, list | tuple) else True


def live_refusal(ds: Dataset, e: BaseException) -> QueryRefused:
    """A plain-words refusal for a live read that failed, with what the clinician can do instead."""
    from health_data_registry.adapters.pdf_tables import LayoutError
    from health_data_registry.deadline import DeadlineExceeded
    from health_data_registry.fetch import FetchRefused

    e_ = ds.entry
    page = e_.source.landing_url
    if isinstance(e, LayoutError):
        return QueryRefused(
            "publisher_layout_unreadable",
            f"{e_.publisher}'s report layout for {e_.title} couldn't be read, or its own totals didn't check out, "
            "so no numbers from it are shown: reading a changed layout needs a reviewed update here, and asking "
            f"again won't fix it. Read the table in the report itself from {page}, or ask about another source; "
            "the Something's wrong helper tells the maintainers.",
        )
    if isinstance(e, FetchRefused) and e.code == "blocked":
        return QueryRefused(
            "publisher_blocked",
            f"{e_.publisher}'s website refused this program's requests, so {e_.title} was not read; this is recorded "
            f"and not worked around. Open the reports yourself at {page}.",
        )
    if isinstance(e, FetchRefused) and e.code == "robots_disallowed":
        return QueryRefused(
            "publisher_disallows_reading",
            f"{e_.publisher}'s website asks automated tools not to read it, so {e_.title} was not read. Open the "
            f"reports yourself at {page}.",
        )
    if isinstance(e, DeadlineExceeded):
        return QueryRefused(
            "publisher_too_slow",
            f"Reading {e_.title} from {e_.publisher} would take longer than one question may wait, so it stopped. "
            f"Open the reports yourself at {page}, or ask about another source.",
        )
    return QueryRefused(
        "publisher_unreachable",
        f"{e_.publisher} could not be reached for {e_.title}, which is read live from the "
        "publisher because its terms don't allow us to keep a copy. Try again in a few minutes.",
    )


@dataclass(frozen=True)
class CompiledMetric:
    sql: str
    params: list[Any]
    columns: list[str]
    method: str


def compile_metric(
    ds: Dataset,
    metric_name: str,
    *,
    filters: Mapping[str, FilterValue] | None = None,
    group_by: Sequence[str] = (),
    period: Mapping[str, Any] | None = None,
) -> CompiledMetric:
    sem = ds.semantic
    try:
        metric = sem.metric(metric_name)
    except KeyError:
        names = ", ".join(m.name for m in sem.metrics)
        raise QueryRefused(
            "unknown_metric", f"{ds.id} has no metric {metric_name!r}. Metrics: {names}"
        ) from None
    measure = sem.measure(metric.measure)
    dims = {d.name: d for d in sem.dimensions}
    for d in metric.single_value_of:
        if measure.kind == "running_count":
            if d in group_by or not _single((filters or {}).get(d)):
                raise QueryRefused(
                    "needs_single_value",
                    f"{metric.name} is a running total, so it is read at one {d}: filter {d} to a single value "
                    f"(normally the latest) and don't group by it. Adding up the running totals of several {d}s "
                    "would count the same cases many times.",
                )
        # A count whose values of d overlap (Michigan includes Detroit) or are different things (conditions): each
        # answer row holds one value of d, by a filter to one value or by grouping by it; never added across them.
        elif d not in group_by and not _single((filters or {}).get(d)):
            raise QueryRefused(
                "needs_single_value",
                f"{metric.name} is never added up across {d}s: they overlap or count different things (for example "
                f"a state's count already includes its cities). Filter {d} to one value, or group by {d} to get one "
                "row each.",
            )

    def dim(name: str) -> str:
        if name not in dims:
            raise QueryRefused(
                "unknown_dimension", f"{ds.id} has no dimension {name!r}. Dimensions: {', '.join(dims)}"
            )
        return dims[name].column

    where: list[str] = [f"({f})" for f in metric.filters]
    params: list[Any] = []
    if metric.exclude_total_rows and ds.contract.total_rows:
        where.append(f"NOT coalesce(({ds.contract.total_rows}), false)")
    for name, value in (filters or {}).items():
        col = _q(dim(name))
        if isinstance(value, list | tuple):
            if not value:
                raise QueryRefused("empty_filter", f"filter {name} has no values")
            where.append(f"{col} IN ({', '.join('?' for _ in value)})")
            params.extend(value)
        else:
            where.append(f"{col} = ?")
            params.append(value)
    period_where: list[str] = []
    period_params: list[Any] = []
    period_col: str | None = None
    if period:
        time_dims = [d for d in sem.dimensions if d.type == "time"]
        pname = str(period.get("dimension") or (time_dims[0].name if time_dims else ""))
        if not pname or dims.get(pname) is None or dims[pname].type != "time":
            raise QueryRefused("no_time_dimension", f"{ds.id}: period needs a time dimension")
        tcol = dims[pname].column
        period_col = pname
        col = _q(tcol)
        is_date = ds.contract.column(tcol).physical_type in ("DATE", "TIMESTAMP")
        start, end = period.get("start"), period.get("end")
        # A year asked of a date dimension means that whole calendar year.
        if is_date and isinstance(start, int):
            start = f"{start}-01-01"
        if is_date and isinstance(end, int):
            end = f"{end}-12-31"
        is_text = ds.contract.column(tcol).physical_type == "VARCHAR"
        if is_text:
            # Text periods ("2025", "2025-01-04", "2025-26") compare as text: a year bound covers every value
            # that starts with that year.
            start = None if start is None else str(start)
            end = None if end is None else str(end) + "\uffff"
        cast = f"CAST(? AS {ds.contract.column(tcol).physical_type})" if is_date else "?"
        if start is not None:
            period_where.append(f"{col} >= {cast}")
            period_params.append(start)
        if end is not None:
            period_where.append(f"{col} <= {cast}")
            period_params.append(end)

    group_cols = [dim(g) for g in group_by]
    select_dims = [f"{_q(c)} AS {_q(g)}" for g, c in zip(group_by, group_cols, strict=True)]
    m_col = _q(measure.column)
    bounds: list[tuple[str, str]] = []
    if metric.uncertainty:
        bounds = [
            ("lower", sem.measure(metric.uncertainty.lower).column),
            ("upper", sem.measure(metric.uncertainty.upper).column),
        ]
    table = _q(ds.id)
    flag = ds.contract.flag_column(measure.column)
    out_cols = [*group_by, metric.name, *(b for b, _ in bounds), *(["flag"] if flag else [])]
    if metric.aggregation == "cumulative_latest":
        return _compile_cumulative(
            ds, metric, where, params, period_where, period_params, period_col, group_by, filters or {}
        )
    where = where + period_where
    params = params + period_params
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    if metric.aggregation == "sum":
        # A group with any empty (suppressed or missing) value has no total: sum() would skip the empty rows and
        # return a smaller number as if it were the whole count. suppressed_rows says how many were empty. An
        # empty value whose flag is the codebook's "no reported cases" code is a zero, not a gap.
        zero = flag.no_cases_code if flag else None
        is_empty = f"{m_col} IS NULL" + (
            f" AND {_q(flag.name)} IS DISTINCT FROM {_lit(zero)}" if flag and zero is not None else ""
        )
        empty = f"count(*) FILTER (WHERE {is_empty})"
        total = f"coalesce(sum({m_col}), 0)" if zero is not None else f"sum({m_col})"
        sel = [
            *select_dims,
            f"CASE WHEN {empty} = 0 THEN {total} END AS {_q(metric.name)}",
            f"{empty} AS suppressed_rows",
        ]
        out_cols = [*group_by, metric.name, "suppressed_rows", *(["flag"] if flag else [])]
        if flag:
            # The publisher's flags on the empty cells the sum had to leave out, so a gap is never silent.
            f_col = _q(flag.name)
            sel.append(
                f"string_agg(DISTINCT {f_col}, ', ' ORDER BY {f_col}) FILTER (WHERE {is_empty}) AS flag"
            )
        sql = f"SELECT {', '.join(sel)} FROM {table}{where_sql}"  # noqa: S608 - quoted contract identifiers, bound values
        if group_cols:
            sql += " GROUP BY ALL ORDER BY ALL"
        method = (
            f"Sum of {measure.name} ({measure.description or measure.kind})"
            + (f" by {', '.join(group_by)}" if group_by else "")
            + (
                ", publisher total rows excluded"
                if metric.exclude_total_rows and ds.contract.total_rows
                else ""
            )
            + ". A group with any suppressed or missing value has no total (never a partial sum); "
            "suppressed_rows gives how many of its values were empty."
            + (
                f" An empty value flagged '{zero}' counts as 0: the publisher's codebook defines '{zero}' as no "
                "reported cases."
                if zero is not None
                else ""
            )
        )
    else:
        # Rates, percents and estimates are read as published, never added up. One row per group or refuse.
        sel = [*select_dims, f"{m_col} AS {_q(metric.name)}", *(f"{_q(c)} AS {_q(b)}" for b, c in bounds)]
        if flag:
            sel.append(f"{_q(flag.name)} AS flag")
        sql = f"SELECT {', '.join(sel)} FROM {table}{where_sql}"  # noqa: S608 - quoted contract identifiers, bound values
        if group_cols:
            sql += " ORDER BY ALL"
        method = f"{measure.name} as published ({measure.kind}); not summed." + (
            " Lower and upper are the publisher's own uncertainty bounds." if bounds else ""
        )
    return CompiledMetric(sql=sql, params=params, columns=out_cols, method=method)


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def label_key_sql(expr: str, renamed: Sequence[tuple[str, str]] = ()) -> str:
    """SQL for registry.label_key, then the contract's renamed labels mapped onto the label they continue."""
    key = f"regexp_replace(upper(trim(CAST({expr} AS VARCHAR))), '[.]', '', 'g')"
    if not renamed:
        return key
    whens = " ".join(f"WHEN {_lit(label_key(a))} THEN {_lit(label_key(b))}" for a, b in renamed)
    return f"CASE {key} {whens} ELSE {key} END"


def _flag_text(flag: Column | None, code_sql: str) -> str:
    """SQL text for a publisher flag: its code and, where the contract quotes the codebook, what it means."""
    if flag is None or not flag.flag_codes:
        return f"'''' || {code_sql} || ''''"
    whens = " ".join(f"WHEN {_lit(c)} THEN {_lit(f"'{c}' = {m}")}" for c, m in flag.flag_codes)
    return f"CASE {code_sql} {whens} ELSE '''' || {code_sql} || '''' END"


def _compile_cumulative(
    ds: Dataset,
    metric: Metric,
    where: list[str],
    params: list[Any],
    period_where: list[str],
    period_params: list[Any],
    period_col: str | None,
    group_by: Sequence[str],
    filters: Mapping[str, FilterValue] = {},  # noqa: B006 - read only
) -> CompiledMetric:
    """A running total read at the last week that has a number in each period (e.g. NNDSS year-to-date).

    The next period's restatement (NNDSS: next year's prior-year column) is returned beside it, never in its
    place: the publisher's codebook doesn't say the two count the same thing, and for some conditions they
    differ widely. Each value says the week it comes from; neither is called final.
    """
    sem = ds.semantic
    time_dims = [d.name for d in sem.dimensions if d.type == "time" and d.name != metric.order_by]
    tname = period_col or (time_dims[0] if time_dims else None)
    if tname is None or tname not in group_by:
        raise QueryRefused(
            "needs_period",
            f"{metric.name} is a running total, so it is read once per {tname or 'period'}; "
            f"group by {tname or 'the time dimension'}.",
        )
    if tname == metric.order_by:
        raise QueryRefused(
            "needs_period", f"{metric.name} can't be grouped by {tname}, the within-period order."
        )
    # A running total is read once per group: with a place or condition neither grouped nor pinned, the last
    # week would pick one group's total at random instead of adding them up.
    loose = [
        d.name
        for d in sem.dimensions
        if d.type == "categorical" and d.name not in group_by and not _single(filters.get(d.name))
    ]
    if loose:
        raise QueryRefused(
            "needs_grouping",
            f"{metric.name} is a running total kept separately for each {', '.join(loose)}, so it can't be read "
            f"for several at once: group by {', '.join(loose)} or filter each to one value.",
        )
    order_by = str(metric.order_by)
    col = {d.name: d.column for d in sem.dimensions}
    ob = _q(col[order_by])
    unit = "week" if "week" in order_by else order_by.replace("_", " ")
    span = "year" if "year" in tname else "period"
    others = [g for g in group_by if g != tname]
    # Publishers re-case and rename labels between years (NNDSS: MICHIGAN in 2024, Michigan from 2025), so a
    # period and its restatement are matched on the label key, with the contract's renamed labels mapped back.
    keys = [
        f"{label_key_sql(_q(col[g]), ds.contract.column(col[g]).renamed_labels)} AS {_q(g)}" for g in others
    ]
    t = _q(col[tname])
    m_col = sem.measure(metric.measure).column
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    def running(value_col: str, period_sql: str) -> str:
        """Per group and period: the last number published, the week it comes from, the period's last week,
        and the flag at that last week."""
        v = _q(value_col)
        fc = ds.contract.flag_column(value_col)
        last_flag = f"arg_max(coalesce({_q(fc.name)}, ''), {ob})" if fc else "''"
        cols = ", ".join([*keys, f"{period_sql} AS {_q(tname)}"])
        aggs = (
            f"arg_max({v}, {ob}) FILTER (WHERE {v} IS NOT NULL) AS v, "
            f"max({ob}) FILTER (WHERE {v} IS NOT NULL) AS v_at, max({ob}) AS last_at, {last_flag} AS last_flag"
        )
        if fc is not None and fc.no_cases_code is not None:
            # Every empty value in the period carries the codebook's "no reported cases" code.
            aggs += (
                f", coalesce(bool_and(coalesce({_q(fc.name)}, '') = {_lit(fc.no_cases_code)}) "
                f"FILTER (WHERE {v} IS NULL), false) AS none_reported"
            )
        return f"SELECT {cols}, {aggs} FROM base GROUP BY ALL"  # noqa: S608 - quoted contract identifiers

    m_flag = ds.contract.flag_column(m_col)
    gap = (
        f"'; later {unit}s have no number' || CASE WHEN c.last_flag <> '' THEN ' (flag at {unit} ' || c.last_at"
        f" || ': ' || {_flag_text(m_flag, 'c.last_flag')} || ')' ELSE '' END"
    )
    zero = m_flag.no_cases_code if m_flag else None
    # A period whose running total is empty in every week, each flagged with the codebook's "no reported cases"
    # code, has no reported cases: read as 0 and labelled so (never stored as a number).
    none_status = (
        f" WHEN c.v IS NULL AND c.none_reported THEN CASE WHEN {{ended}} THEN 'provisional: as published at {span} "
        f"end: no reported cases (through {unit} ' || c.last_at || '; flag ''{zero}'' = no reported cases)' ELSE "
        f"'provisional: running total so far: no reported cases (through {unit} ' || c.last_at || '; flag "
        f"''{zero}'' = no reported cases)' END"
        if zero is not None
        else ""
    )
    status = (
        f"CASE{none_status} WHEN c.v IS NULL THEN 'no number published through {unit} ' || c.last_at"
        f" || CASE WHEN c.last_flag <> '' THEN ' (flag: ' || {_flag_text(m_flag, 'c.last_flag')} || ')' ELSE '' END"
        f" WHEN c.v_at < c.last_at THEN 'incomplete: running total last published at {unit} ' || c.v_at"
        f" || ' of ' || c.last_at || {gap} || ', so this is not the {span} total'"
        f" WHEN {{ended}} THEN 'provisional: as published at {span} end (through {unit} ' || c.v_at || ')'"
        f" ELSE 'provisional: running total so far (through {unit} ' || c.v_at || ')' END"
    )
    flag_out = "CASE WHEN c.v_at IS DISTINCT FROM c.last_at THEN nullif(c.last_flag, '') END"
    ctes = f"WITH base AS (SELECT * FROM {_q(ds.id)}{where_sql}), cur AS ({running(m_col, t)})"  # noqa: S608 - quoted contract identifiers
    value = "CASE WHEN c.v IS NULL AND c.none_reported THEN 0 ELSE c.v END" if zero is not None else "c.v"
    sel = [*(f"c.{_q(g)}" for g in group_by), f"{value} AS {_q(metric.name)}"]
    columns = [*group_by, metric.name, "period_status", "flag"]
    join = ""
    if metric.restated_by:
        r_col = sem.measure(metric.restated_by.measure).column
        r_flag = ds.contract.flag_column(r_col)
        ctes += f", nxt AS ({running(r_col, f'{t} - 1')})"
        join = " LEFT JOIN nxt n USING (" + ", ".join(_q(g) for g in [*others, tname]) + ")"
        status = status.replace("{ended}", "n.last_at IS NOT NULL")
        # Until a domain reviewer approves the two as the same count (resolution_policy), the next table's figure
        # is an unverified restatement: never a final disease count, never in place of the year's own total.
        r_status = (
            f"CASE WHEN n.last_at IS NULL THEN 'no next-{span} table yet'"
            f" WHEN n.v IS NULL THEN 'unverified_restatement: next {span}''s table has no number for this {span}'"
            f" || CASE WHEN n.last_flag <> '' THEN ' (flag: ' || {_flag_text(r_flag, 'n.last_flag')} || ')' ELSE '' END"
            f" ELSE 'unverified_restatement: as published in next {span}''s table (through {unit} ' || n.v_at || ')'"
            f" || CASE WHEN n.v_at < c.last_at THEN '; covers {unit}s 1-' || n.v_at || ' of ' || c.last_at"
            f" || ' only, not the whole {span}' ELSE '' END || '; not a final disease count' END"
        )
        sel += [
            f"{status} AS period_status",
            f"{flag_out} AS flag",
            "n.v AS restated",
            f"{r_status} AS restated_status",
            "CASE WHEN n.v_at IS DISTINCT FROM n.last_at THEN nullif(n.last_flag, '') END AS restated_flag",
        ]
        columns += ["restated", "restated_status", "restated_flag"]
    else:
        # Without a restatement there is no sign the period has ended, so the last week read is provisional.
        sel += [f"{status.replace('{ended}', 'false')} AS period_status", f"{flag_out} AS flag"]
    outer_where = [w.replace(t, f"c.{_q(tname)}", 1) for w in period_where]
    sql = f"{ctes} SELECT {', '.join(sel)} FROM cur c{join}"  # noqa: S608 - quoted contract identifiers, bound values
    if outer_where:
        sql += " WHERE " + " AND ".join(outer_where)
    sql += " ORDER BY ALL"
    method = (
        f"Running total ({sem.measure(metric.measure).name}) read at the last {unit} of each {span} that has a "
        f"number, labelled with that {unit}"
        + (
            f"; next {span}'s table's figure for the same {span} "
            f"({sem.measure(metric.restated_by.measure).name}) is kept as a separate observation, an unverified "
            f"restatement, with the {unit} it reaches. The two are never swapped or combined and neither is final "
            "until a domain reviewer approves that they count the same thing"
            if metric.restated_by
            else ""
        )
        + ("; publisher total rows excluded" if metric.exclude_total_rows and ds.contract.total_rows else "")
        + ". Weekly counts are never added up, because late reports land in the running total, not the week."
    )
    return CompiledMetric(sql=sql, params=[*params, *period_params], columns=columns, method=method)


def _live_enabled() -> bool:
    import os

    return os.environ.get("HDR_FETCH_AT_RUN_TIME", "1") != "0"


def _default_hub() -> Any:
    from health_data_registry.hub import Hub, cache_root, hub_from_env

    url = hub_from_env()
    return Hub(url=url, cache=cache_root()) if url else None


def _default_cache() -> Path:
    from health_data_registry.hub import cache_root

    return cache_root()


@dataclass
class Engine:
    """One in-memory DuckDB connection with every available source attached read-only, then locked down.

    A redistributable source comes from the local lake when it holds a snapshot (maintainers, CI), else from the
    published hub release (downloaded on first use, checked against its sha256, bounded cache). A restricted
    source (e.g. WHO GHO) is served live: fetched from the publisher per request, held in memory for at most
    live.LIVE_TTL_S (live.DASHBOARD_TTL_S for a dashboard read with the browser, stated in its provenance), and
    never written to disk (see live.py).
    """

    registry: Registry
    lake: Path
    manifests: dict[str, Manifest] = field(default_factory=dict)
    hub: Any = field(default_factory=_default_hub)  # hub.Hub | None
    cache: Path = field(default_factory=_default_cache)
    fetch_at_run_time: bool = field(default_factory=_live_enabled)
    getter: Any = None  # a crawl.Getter for live fetches; None builds the guarded HTTPS fetcher
    _con: duckdb.DuckDBPyConnection | None = None
    _hub_files: dict[str, tuple[Path, Manifest]] = field(default_factory=dict)
    _live: dict[str, Any] = field(default_factory=dict)  # dataset id -> live.LiveTable (memory only)
    _live_failed: set[str] = field(default_factory=set)  # live tables that could not be loaded (refused once)
    # Lake snapshots to read instead of LATEST (dataset id -> snapshot id): a later page pinned to older data.
    lake_pins: dict[str, str] = field(default_factory=dict)
    # The sources the current call has read, in order (paging.respond clears it per call and reads it after).
    touched: dict[str, None] = field(default_factory=dict)
    _pinned: OrderedDict[tuple[tuple[str, tuple[str, str | None]], ...], Engine] = field(
        default_factory=OrderedDict
    )

    def connect(self) -> duckdb.DuckDBPyConnection:
        # Every read starts here: a call past its deadline (one the hosted guard gave up on) stops before it
        # touches the connection, so it never uses the engine alongside the next call.
        deadline.check()
        if self._con is not None:
            return self._con
        from health_data_registry.live import load_live

        con = duckdb.connect(":memory:")
        parquet: list[str] = []
        for ds in self.registry:
            if not ds.entry.licence.redistribution:
                table = self._live.get(ds.id)
                if table is None:
                    continue  # restricted sources are never read from disk, even a maintainer's lake
                try:
                    load_live(con, ds, table.records)
                except Exception:  # noqa: BLE001 - one bad publisher table must not break the other sources
                    del self._live[ds.id]
                    self._live_failed.add(ds.id)
                    continue
                manifest = table.manifest
            elif (found := self._lake_snapshot(ds.id)) is not None:
                path, manifest = found
                alias = f"snap_{ds.id}"
                db = str(path / "data.duckdb").replace("'", "''")
                con.execute(f"ATTACH '{db}' AS {_q(alias)} (READ_ONLY)")
                con.execute(f"CREATE VIEW {_q(ds.id)} AS SELECT * FROM {_q(alias)}.{_q(ds.id)}")  # noqa: S608 - quoted contract identifiers, bound values
            elif ds.id in self._hub_files and not self._hub_files[ds.id][0].is_file():
                del self._hub_files[ds.id]  # removed from the cache: downloaded again on next use
                continue
            elif ds.id in self._hub_files:
                file, manifest = self._hub_files[ds.id]
                lit = str(file).replace("'", "''")
                con.execute(f"CREATE VIEW {_q(ds.id)} AS SELECT * FROM read_parquet('{lit}')")  # noqa: S608 - quoted identifier, verified cache path
                parquet.append(str(file))
            else:
                continue
            self.manifests[ds.id] = manifest
        harden(con, allowed_paths=parquet)
        self._con = con
        return con

    def _lake_snapshot(self, dataset_id: str) -> tuple[Path, Manifest] | None:
        sid = self.lake_pins.get(dataset_id)
        if sid is None:
            return latest_snapshot(self.lake, dataset_id)
        path = snapshot_dir(self.lake, dataset_id, sid)
        return (path, Manifest.read(path / "manifest.json")) if (path / "manifest.json").is_file() else None

    def _reconnect(self) -> None:
        if self._con is not None:
            self._con.close()
        self._con = None
        self.manifests.clear()
        self.connect()

    def _obtain(self, ds: Dataset) -> bool:
        """Bring one source in: from the hub release, or live from the publisher (memory only)."""
        from health_data_registry.deadline import DeadlineExceeded
        from health_data_registry.hub import HubError

        if ds.entry.licence.redistribution:
            if self.hub is None:
                return False
            try:
                self._hub_files[ds.id] = self.hub.snapshot(ds)
            except HubError as e:
                raise QueryRefused(e.code, str(e)) from e
            return True
        if not self.fetch_at_run_time:
            return False
        from health_data_registry import deadline
        from health_data_registry.crawl import fetcher, shared_robots
        from health_data_registry.live import LIVE_READ_BUDGET_S, fetch_live

        # One access policy per server process: two live sources on one host keep its pace between them.
        get = self.getter or fetcher(self.registry, None, robots=shared_robots(self.registry))
        # A question's live read has its own time budget (a call deadline, nested inside any hosted one), except a
        # dashboard's, which takes minutes and is kept for an hour.
        bound: AbstractContextManager[None] = (
            nullcontext() if ds.entry.source.browser else deadline.call_deadline(LIVE_READ_BUDGET_S)
        )
        try:
            with bound:
                self._live[ds.id] = fetch_live(ds, get)
        except DeadlineExceeded as e:
            left = deadline.remaining()
            if left is not None and left <= 0:
                raise  # the hosted call's budget ran out: its own plain message, not "unreachable"
            raise live_refusal(ds, e) from e
        except Exception as e:  # noqa: BLE001 - any publisher failure becomes one plain refusal
            raise live_refusal(ds, e) from e
        return True

    def manifest(self, dataset_id: str) -> Manifest:
        self.connect()
        ds = self.registry.datasets.get(dataset_id)
        live = self._live.get(dataset_id)
        if ds is not None and live is not None and not live.fresh():
            del self._live[dataset_id]  # past the in-memory limit: read the publisher again
            self._reconnect()
        m = self.manifests.get(dataset_id)
        if m is None and ds is not None and self._obtain(ds):
            self._reconnect()
            m = self.manifests.get(dataset_id)
        if m is None and dataset_id in self._live_failed:
            self._live_failed.discard(dataset_id)  # the next question reads the publisher afresh
            raise QueryRefused(
                "publisher_bad_table",
                f"{ds.entry.publisher if ds else 'The publisher'} sent a table for {dataset_id} that could not be "
                "read (a value did not match its column), so no numbers are shown for it. Other sources are not "
                "affected. Try again later; if it keeps happening, run the Something's wrong helper.",
            )
        if m is not None:
            self.touched[dataset_id] = None
        if m is None:
            raise QueryRefused(
                "no_snapshot",
                f"This setup has reading data turned off for {dataset_id} (neither the open data hub nor the "
                "publisher is read), so there are no numbers to show.",
            )
        return m

    def _serves(self, pins: Mapping[str, tuple[str, str | None]]) -> bool:
        """Does this engine read exactly these versions (snapshot id, hub release tag) of these sources?"""
        for dataset_id, version in pins.items():
            if dataset_id not in self.registry.datasets:
                return False
            try:
                m = self.manifest(dataset_id)
            except QueryRefused:
                return False
            if (m.snapshot_id, m.release_ref) != version:
                return False
        return True

    def _hub_at(self, pins: Mapping[str, tuple[str, str | None]]) -> Any:
        """The hub read at the pinned release tag, when the pins name one and this hub can be read at another."""
        from health_data_registry.hub import RELEASE_TAG, Hub

        refs = {r for _, r in pins.values() if r}
        if not refs or self.hub is None:
            return self.hub
        ref = refs.pop()
        # One answer reads one release; a local hub copy has only its own; and a pin may only name a tagged
        # release, never a bare commit (which could be any unreleased or pull-request commit of the hub).
        if refs or not self.hub.is_remote or not RELEASE_TAG.fullmatch(ref):
            return None
        if self.hub.revision() == ref:
            return self.hub
        return Hub(url=f"{self.hub.url.split('@')[0]}@{ref}", cache=self.cache, max_bytes=self.hub.max_bytes,
                   api=self.hub.api)  # fmt: skip

    def at(self, pins: Mapping[str, tuple[str, str | None]]) -> Engine | None:
        """An engine that reads exactly the pinned versions (this one when it does), or None when they are gone.

        A later page names the versions its first page read. This engine serves them if it still does; otherwise
        an engine on the pinned hub release or lake snapshot (kept for the next page, at most two) serves them;
        restricted sources are never re-read for an old page, because their copies are never kept.
        """
        key = tuple(sorted(pins.items()))
        try:
            if self._serves(pins):
                return self
            if key in self._pinned:
                self._pinned.move_to_end(key)
                return self._pinned[key]
            hub = self._hub_at(pins)
            if hub is None and any(r for _, r in pins.values()):
                return None
            alt = Engine(registry=self.registry, lake=self.lake, hub=hub, cache=self.cache, fetch_at_run_time=False,
                         getter=self.getter, lake_pins={i: s for i, (s, r) in pins.items() if r is None})  # fmt: skip
            if not alt._serves(pins):
                alt.close(release_hub=alt.hub is not self.hub)
                return None
        except deadline.DeadlineExceeded:
            raise  # out of time is not "the data changed" (DeadlineExceeded is an OSError)
        except (QueryRefused, ValueError, OSError):  # an old release that can't be read is gone
            return None
        self._pinned[key] = alt
        while len(self._pinned) > MAX_PINNED:
            _, old = self._pinned.popitem(last=False)
            old.close(release_hub=old.hub is not self.hub)
        return alt

    def engines(self) -> list[Engine]:
        """This engine and the pinned ones it keeps: every connection a call may be using."""
        return [self, *self._pinned.values()]

    def close(self, *, release_hub: bool = True) -> None:
        """Close the connection and (unless another engine shares the hub) give back the hub files its views
        read, so the cache may evict them."""
        if self._con is not None:
            self._con.close()
            self._con = None
        if release_hub and hasattr(self.hub, "release_files"):
            self.hub.release_files()

    def provenance(self, dataset_id: str) -> dict[str, Any]:
        """Where a source's numbers come from, without downloading anything: origin, contract, product, release."""
        from health_data_registry.product import load_ports
        from health_data_registry.publish import HF_PREFIX, add_to_claude

        ds = self.registry.get(dataset_id)
        self.connect()
        m = self.manifests.get(dataset_id)
        port = load_ports(self.registry.root).get(ds.contract.id)
        if m is not None:
            origin = {
                "lake": "the local lake (a maintainer's copy)",
                "hub": f"the open data hub, release {m.release_ref} ({m.release_id})",
                "publisher": f"{ds.entry.publisher}, read live at {m.retrieved_at[:16]} UTC and not stored",
            }.get(m.origin, m.origin)
        elif ds.entry.licence.redistribution:
            origin = (
                f"not loaded yet: the open data hub ({self.hub.url}) on first use"
                if self.hub is not None
                else "not loaded yet: no open data hub is configured (HDR_HUB)"
            )
        else:
            origin = (
                f"{ds.entry.publisher}, read live when asked (never stored)"
                if self.fetch_at_run_time
                else "not available: reading this publisher live is turned off"
            )
        return {
            "data_from": origin,
            "contract_id": ds.contract.id,
            "contract_version": ds.contract.version,
            "release_contract_version": m.release_contract_version if m else None,
            "data_product": port.product_id if port else None,
            "output_port": port.name if port else None,
            "release_id": m.release_id if m else None,
            "release_ref": m.release_ref if m else None,
            "run_date": m.run_date if m else None,
            "release_files": dict(m.release_files) if m else {},
            "quality": dict(m.quality) if m and m.quality else {"tool": None, "results": []},
            "add_to_claude": add_to_claude(
                ds,
                hub_url=self.hub.url.split("@")[0]
                if self.hub is not None and self.hub.url.startswith(HF_PREFIX)
                else None,
                tag=m.release_ref if m else None,
            ),
        }

    def query_metric(
        self,
        dataset_id: str,
        metric: str,
        *,
        filters: Mapping[str, FilterValue] | None = None,
        group_by: Sequence[str] = (),
        period: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ds = self.registry.get(dataset_id)
        manifest = self.manifest(dataset_id)
        cm = compile_metric(ds, metric, filters=filters, group_by=group_by, period=period)
        con = self.connect()
        rows = con.execute(cm.sql + f" LIMIT {MAX_ROWS + 1}", cm.params).fetchall()
        m = ds.semantic.metric(metric)
        if m.aggregation == "none" and group_by:
            keys = [tuple(r[: len(group_by)]) for r in rows]
            if len(keys) != len(set(keys)):
                raise QueryRefused(
                    "would_combine_rates",
                    f"{metric} is a {ds.semantic.measure(m.measure).kind} and more than one published value "
                    f"matches some {', '.join(group_by)} groups. Rates and estimates can't be added up: "
                    "filter or group by the other dimensions too (describe_dataset lists them).",
                )
        elif m.aggregation == "none" and len(rows) > 1:
            dims = ", ".join(d.name for d in ds.semantic.dimensions)
            raise QueryRefused(
                "would_combine_rates",
                f"{metric} is published per group and can't be added up; choose group_by from: {dims}.",
            )
        method = f"{manifest.notes[0]} {cm.method}" if manifest.origin == "publisher" else cm.method
        return envelope(ds, manifest, cm.columns, rows, sql=cm.sql, params=cm.params, method=method)

    def distinct_values(self, dataset_id: str, column: str) -> list[Any]:
        """Every value a dimension column takes in the snapshot (for matching place names)."""
        ds = self.registry.get(dataset_id)
        self.manifest(dataset_id)
        if column not in ds.contract.column_names:
            raise QueryRefused("unknown_column", f"{dataset_id} has no column {column}")
        rows = (
            self.connect()
            .execute(f"SELECT DISTINCT {_q(column)} FROM {_q(dataset_id)} ORDER BY 1")  # noqa: S608 - quoted contract identifiers
            .fetchall()
        )  # noqa: S608 - quoted contract identifiers
        return [r[0] for r in rows if r[0] is not None]

    def loaded_values(
        self, dataset_id: str, column: str, timeout_s: float = LOADED_VALUES_TIMEOUT_S
    ) -> list[Any] | None:
        """The values a column takes in a copy that is already on this computer, or None when there is none.

        Never downloads or reads the publisher (describe_dataset is a catalog lookup), and gives up after
        timeout_s, so a slow or broken copy can't hold up the answer."""
        ds = self.registry.get(dataset_id)
        try:
            con = self.connect()
        except (duckdb.Error, OSError):
            return None
        if dataset_id not in self.manifests or column not in ds.contract.column_names:
            return None
        timer = threading.Timer(timeout_s, con.interrupt)
        timer.start()
        try:
            rows = con.execute(f"SELECT DISTINCT {_q(column)} FROM {_q(dataset_id)} ORDER BY 1").fetchall()  # noqa: S608 - quoted contract identifiers
        except (duckdb.Error, OSError):
            return None
        finally:
            timer.cancel()
        return [r[0] for r in rows if r[0] is not None]

    def query_sql(self, sql: str) -> dict[str, Any]:
        self.connect()
        available = {ds.id: ds.contract.column_names for ds in self.registry}
        q: GuardedQuery = guard_sql(sql, allowed_tables=available, max_rows=MAX_ROWS, ordered=True)
        for t in sorted(q.tables):
            if t in self.registry.datasets:
                self.manifest(t)  # brings a hub or run-time source onto this computer on first use
        con = self.connect()
        cols, rows = run_guarded(con, q)
        ids = sorted(t for t in q.tables if t in self.manifests)
        notices = [n for i in ids if self.manifests[i].origin == "publisher" for n in self.manifests[i].notes]
        if len(ids) == 1:
            ds = self.registry.get(ids[0])
            out = envelope(
                ds, self.manifests[ids[0]], cols, rows, sql=q.sql, params=[], method="Read-only SQL."
            )
            return out
        caveats = list(notices)
        for i in ids:  # each source's review state and licence travel with a joined answer too
            ds = self.registry.get(i)
            if self.manifests[i].catalog_status != "reviewed":
                caveats.append(
                    f"{i}: this dataset's contract is a draft that no maintainer has reviewed yet."
                )
            caveats += [f"{i}: {c}" for c in ds.entry.caveats]
        return {
            "columns": cols,
            "rows": [list(r) for r in rows],
            "sql": q.sql,
            "sources": [_source(self.registry.get(i), self.manifests[i]) for i in ids],
            "method": "Read-only SQL across datasets.",
            "caveats": caveats,
        }

    def _tables(self) -> dict[str, frozenset[str]]:
        self.connect()
        return {i: self.registry.get(i).contract.column_names for i in self.manifests}


def _source(ds: Dataset, m: Manifest) -> dict[str, Any]:
    return {
        "dataset": ds.id,
        "title": ds.entry.title,
        "publisher": ds.entry.publisher,
        "source_urls": m.source_urls[:3] + (["..."] if len(m.source_urls) > 3 else []),
        "landing_url": ds.entry.source.landing_url,
        "retrieved_at": m.retrieved_at,
        "raw_sha256": list(m.raw_sha256),
        "snapshot_id": m.snapshot_id,
        "contract_version": m.contract_version,
        "review_status": m.catalog_status,
        "partial_snapshot": m.partial,
        "licence": ds.entry.licence.id,
        "terms_url": ds.entry.licence.terms_url,
        "citation": ds.entry.citation,
        "data_from": m.origin,
        "release_id": m.release_id,
        "release_ref": m.release_ref,
        "release_contract_version": m.release_contract_version,
        "release_files": dict(m.release_files),
    }


def envelope(
    ds: Dataset,
    m: Manifest,
    columns: list[str],
    rows: Sequence[Sequence[Any]],
    *,
    sql: str,
    params: list[Any],
    method: str,
) -> dict[str, Any]:
    truncated = len(rows) > MAX_ROWS
    out = [list(r) for r in rows[:MAX_ROWS]]
    notes = list(ds.entry.caveats)
    if m.catalog_status != "reviewed":
        notes.insert(0, "This dataset's contract is a draft that no maintainer has reviewed yet.")
    if m.partial:
        notes.insert(0, "This snapshot is a partial sample, not the full dataset.")
    if truncated:
        notes.insert(0, f"Only the first {MAX_ROWS} rows are shown.")
    notes = list(m.notes) + notes  # e.g. a hub release built with another contract version comes first
    return {
        "columns": columns,
        "rows": out,
        "row_count": len(out),
        "truncated": truncated,
        "sql": sql,
        "params": params,
        "method": method,
        "source": _source(ds, m),
        "caveats": notes,
        "uncertainty": "Observed counts carry no uncertainty interval."
        if ds.entry.shape in ("surveillance_series", "frequency_table")
        else "Uncertainty bounds are shown only where the publisher gives them.",
    }
