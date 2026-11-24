"""Analysis tools: rates, comparisons, trends, age adjustment and signals, computed from the loaded tables.

Each result gives the number, its 95% uncertainty range, the method in one plain sentence, what the data can't
say, and the source's own flags carried through: a suppressed or missing count is never treated as zero, and
an unreliable-rate rule is applied only when the source's contract records one (never a hard-coded threshold).
The statistics themselves live in stats.py, with their published sources.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from health_data_registry import geo, stats
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Dataset, Registry, Series
from health_data_registry.tools import (
    Fmt,
    Out,
    Result,
    _match_places,
    message,
    refusal,
    series_for,
)

PER = 100_000.0
NATION = "United States"
GBD_WARNING = (
    "Never compare this with GBD's age-standardised rates: GBD standardises to its own world population, not the "
    "2000 US standard, and models its numbers, so the two are not on the same scale."
)
_CANT_SAY_SURVEILLANCE = (
    "Case definitions can differ or change between places and years; ascertainment (who gets tested, how "
    "thoroughly cases are found and reported) differs by place and over time; and coverage can differ (which "
    "jurisdictions, facilities or residents a count includes, and whether the population matches it)."
)
CRUDE_WARNING = (
    "This is a crude rate, not age-standardised, so don't compare it with age-standardised GBD or CDC rates."
)


# ---------------------------------------------------------------------------------- reading the tables


@dataclass(frozen=True)
class Obs:
    place: str  # the place name (GBD location name where geo.py knows it)
    raw_place: str  # the publisher's own label
    year: int
    value: float | None  # None: suppressed or missing in the source, never zero
    status: str  # the source's period status ('' when it has none)
    extra: tuple[Any, ...] = ()  # the week or age group, for weekly and age-specific series
    flag: Any = None  # the publisher's flag carried through (a suppression or missing-data code)
    # For a running total: why this figure is not the period's own total ('' when it is), e.g. later weeks
    # have no number. Such a figure is never used as a year's count.
    not_total: str = ""
    suppressed: int = 0  # how many source values in this group were empty (suppressed or missing)


def _place_name(text: str) -> str:
    p = geo.place(text)
    return p.name if p else str(text)


def _key(text: str) -> str:
    return re.sub(r"[.]", "", " ".join(str(text).upper().split()))


def _unlocated_place(ds: Dataset, s: Series) -> str:
    """A series with no location dimension: the nation for a US population source, else 'as published'."""
    if s.measure == "population" and ds.entry.country == "US" and "nation" in ds.entry.geography:
        return NATION
    return "as published"


def read_series(
    engine: Engine,
    ds: Dataset,
    s: Series,
    locations: Sequence[str],
    year_start: int | None,
    year_end: int | None,
    extra: Sequence[str] = (),
) -> tuple[list[Obs], list[str]]:
    """One series' rows for the places and years asked, grouped also by `extra` dimensions (week, age group).

    For a running total (NNDSS) only the year's own figure is read (observation_type year_end_ytd): next year's
    table's figure for the same year is an unverified restatement and is never used as a count.
    """
    places: dict[Any, str] = {}
    unmatched: list[str] = []
    filters: dict[str, Any] = dict(s.filters)
    group_by: list[str] = [s.time_dimension]
    if s.location_dimension:
        places, unmatched = _match_places(engine, ds, s, locations)
        if not places:
            return [], unmatched
        filters[s.location_dimension] = list(places)
        group_by = [s.location_dimension, s.time_dimension]
    elif locations:
        # One figure for the publisher's whole area: it is not the figure for any place asked about.
        return [], list(locations)
    group_by += list(extra)
    out = engine.query_metric(
        ds.id,
        s.metric,
        filters=filters,
        group_by=group_by,
        period={"dimension": s.time_dimension, "start": year_start, "end": year_end},
    )
    if out.get("truncated"):
        raise QueryRefused(
            "too_many_rows",
            f"{s.label} has more than {len(out['rows'])} rows for this question, so nothing is worked out rather "
            "than from a table cut off part way. Ask again for fewer places or a shorter span of years.",
        )
    at = {c: i for i, c in enumerate(out["columns"])}
    n = len(group_by)
    loc_col = (
        ds.contract.column(ds.semantic.dimension(s.location_dimension).column)
        if s.location_dimension
        else None
    )
    by_key = {loc_col.canonical_label(str(k)): v for k, v in places.items()} if loc_col else {}
    rows: list[Obs] = []
    for r in out["rows"]:
        status = str(r[at["period_status"]] or "") if "period_status" in at else ""
        flag = r[at["flag"]] if "flag" in at else None
        if loc_col is not None:
            raw = str(r[0])
            p = geo.place(raw)
            place = places.get(r[0]) or by_key.get(loc_col.canonical_label(raw)) or (p.name if p else raw)
            year, rest = r[1], tuple(r[2:n])
        else:
            raw = place = _unlocated_place(ds, s)
            year, rest = r[0], tuple(r[1:n])
        value = r[n]
        not_total = status if status.startswith(("incomplete", "no number")) else ""
        suppressed = int(r[at["suppressed_rows"]] or 0) if "suppressed_rows" in at else 0
        rows.append(
            Obs(
                place,
                raw,
                int(year),
                None if value is None else float(value),
                status,
                rest,
                flag,
                not_total,
                suppressed,
            )
        )
    return rows, unmatched


def _custom(items: Any) -> dict[str, Any]:
    return {
        str(i["property"]): i.get("value") for i in items or [] if isinstance(i, Mapping) and "property" in i
    }


def contract_property(ds: Dataset, name: str) -> Any:
    """A top-level customProperty of the source's contract (unreliableRateRule, caseDefinitions)."""
    return _custom(ds.contract.raw.get("customProperties")).get(name)


