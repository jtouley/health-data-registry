"""The read path: attach snapshots read-only, compile metrics to SQL, run guarded SQL, wrap every answer.

Every result carries the table, the SQL that produced it, the source and its version, and a one-line method,
so a clinician (or a grader) can check the number without trusting the model that relayed it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from health_data_registry.registry import Column, Dataset, Metric, Registry, label_key
from health_data_registry.sql_guard import GuardedQuery, guard_sql, harden, run_guarded
from health_data_registry.store import Manifest, latest_snapshot

MAX_ROWS = 5000
FilterValue = str | int | float | bool | Sequence[str | int | float | bool]


class QueryRefused(ValueError):
    """The question can't be answered this way; the message says why in plain words."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


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
            ds, metric, where, params, period_where, period_params, period_col, group_by
        )
    where = where + period_where
    params = params + period_params
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    if metric.aggregation == "sum":
        sel = [*select_dims, f"sum({m_col}) AS {_q(metric.name)}"]
        if flag:
            # The publisher's flags on the empty cells the sum had to leave out, so a gap is never silent.
            f_col = _q(flag.name)
            sel.append(
                f"string_agg(DISTINCT {f_col}, ', ' ORDER BY {f_col}) FILTER (WHERE {m_col} IS NULL) AS flag"
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
            + "."
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
        return f"SELECT {cols}, {aggs} FROM base GROUP BY ALL"  # noqa: S608 - quoted contract identifiers

    m_flag = ds.contract.flag_column(m_col)
    gap = (
        f"'; later {unit}s have no number' || CASE WHEN c.last_flag <> '' THEN ' (flag at {unit} ' || c.last_at"
        f" || ': ' || {_flag_text(m_flag, 'c.last_flag')} || ')' ELSE '' END"
    )
    status = (
        f"CASE WHEN c.v IS NULL THEN 'no number published through {unit} ' || c.last_at"
        f" || CASE WHEN c.last_flag <> '' THEN ' (flag: ' || {_flag_text(m_flag, 'c.last_flag')} || ')' ELSE '' END"
        f" WHEN c.v_at < c.last_at THEN 'incomplete: running total last published at {unit} ' || c.v_at"
        f" || ' of ' || c.last_at || {gap} || ', so this is not the {span} total'"
        f" WHEN {{ended}} THEN 'as published at {span} end (through {unit} ' || c.v_at || ')'"
        f" ELSE 'provisional: running total so far (through {unit} ' || c.v_at || ')' END"
    )
    flag_out = "CASE WHEN c.v_at IS DISTINCT FROM c.last_at THEN nullif(c.last_flag, '') END"
    ctes = f"WITH base AS (SELECT * FROM {_q(ds.id)}{where_sql}), cur AS ({running(m_col, t)})"  # noqa: S608 - quoted contract identifiers
    sel = [*(f"c.{_q(g)}" for g in group_by), f"c.v AS {_q(metric.name)}"]
    columns = [*group_by, metric.name, "period_status", "flag"]
    join = ""
    if metric.restated_by:
        r_col = sem.measure(metric.restated_by.measure).column
        r_flag = ds.contract.flag_column(r_col)
        ctes += f", nxt AS ({running(r_col, f'{t} - 1')})"
        join = " LEFT JOIN nxt n USING (" + ", ".join(_q(g) for g in [*others, tname]) + ")"
        status = status.replace("{ended}", "n.last_at IS NOT NULL")
        r_status = (
            f"CASE WHEN n.last_at IS NULL THEN 'no next-{span} table yet'"
            f" WHEN n.v IS NULL THEN 'next {span}''s table has no number for this {span}'"
            f" || CASE WHEN n.last_flag <> '' THEN ' (flag: ' || {_flag_text(r_flag, 'n.last_flag')} || ')' ELSE '' END"
            f" ELSE 'as published in next {span}''s table (through {unit} ' || n.v_at || ')'"
            f" || CASE WHEN n.v_at < c.last_at THEN '; covers {unit}s 1-' || n.v_at || ' of ' || c.last_at"
            f" || ' only, not the whole {span}' ELSE '' END END"
        )
        sel += [
            f"{status} AS period_status",
            f"{flag_out} AS flag",
            "n.v AS restated",
            f"{r_status} AS restated_status",
        ]
        columns += ["restated", "restated_status"]
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
            f"({sem.measure(metric.restated_by.measure).name}) is shown beside it as 'restated', with the {unit} "
            "it reaches. The two are never swapped or combined, because the publisher doesn't say they count the "
            "same thing"
            if metric.restated_by
            else ""
        )
        + ("; publisher total rows excluded" if metric.exclude_total_rows and ds.contract.total_rows else "")
        + ". Weekly counts are never added up, because late reports land in the running total, not the week."
    )
    return CompiledMetric(sql=sql, params=[*params, *period_params], columns=columns, method=method)


@dataclass
class Engine:
    """One DuckDB connection with every available snapshot attached read-only, then locked down."""

    registry: Registry
    lake: Path
    manifests: dict[str, Manifest] = field(default_factory=dict)
    _con: duckdb.DuckDBPyConnection | None = None

    def connect(self) -> duckdb.DuckDBPyConnection:
        if self._con is not None:
            return self._con
        con = duckdb.connect(":memory:")
        for ds in self.registry:
            found = latest_snapshot(self.lake, ds.id)
            if found is None:
                continue
            path, manifest = found
            alias = f"snap_{ds.id}"
            db = str(path / "data.duckdb").replace("'", "''")
            con.execute(f"ATTACH '{db}' AS {_q(alias)} (READ_ONLY)")
            con.execute(f"CREATE VIEW {_q(ds.id)} AS SELECT * FROM {_q(alias)}.{_q(ds.id)}")  # noqa: S608 - quoted contract identifiers, bound values
            self.manifests[ds.id] = manifest
        harden(con)
        self._con = con
        return con

    def manifest(self, dataset_id: str) -> Manifest:
        self.connect()
        m = self.manifests.get(dataset_id)
        if m is None:
            raise QueryRefused(
                "no_snapshot",
                f"The data for {dataset_id} hasn't been downloaded on this computer yet, so there are no numbers "
                "to show. The one-time 'load the data' step in the setup guide (docs/setup.md) fixes this.",
            )
        return m

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
        return envelope(ds, manifest, cm.columns, rows, sql=cm.sql, params=cm.params, method=cm.method)

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

    def query_sql(self, sql: str) -> dict[str, Any]:
        con = self.connect()
        available = {k: v for k, v in self._tables().items()}
        q: GuardedQuery = guard_sql(sql, allowed_tables=available, max_rows=MAX_ROWS)
        cols, rows = run_guarded(con, q)
        ids = sorted(t for t in q.tables if t in self.manifests)
        if len(ids) == 1:
            ds = self.registry.get(ids[0])
            return envelope(
                ds, self.manifests[ids[0]], cols, rows, sql=q.sql, params=[], method="Read-only SQL."
            )
        return {
            "columns": cols,
            "rows": [list(r) for r in rows],
            "sql": q.sql,
            "sources": [_source(self.registry.get(i), self.manifests[i]) for i in ids],
            "method": "Read-only SQL across datasets.",
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
        "snapshot_id": m.snapshot_id,
        "contract_version": m.contract_version,
        "review_status": m.catalog_status,
        "partial_snapshot": m.partial,
        "licence": ds.entry.licence.id,
        "terms_url": ds.entry.licence.terms_url,
        "citation": ds.entry.citation,
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