def schema_property(ds: Dataset, name: str) -> Any:
    schema = ds.contract.raw.get("schema") or [{}]
    return _custom(schema[0].get("customProperties")).get(name)


@dataclass(frozen=True)
class UnreliableRule:
    """When a source calls its own rates unreliable, read from its contract (property unreliableRateRule).

    kind min_count: unreliable when the count is below `threshold`.
    kind max_relative_ci_width: unreliable when the 95% interval is wider than `threshold` times the rate.
    """

    kind: str
    threshold: float
    text: str

    def applies(self, count: float, est: stats.Estimate) -> bool:
        if self.kind == "min_count":
            return count < self.threshold
        if est.value <= 0:
            return True
        return (est.upper - est.lower) / est.value > self.threshold


def unreliable_rule(ds: Dataset) -> UnreliableRule | None:
    raw = contract_property(ds, "unreliableRateRule")
    if raw is None:
        return None
    try:
        kind = str(raw["kind"])
        threshold = float(raw["threshold"])
        text = str(raw["description"])
    except (KeyError, TypeError, ValueError):
        kind = ""
    if kind not in ("min_count", "max_relative_ci_width") or threshold <= 0:
        raise QueryRefused(
            "bad_contract_rule",
            f"The contract for {ds.id} records an unreliable-rate rule this server can't read, so no rate is shown "
            "rather than one that might be wrongly flagged. Use the help_me_fix_it helper to report it.",
        )
    return UnreliableRule(kind, threshold, text)


def case_definition_years(ds: Dataset, condition: str) -> list[tuple[int, str]]:
    """(effective year, definition) for a condition, from the contract's caseDefinitions, oldest first."""
    out: list[tuple[int, str]] = []
    for item in contract_property(ds, "caseDefinitions") or []:
        if isinstance(item, Mapping) and str(item.get("condition")) == condition:
            out.append((int(item["effectiveYear"]), str(item.get("definition") or "")))
    return sorted(out)


def partial_area(ds: Dataset, raw_place: str) -> str | None:
    """Why a source's count for this area covers only part of the place (e.g. New York State without NYC)."""
    areas = schema_property(ds, "partialAreas") or {}
    for label, reason in dict(areas).items():
        if _key(label) == _key(raw_place):
            return str(reason)
    return None


def _provisional(status: str) -> bool:
    return status.lower().startswith("provisional")


def _in_progress(status: str) -> bool:
    """A running total for a period that hasn't ended: the total so far, not the period's count."""
    return status.lower().startswith("provisional: running total so far")


def _own_count(datasets: Sequence[Dataset]) -> str:
    """The method line's words on which figure is the year's count, for sources that publish running totals."""
    if not any(m.aggregation == "cumulative_latest" for ds in datasets for m in ds.semantic.metrics):
        return ""
    return (
        " Each year's count is that year's own running total at its last week with a number (observation_type "
        "year_end_ytd); next year's table's figure for it (subsequent_year_restatement) is unverified and is never "
        "used as a count, and a running total that stops before the year's last week is not used at all."
    )


def _sig(x: float | None, digits: int = 4) -> float | None:
    """Round to `digits` significant figures, so a small rate never rounds to zero."""
    if x is None:
        return None
    if x == 0:
        return 0.0
    from math import floor, log10

    return round(x, digits - 1 - floor(log10(abs(x))))


def _unique(datasets: Sequence[Dataset]) -> list[Dataset]:
    """Each dataset once, in first-seen order (datasets aren't hashable)."""
    seen: dict[str, Dataset] = {}
    for ds in datasets:
        seen.setdefault(ds.id, ds)
    return list(seen.values())


def _source_notes(engine: Engine, datasets: Sequence[Dataset]) -> list[str]:
    notes: list[str] = []
    for ds in _unique(datasets):
        m = engine.manifest(ds.id)
        notes.append(f"Source: {ds.id}, {ds.entry.source.landing_url}, fetched {m.retrieved_at[:19]}.")
        if ds.entry.status != "reviewed":
            notes.append(
                f"{ds.id}: this source's contract is still a draft; no maintainer has reviewed it yet."
            )
        if m.partial:
            notes.append(f"{ds.id}: the local copy is a partial sample, not the full dataset.")
    return notes


def _population_series(registry: Registry) -> list[tuple[Dataset, Series]]:
    return [
        (d, s)
        for d in registry
        for s in d.semantic.series
        if s.measure == "population" and not s.age_dimension
    ]


def populations(
    engine: Engine,
    pops: Sequence[tuple[Dataset, Series]],
    places: Sequence[str],
    year_start: int | None,
    year_end: int | None,
) -> tuple[dict[tuple[str, int], tuple[float | None, Dataset]], set[int]]:
    """Population by (place, year) from every population series, and the years any source covers."""
    want_nation = not places or any(_place_name(p) == NATION for p in places)
    found: dict[tuple[str, int], tuple[float | None, Dataset]] = {}
    years: set[int] = set()
    for ds, s in pops:
        if s.location_dimension is None and not want_nation:
            continue
        locs = [p for p in places if _place_name(p) != NATION]
        if s.location_dimension is not None and places and not locs:
            continue
        rows, _ = read_series(engine, ds, s, locs if s.location_dimension else [], year_start, year_end)
        for o in rows:
            years.add(o.year)
            found.setdefault((o.place, o.year), (o.value, ds))
    return found, years


def _no_population(fmt: Fmt) -> Out:
    return message(
        "Population counts (Census) aren't loaded yet, so a rate can't be worked out here. Use get_series for the "
        "counts, or a published rate if the source has one (describe_dataset says).",
        fmt,
        refused="no_denominator",
    )


def _unknown(condition: str, what: str, fmt: Fmt) -> Out:
    return message(
        f"No loaded source reports '{condition}' as {what}. Use list_sources or suggest_datasets to see what is "
        "available.",
        fmt,
        refused="unknown_condition",
    )


# ---------------------------------------------------------------------------------- get_rate


@dataclass(frozen=True)
class RateRow:
    place: str
    year: int
    label: str
    cases: float | None
    population: float | None
    est: stats.Estimate | None
    status: str
    flags: list[str]
    case_ds: Dataset
    pop_ds: Dataset | None


def _rate_rows(
    registry: Registry,
    engine: Engine,
    condition: str,
    locations: Sequence[str],
    year_start: int,
    year_end: int,
) -> tuple[list[RateRow], list[str], list[Dataset]]:
    cases = series_for(registry, condition, "cases")
    pop, pop_years = populations(engine, _population_series(registry), locations, year_start, year_end)
    rows: list[RateRow] = []
    unmatched: list[str] = []
    used: list[Dataset] = []
    for ds, s in cases:
        got, miss = read_series(engine, ds, s, locations, year_start, year_end)
        unmatched += miss
        rule = unreliable_rule(ds)
        if got:
            used.append(ds)
        for o in got:
            flags: list[str] = []
            denom, pds = pop.get((o.place, o.year), (None, None))
            est = None
            part = partial_area(ds, o.raw_place)
            if o.not_total:
                flags.append(f"{o.not_total}; no rate")
            elif o.value is None and o.suppressed:
                flags.append(
                    f"{o.suppressed} value{'s' if o.suppressed != 1 else ''} in this count suppressed or missing "
                    "in the source, so no total and no rate (it is not zero)"
                )
            elif o.value is None:
                flags.append("count suppressed or missing in the source, so no rate (it is not zero)")
            elif part:
                flags.append(
                    f"the count covers only part of {o.place} ({part}), so no rate against its population"
                )
            elif o.year not in pop_years:
                flags.append(f"no Census population estimate for {o.year}, so no rate")
            elif denom is None:
                flags.append(f"no Census population for {o.place}, so no rate")
            elif denom <= 0:
                flags.append(f"the Census population for {o.place} in {o.year} is {denom:g}, so no rate")
            else:
                est = stats.crude_rate(int(o.value), denom, PER)
                if rule and rule.applies(o.value, est):
                    flags.append(f"unreliable by the source's own rule: {rule.text}")
            if o.flag:
                flags.append(f"source flag: {o.flag}")
            if _provisional(o.status):
                flags.append("provisional: may change")
            rows.append(RateRow(o.place, o.year, s.label, o.value, denom, est, o.status, flags, ds, pds))
            if pds is not None:
                used.append(pds)
    return rows, unmatched, used


def _rule_notes(datasets: Sequence[Dataset]) -> list[str]:
    notes = []
    for ds in _unique(datasets):
        if not any(s.measure == "cases" for s in ds.semantic.series):
            continue
        rule = unreliable_rule(ds)
        notes.append(
            f"{ds.id}: unreliable rates are flagged by the source's own rule: {rule.text}"
            if rule
            else f"{ds.id}: the source's contract records no unreliable-rate rule, so none is flagged; with small "
            "counts read the interval, which is wide."
        )
    return notes


def get_rate(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    year: int,
    locations: Sequence[str] = (),
    fmt: Fmt = "csv",
) -> Out:
    if not _population_series(registry):
        return _no_population(fmt)
    if not series_for(registry, condition, "cases"):
        return _unknown(condition, "case counts", fmt)
    try:
        rows, unmatched, used = _rate_rows(registry, engine, condition, locations, year, year)
        notes = _source_notes(engine, used)
    except QueryRefused as e:
        return refusal(e, fmt)
    cols = ["location", "year", "condition", "cases", "population", "rate_per_100k", "lower_95", "upper_95",
            "period_status", "flags", "case_source", "population_source"]  # fmt: skip
    table = [
        [r.place, r.year, r.label, r.cases, r.population, _sig(r.est.value) if r.est else None,
         _sig(r.est.lower) if r.est else None, _sig(r.est.upper) if r.est else None, r.status,
         "; ".join(r.flags), r.case_ds.id, r.pop_ds.id if r.pop_ds else ""]
        for r in rows
    ]  # fmt: skip
    out_notes = [
        "Method: crude rate = cases / Census July 1 population x 100,000, with the exact Poisson (Garwood) 95% "
        "interval for the count divided by the population." + _own_count([r.case_ds for r in rows]),
        "What this can't say: the interval covers chance variation in the count only, not error in the population "
        "estimate. " + _CANT_SAY_SURVEILLANCE + " " + CRUDE_WARNING,
        *_rule_notes([r.case_ds for r in rows]),
    ]
    if unmatched:
        out_notes.append(f"No rows for: {', '.join(dict.fromkeys(unmatched))}.")
    if not rows:
        out_notes.append(f"No case counts for '{condition}' in {year} for the places asked.")
    return Result(cols, table, out_notes + notes).render(fmt)


# ---------------------------------------------------------------------------------- compare_rates


def compare_rates(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    location: str,
    year: int,
    other_location: str | None = None,
    other_year: int | None = None,
    fmt: Fmt = "csv",
) -> Out:
    loc_b = other_location or location
    year_b = other_year if other_year is not None else year
    if _place_name(loc_b) == _place_name(location) and year_b == year:
        return message(
            "Say what to compare with: another place (other_location) or another year (other_year).",
            fmt,
            refused="nothing_to_compare",
        )
    if not _population_series(registry):
        return _no_population(fmt)
    if not series_for(registry, condition, "cases"):
        return _unknown(condition, "case counts", fmt)
    try:
        a_rows, _, used_a = _rate_rows(registry, engine, condition, [location], year, year)
        b_rows, _, used_b = _rate_rows(registry, engine, condition, [loc_b], year_b, year_b)
        notes = _source_notes(engine, used_a + used_b)
    except QueryRefused as e:
        return refusal(e, fmt)
    a = next((r for r in a_rows if r.est), None)
    b = next((r for r in b_rows if r.est and (a is None or r.case_ds.id == a.case_ds.id)), None)
    if a is None or b is None:
        side, rows = ((location, year), a_rows) if a is None else ((loc_b, year_b), b_rows)
        why = "; ".join(rows[0].flags) if rows else "no case count was found"
        return message(
            f"A rate for {side[0]} in {side[1]} can't be worked out ({why}), so the comparison can't be made. "
            "Try get_rate to see which years and places have counts and populations.",
            fmt,
            refused="no_rate",
        )
    name_a, name_b = f"{a.place} {a.year}", f"{b.place} {b.year}"
    ca, cb, pa, pb = int(a.cases or 0), int(b.cases or 0), float(a.population or 0), float(b.population or 0)
    rd = stats.rate_difference(ca, pa, cb, pb, PER)
    cols = ["what", "estimate", "lower_95", "upper_95", "cases", "population", "period_status", "flags"]
    table: list[list[Any]] = []
    for r, est, nm in ((a, a.est, name_a), (b, b.est, name_b)):
        if est is None:  # pragma: no cover - a and b were chosen for having a rate
            continue
        table.append([f"rate per 100,000: {nm}", _sig(est.value), _sig(est.lower), _sig(est.upper),
                      r.cases, r.population, r.status, "; ".join(r.flags)])  # fmt: skip
    small = min(ca, cb) < stats.SMALL_COUNT
    rr_label = f"rate ratio ({name_a} / {name_b})"
    if cb == 0:
        table.append([rr_label, None, None, None, "", "", "",
                      f"not computed: {name_b} has no cases, so the ratio has no upper limit"])  # fmt: skip
    else:
        rr = stats.rate_ratio_exact(ca, pa, cb, pb) if small else stats.rate_ratio(ca, pa, cb, pb)
        how = "exact conditional interval (fewer than 5 cases in a group)" if small else ""
        table.append([rr_label, _sig(rr.value), _sig(rr.lower), _sig(rr.upper), "", "", "", how])  # fmt: skip
    rd_label = f"rate difference per 100,000 ({name_a} - {name_b})"
    if small:
        table.append([rd_label, _sig(rd.value), None, None, "", "", "",
                      "no interval: with fewer than 5 cases in a group the normal approximation is not "
                      "trustworthy; use the rate ratio's exact interval"])  # fmt: skip
    else:
        table.append([rd_label, _sig(rd.value), _sig(rd.lower), _sig(rd.upper), "", "", "", ""])  # fmt: skip
    out_notes = [
        "Method: the rate ratio's 95% interval uses the log method on the two Poisson case counts (standard error "
        "sqrt(1/a + 1/b)), or the exact conditional (Clopper-Pearson) interval when either group has fewer than 5 "
        "cases; the rate difference's uses a normal approximation to the Poisson variances and is not given "
        "below 5 cases in a group." + _own_count([a.case_ds]),
        "What this can't say: an interval that excludes 1 (ratio) or 0 (difference) means chance alone is an "
        "unlikely explanation, not that risk differs. " + _CANT_SAY_SURVEILLANCE + " " + CRUDE_WARNING,
        *_rule_notes([a.case_ds]),
    ]
    if a.year != b.year:
        changes = [y for y, _ in case_definition_years(a.case_ds, _series_condition(a.case_ds, a.label))
                   if min(a.year, b.year) < y <= max(a.year, b.year)]  # fmt: skip
        if changes:
            out_notes.insert(0, f"The case definition changed in {', '.join(map(str, changes))}, between the two "
                             "years compared: part of any difference may be the definition, not the disease.")  # fmt: skip
    if _provisional(a.status) or _provisional(b.status):
        out_notes.append("At least one count is provisional and may change.")
    return Result(cols, table, out_notes + notes).render(fmt)


def _series_condition(ds: Dataset, label: str) -> str:
    return next((s.condition for s in ds.semantic.series if s.label == label), label)


# ---------------------------------------------------------------------------------- get_trend


def _span(years: set[int]) -> str:
    return f"{min(years)}-{max(years)}" if years else "no years (no population source is loaded)"


def _segments(years: Sequence[int], changes: Sequence[tuple[int, str]]) -> list[tuple[int | None, list[int]]]:
    """Split years where the case definition changed: [(effective year or None, years in that segment)]."""
    starts = [y for y, _ in changes]
    out: dict[int | None, list[int]] = {}
    for y in sorted(years):
        eff = max((s for s in starts if s <= y), default=None)
        out.setdefault(eff, []).append(y)
    return list(out.items())


def get_trend(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    location: str,
    year_start: int | None = None,
    year_end: int | None = None,
    include_provisional: bool = False,
    fit_on: Literal["rates", "counts"] = "rates",
    fmt: Fmt = "csv",
) -> Out:
    """Annual percent change. fit_on="rates" (the default) needs a population for every year fitted and refuses
    otherwise; fit_on="counts" fits case counts and ignores population change. Never one in place of the other."""
    if fit_on not in ("rates", "counts"):
        return message('fit_on must be "rates" or "counts".', fmt, refused="bad_fit_on")
    matches = series_for(registry, condition, "cases")
    if not matches:
        return _unknown(condition, "case counts", fmt)
    cols = ["location", "condition", "years", "points_used", "annual_percent_change", "lower_95", "upper_95",
            "fit_on", "case_definition_from", "left_out", "dispersion", "source_id"]  # fmt: skip
    table: list[list[Any]] = []
    notes: list[str] = []
    used: list[Dataset] = []
    too_few: list[str] = []
    no_denominator: list[str] = []
    try:
        pop, pop_years = populations(engine, _population_series(registry), [location], year_start, year_end)
        for ds, s in matches:
            got, _ = read_series(engine, ds, s, [location], year_start, year_end)
            if not got:
                continue
            used.append(ds)
            changes = case_definition_years(ds, s.condition)
            for eff, years in _segments([o.year for o in got], changes):
                seg = [o for o in got if o.year in years]
                left_out = [f"{o.year} not the year's total ({o.not_total})" for o in seg if o.not_total]
                left_out += [
                    f"{o.year} suppressed or missing" for o in seg if o.value is None and not o.not_total
                ]
                if not include_provisional:
                    left_out += [
                        f"{o.year} provisional"
                        for o in seg
                        if o.value is not None and not o.not_total and _in_progress(o.status)
                    ]
                pts = [
                    o
                    for o in seg
                    if o.value is not None
                    and not o.not_total
                    and (include_provisional or not _in_progress(o.status))
                ]
                part = partial_area(ds, seg[0].raw_place)
                if part and fit_on == "rates":
                    no_denominator.append(
                        f"{seg[0].place}: {ds.id} counts cover only part of the place ({part})"
                    )
                    continue
                if part:
                    notes.append(f"{ds.id}: the counts for {seg[0].place} cover only part of it ({part}).")
                span = f"{min(years)}-{max(years)}"
                if len(pts) < 3:
                    too_few.append(f"{span} ({len(pts)} usable year{'s' if len(pts) != 1 else ''})")
                    continue
                on_rates = fit_on == "rates"
                denoms = [pop.get((o.place, o.year), (None, None))[0] for o in pts]
                missing = [str(o.year) for o, d in zip(pts, denoms, strict=True) if d is None]
                if on_rates and missing:
                    no_denominator.append(f"{pts[0].place} in {', '.join(missing)}")
                    continue
                fit = stats.poisson_trend(
                    [o.year for o in pts],
                    [o.value or 0.0 for o in pts],
                    [float(d or 0) for d in denoms] if on_rates else None,
                )
                if on_rates:
                    used += [pop[(o.place, o.year)][1] for o in pts[:1]]
                table.append([
                    pts[0].place, s.label, f"{min(o.year for o in pts)}-{max(o.year for o in pts)}", len(pts),
                    _sig(fit.apc), _sig(fit.lower), _sig(fit.upper),
                    "rates (cases per Census population)" if on_rates else "case counts (population change ignored)",
                    eff if eff is not None else "", "; ".join(left_out), _sig(fit.dispersion, 3), ds.id,
                ])  # fmt: skip
            notes.append(
                f"{ds.id}: the case definition changed in {', '.join(str(y) for y, _ in changes)}; the trend is split "
                "there and the parts are never joined."
                if changes
                else f"{ds.id}: the contract records no change in case definition, so this trend assumes the "
                "definition stayed the same over these years."
            )
        notes += _source_notes(engine, used)
    except (QueryRefused, stats.StatsError) as e:
        if isinstance(e, stats.StatsError):
            return message(
                f"The trend can't be estimated from these counts: {e}. Use get_series to see the yearly values.",
                fmt,
                refused="no_trend",
            )
        return refusal(e, fmt)
    if no_denominator and not table:
        return message(
            "A trend in rates needs a Census population for every year fitted (and counts that cover the whole "
            f"place), and it is missing for: {'; '.join(no_denominator)}. Census estimates here cover "
            f'{_span(pop_years)}. Ask for years inside that range, or ask again with fit_on="counts" for a trend '
            "in case counts that ignores population change.",
            fmt,
            refused="no_denominator",
        )
    if not table:
        detail = "; ".join(too_few) or f"no yearly counts for {location}"
        return message(
            f"A trend needs at least 3 usable years of counts, and there are not enough here ({detail}). "
            + ("Provisional years were left out; ask again with include_provisional=true to include them, or "
               if not include_provisional else "")
            + "use get_series to see the yearly values.",
            fmt,
            refused="too_few_points",
        )  # fmt: skip
    out_notes = [
        "Method: annual percent change from a log-linear Poisson regression of yearly case counts on year (with "
        "the Census population as an offset for a trend in rates), its 95% interval widened when the counts "
        "vary more than chance allows (dispersion above 1)." + _own_count(used),
        "What this can't say: it assumes a steady percent change each year, it is fragile over few years, and it "
        "can't separate a real change from changes in testing, reporting or awareness. "
        + _CANT_SAY_SURVEILLANCE
        + " "
        + CRUDE_WARNING,
    ]
    if not include_provisional:
        out_notes.append(
            "Years still in progress (running total so far) are left out of the fit by default "
            "(include_provisional=true adds them); year-end running totals are used and stay provisional."
        )
    if too_few:
        out_notes.append("Not fitted (fewer than 3 usable years): " + "; ".join(too_few) + ".")
    if no_denominator:
        out_notes.append("Not fitted (no population for every year): " + "; ".join(no_denominator) + ".")
    if fit_on == "counts":
        out_notes.append(
            "This is a trend in case counts: population growth or decline is not taken into account, so part of "
            "any change can be the population, not the disease."
        )
    return Result(cols, table, out_notes + notes).render(fmt)


# ---------------------------------------------------------------------------------- age_adjust

_AGE_NUM = re.compile(r"\d+")


def parse_age_group(label: str) -> tuple[int, int | None] | None:
    """(lowest, highest) completed years of age for labels like '<1', 'Under 1 year', '01-04 years', '85+'."""
    text = str(label).strip().lower().replace("–", "-")
    nums = [int(n) for n in _AGE_NUM.findall(text)]
    if text.startswith(("<", "under")) and nums == [1]:
        return (0, 0)
    if len(nums) == 1 and ("+" in text or "over" in text or "older" in text):
        return (nums[0], None)
    if len(nums) == 1 and re.fullmatch(r"\d+( years?)?", text):
        return (nums[0], nums[0])
    if len(nums) == 2 and "-" in text:
        return (nums[0], nums[1])
    return None


def _standard_for(
    groups: set[tuple[int, int | None]],
) -> tuple[str, dict[tuple[int, int | None], int]] | None:
    for name, table in (
        ("2000 US standard population, 11 age groups (NCHS Healthy People 2010 Statistical Notes no. 20, "
         "table 2, distribution #1)", stats.US2000_STD_11),
        ("2000 US standard population, 19 age groups (NCI SEER standard populations, Census P25-1130)",
         stats.US2000_STD_19),
    ):  # fmt: skip
        weights = {(lo, hi): w for lo, hi, w in table}
        if groups == set(weights):
            return name, weights
    return None


def age_adjust(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    location: str,
    year: int,
    fmt: Fmt = "csv",
) -> Out:
    case_series = series_for(registry, condition, "cases", kind="age")
    pop_series = [
        (d, s) for d in registry for s in d.semantic.series if s.measure == "population" and s.age_dimension
    ]
    if not case_series or not pop_series:
        missing = "case counts by age group" if not case_series else "population by age group"
        return message(
            f"An age-adjusted rate needs case counts and population for the same age groups, and no loaded source "
            f"has {missing} for '{condition}'. Use get_rate for the crude rate, and say in your write-up that it is "
            "not age-adjusted.",
            fmt,
            refused="no_age_groups",
        )
    (cds, cs), (pds, ps) = case_series[0], pop_series[0]
    try:
        c_rows, _ = read_series(engine, cds, cs, [location], year, year, [str(cs.age_dimension)])
        p_rows, _ = read_series(engine, pds, ps, [location], year, year, [str(ps.age_dimension)])
        notes = _source_notes(engine, [cds, pds])
    except QueryRefused as e:
        return refusal(e, fmt)
    counts: dict[tuple[int, int | None], float | None] = {}
    pops: dict[tuple[int, int | None], float | None] = {}
    for rows, into in ((c_rows, counts), (p_rows, pops)):
        for o in rows:
            g = parse_age_group(str(o.extra[0]))
            if g is None or g in into:
                return message(
                    f"The age group '{o.extra[0]}' can't be matched to the standard population's groups, so no "
                    "age-adjusted rate is shown. Use get_rate for the crude rate.",
                    fmt,
                    refused="age_groups_mismatch",
                )
            into[g] = o.value
    if not counts:
        return message(
            f"No age-specific counts for '{condition}' in {location} in {year}.", fmt, refused="no_rows"
        )
    std = _standard_for(set(counts))
    if std is None or set(pops) != set(counts):
        return message(
            "The case counts and the population aren't split into the same age groups as the 2000 US standard "
            "population (11 or 19 groups), so an age-adjusted rate can't be computed. Use get_rate for the crude "
            "rate.",
            fmt,
            refused="age_groups_mismatch",
        )
    name, weights = std
    groups = sorted(weights, key=lambda g: g[0])
    if any(counts[g] is None for g in groups) or any(not pops[g] for g in groups):
        return message(
            "At least one age group's count is suppressed or missing (or has no population), and treating it as "
            "zero would understate the rate, so no age-adjusted rate is shown. Use get_rate for the crude rate.",
            fmt,
            refused="suppressed_age_group",
        )
    d = [int(counts[g] or 0) for g in groups]
    n = [float(pops[g] or 0) for g in groups]
    est = stats.direct_standardize(d, n, [weights[g] for g in groups], PER)
    crude = stats.crude_rate(sum(d), sum(n), PER)
    cols = ["location", "year", "condition", "age_adjusted_rate_per_100k", "lower_95", "upper_95",
            "crude_rate_per_100k", "cases", "population", "standard", "case_source", "population_source"]  # fmt: skip
    place = c_rows[0].place
    table = [[place, year, cs.label, _sig(est.value), _sig(est.lower), _sig(est.upper), _sig(crude.value), sum(d),
              sum(n), name, cds.id, pds.id]]  # fmt: skip
    out_notes = [
        "Method: direct standardisation to the 2000 US standard population, with the gamma (Fay-Feuer) 95% "
        "interval that NCHS uses.",
        "What this can't say: an age-adjusted rate is a summary for comparing places or years with different age "
        "structures, not the rate anyone actually experienced; it does not adjust for anything but age. "
        + _CANT_SAY_SURVEILLANCE
        + " "
        + GBD_WARNING,
    ]
    return Result(cols, table, out_notes + notes).render(fmt)


# ---------------------------------------------------------------------------------- check_signal

HISTORY_YEARS = 5
WINDOW = 4


def check_signal(
    registry: Registry,
    engine: Engine,
    *,
    condition: str,
    location: str,
    year: int,
    week: int,
    fmt: Fmt = "csv",
) -> Out:
    matches = series_for(registry, condition, "cases", kind="weekly")
    if not matches:
        return message(
            f"No loaded source has weekly counts of '{condition}', which this check needs. Use get_series for yearly "
            "counts.",
            fmt,
            refused="no_weekly_series",
        )
    if not 2 * WINDOW <= week <= 52 - WINDOW:
        return message(
            f"Week {week} is too close to the turn of the year: the comparison periods would cross into another "
            f"year, which this check doesn't handle yet. Ask for a week from {2 * WINDOW} to {52 - WINDOW}.",
            fmt,
            refused="week_out_of_range",
        )
    ds, s = matches[0]
    try:
        rows, _ = read_series(engine, ds, s, [location], year - HISTORY_YEARS, year, [str(s.week_dimension)])
        notes = _source_notes(engine, [ds])
    except QueryRefused as e:
        return refusal(e, fmt)
    weekly = {(o.year, int(o.extra[0])): o.value for o in rows}
    place = rows[0].place if rows else location

    def total(y: int, first: int) -> float | str:
        vals = [weekly.get((y, w), "absent") for w in range(first, first + WINDOW)]
        if "absent" in vals:
            return f"{y} weeks {first}-{first + WINDOW - 1} are not in the data"
        if any(v is None for v in vals):
            return f"{y} weeks {first}-{first + WINDOW - 1} include a suppressed or missing count"
        return float(sum(v for v in vals if isinstance(v, float)))

    current = total(year, week - WINDOW + 1)
    history = [
        total(year - k, first)
        for k in range(1, HISTORY_YEARS + 1)
        for first in (week - 2 * WINDOW + 1, week - WINDOW + 1, week + 1)
    ]
    problems = [t for t in [current, *history] if isinstance(t, str)]
    if problems:
        return message(
            f"CDC's historical-limits check needs complete weekly counts for this year and the past {HISTORY_YEARS} "
            f"years, and some are missing ({'; '.join(problems[:3])}"
            + ("; and more" if len(problems) > 3 else "")
            + "). A missing week can't be counted as zero, so no signal is shown. Use get_series for what is there.",
            fmt,
            refused="incomplete_history",
        )
    hist = [h for h in history if isinstance(h, float)]
    try:
        sig = stats.historical_limits(float(current), hist)
    except stats.StatsError as e:
        return message(
            f"The check can't be made: {e}. Use get_series to see the counts.", fmt, refused="no_signal"
        )
    words = {
        "above": "above historical limits",
        "below": "below historical limits",
        "within": "within limits",
    }
    cols = ["location", "condition", "year", "weeks", "current_4_week_total", "historical_mean", "historical_sd",
            "ratio", "lower_limit", "upper_limit", "signal", "source_id"]  # fmt: skip
    table = [[place, s.label, year, f"{week - WINDOW + 1}-{week}", sig.current, _sig(sig.mean), _sig(sig.sd),
              _sig(sig.ratio), _sig(sig.lower_limit), _sig(sig.upper_limit), words[sig.flag], ds.id]]  # fmt: skip
    out_notes = [
        "Method: CDC's historical limits for notifiable diseases (Stroup et al., Stat Med 1989; MMWR notifiable "
        "disease tables): the current 4-week total over the mean of 15 historical 4-week totals (the same, "
        "preceding and following 4 weeks in each of the past 5 years), flagged beyond 1 +/- 2 SD / mean.",
        "What this can't say: a flag means more (or fewer) reports than usual for these weeks, not an outbreak; "
        "reporting delays, changes in testing or reporting, and provisional recent weeks can all cause one. A "
        "case-definition change, a shift in ascertainment (more testing) or in coverage (a jurisdiction starting "
        "or stopping reporting) within the past 5 years makes the historical baseline a poor comparison. Check "
        "with the health department before acting on it.",
        "Weekly counts are provisional and can change as late reports arrive.",
    ]
    return Result(cols, table, out_notes + notes).render(fmt)
