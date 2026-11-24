"""The analysis tools end to end on a synthetic registry and lake (no public data).

Three toy sources: weekly notifiable-disease counts with a running year-to-date total (the NNDSS shape), a
Census-like population by state and year, and age-specific counts with age-specific populations. Expected
numbers are worked out here from the generated records, independently of the tools' SQL.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import analysis, stats, tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

YEARS = range(2015, 2025)  # 2024 is provisional: no 2025 table has restated it yet
WEEKS = range(1, 53)
AREAS = ["MICHIGAN", "OHIO", "NEW YORK", "WYOMING", "VERMONT", "MAINE", "US RESIDENTS"]
POP = {
    "Michigan": 10_000_000,
    "Ohio": 11_800_000,
    "New York": 19_500_000,
    "Wyoming": 580_000,
    "Vermont": 640_000,
    "Maine": 1_390_000,
}
SUPPRESSED = ("OHIO", 2022, 30)


def catalog(ds: str, *, geography: str = "[nation, state]") -> str:
    return f"""\
id: {ds}
title: Toy {ds}
agency: CDC
publisher: Toy publisher
level: federal
country: US
research_areas: [infectious_disease]
years: {{start: 2015, end: 2024}}
geography: {geography}
shape: surveillance_series
grain: toy rows
refresh: weekly
licence: {{id: public-domain-us-federal, redistribution: true, terms_url: "https://www.usa.gov/government-works"}}
citation: "Toy {ds}."
source:
  portal: socrata
  host: data.cdc.gov
  dataset: toy0-{ds[:4]}
  landing_url: https://data.cdc.gov/d/{ds}
  api_url: https://data.cdc.gov/resource/{ds}.json
codebook_url: https://data.cdc.gov/d/{ds}
caveats: [Toy caveat.]
contract: contracts/{ds}.odcs.yaml
semantic_model: semantic/{ds}.yaml
status: draft
"""


def prop(name: str, typ: str, role: str, desc: str = "toy column", required: bool = False) -> str:
    return (
        f"      - {{name: {name}, physicalType: {typ}, logicalType: x, required: {str(required).lower()}, "
        f"description: {desc}, customProperties: [{{property: role, value: {role}}}]}}\n"
    )


CASES_RULES = """\
customProperties:
  - property: unreliableRateRule
    value: {kind: min_count, threshold: 20, description: "rates from fewer than 20 cases are unreliable (toy rule)"}
  - property: caseDefinitions
    value:
      - {condition: legionellosis, effectiveYear: 2020, definition: "toy 2020 definition"}
"""

CASES_CONTRACT = (
    """\
apiVersion: v3.2.0
kind: DataContract
id: toy_weekly
version: 0.1.0
status: draft
RULES_HERE
schema:
  - name: toy_weekly
    customProperties:
      - {property: totalRows, value: "upper(area) = 'US RESIDENTS'"}
    properties:
      - {name: area, physicalType: VARCHAR, logicalType: x, required: true, description: toy column,
         customProperties: [{property: role, value: dimension},
                            {property: partialPlaces, value: {"NEW YORK": "excludes New York City"}}]}
"""
    + prop("year", "INTEGER", "dimension", required=True)
    + prop("week", "INTEGER", "dimension", required=True)
    + prop("wk", "BIGINT", "measure")
    + prop("ytd", "BIGINT", "measure")
    + prop("prior_ytd", "BIGINT", "measure")
)

CASES_SEMANTIC = """\
dataset: toy_weekly
version: 0.1.0
dimensions:
  - {name: state, column: area, type: categorical}
  - {name: year, column: year, type: time}
  - {name: week, column: week, type: time}
measures:
  - {name: wk, column: wk, kind: count}
  - {name: ytd, column: ytd, kind: count}
  - {name: prior_ytd, column: prior_ytd, kind: count}
metrics:
  - {name: annual, description: Annual cases, measure: ytd, aggregation: cumulative_latest, order_by: week,
     restated_by: {measure: prior_ytd, resolution_policy: require_domain_review}}
  - {name: us_annual, description: US annual, measure: ytd, aggregation: cumulative_latest, order_by: week,
     restated_by: {measure: prior_ytd, resolution_policy: require_domain_review}, exclude_total_rows: false,
     filters: ["upper(area) = 'US RESIDENTS'"]}
  - {name: weekly, description: Weekly cases, measure: wk, aggregation: sum}
series:
  - {condition: legionellosis, label: Legionellosis, metric: annual, measure: cases, location_dimension: state,
     time_dimension: year}
  - {condition: legionellosis, label: Legionellosis, metric: us_annual, measure: cases, location_dimension: state,
     time_dimension: year}
  - {condition: legionellosis, label: Legionellosis, metric: weekly, measure: cases, location_dimension: state,
     time_dimension: year, week_dimension: week}
"""

POP_CONTRACT = (
    """\
apiVersion: v3.2.0
kind: DataContract
id: toy_pop
version: 0.1.0
status: draft
schema:
  - name: toy_pop
    customProperties: [{property: totalRows, value: none}]
    properties:
"""
    + prop("state", "VARCHAR", "dimension", required=True)
    + prop("year", "INTEGER", "dimension", required=True)
    + prop("population", "BIGINT", "measure", required=True)
)

POP_SEMANTIC = """\
dataset: toy_pop
version: 0.1.0
dimensions:
  - {name: state, column: state, type: categorical}
  - {name: year, column: year, type: time}
measures:
  - {name: population, column: population, kind: count}
metrics:
  - {name: pop, description: Population, measure: population, aggregation: sum}
series:
  - {condition: population, label: Population, metric: pop, measure: population, location_dimension: state,
     time_dimension: year}
  - {condition: population, label: Population, metric: pop, measure: population, time_dimension: year}
"""

AGE_CONTRACT = (
    """\
apiVersion: v3.2.0
kind: DataContract
id: toy_age
version: 0.1.0
status: draft
schema:
  - name: toy_age
    customProperties: [{property: totalRows, value: none}]
    properties:
"""
    + prop("state", "VARCHAR", "dimension", required=True)
    + prop("year", "INTEGER", "dimension", required=True)
    + prop("age_group", "VARCHAR", "dimension", required=True)
    + prop("cases", "BIGINT", "measure")
    + prop("population", "BIGINT", "measure")
)

AGE_SEMANTIC = """\
dataset: toy_age
version: 0.1.0
dimensions:
  - {name: state, column: state, type: categorical}
  - {name: year, column: year, type: time}
  - {name: age, column: age_group, type: categorical}
measures:
  - {name: cases, column: cases, kind: count}
  - {name: population, column: population, kind: count}
metrics:
  - {name: cases, description: Cases, measure: cases, aggregation: sum}
  - {name: pop, description: Population, measure: population, aggregation: sum}
series:
  - {condition: salmonellosis, label: Salmonellosis, metric: cases, measure: cases, location_dimension: state,
     time_dimension: year, age_dimension: age}
  - {condition: population, label: Population by age, metric: pop, measure: population, location_dimension: state,
     time_dimension: year, age_dimension: age}
"""

AGE_LABELS = ["Under 1 year", "1-4 years", "5-14 years", "15-24 years", "25-34 years", "35-44 years",
              "45-54 years", "55-64 years", "65-74 years", "75-84 years", "85 years and over"]  # fmt: skip
AGE_CASES = [2, 1, 3, 10, 14, 20, 25, 30, 40, 52, 30]
AGE_POPS = [9_000, 38_000, 101_000, 99_000, 95_000, 120_000, 110_000, 70_000, 52_000, 33_000, 11_000]


def weekly_count(area: str, year: int, week: int) -> int | None:
    if (area, year, week) == SUPPRESSED:
        return None
    if area == "US RESIDENTS":
        return 50
    if area == "MAINE":
        return 1 if year == 2023 and week <= 5 else 0  # exactly 5 cases in 2023: large-sample methods apply
    if area == "VERMONT":
        return 1 if year == 2023 and week in (10, 30) else 0  # 2 cases in 2023, none in other years
    if area == "WYOMING":
        return 1 if week % 4 == 0 else 0  # 13 a year: below the toy unreliable-rate rule of 20
    base = {"MICHIGAN": 3, "OHIO": 5, "NEW YORK": 4}[area]
    v = base + week % 3 + (year - 2015) // 3
    if area == "MICHIGAN" and year == 2024 and 17 <= week <= 20:
        v += 30  # an outbreak-sized jump
    return v


def annual(area: str, year: int) -> int:
    return sum(weekly_count(area, year, w) or 0 for w in WEEKS)


def case_records() -> list[dict[str, Any]]:
    out = []
    for area in AREAS:
        for year in YEARS:
            ytd = 0
            for week in WEEKS:
                ytd += weekly_count(area, year, week) or 0  # late reports still reach the running total
                prior = None
                if year > YEARS[0]:
                    prior = sum(weekly_count(area, year - 1, w) or 0 for w in range(1, week + 1))
                out.append({"area": area, "year": year, "week": week, "wk": weekly_count(area, year, week),
                            "ytd": ytd, "prior_ytd": prior})  # fmt: skip
    return out


def pop_records() -> list[dict[str, Any]]:
    # Populations for 2015-2023 only, so 2024 has no denominator.
    return [
        {"state": s, "year": y, "population": p + 10_000 * (y - 2015)}
        for s, p in POP.items()
        for y in range(2015, 2024)
    ]


def age_records() -> list[dict[str, Any]]:
    rows = [
        {"state": "Michigan", "year": 2023, "age_group": g, "cases": c, "population": n}
        for g, c, n in zip(AGE_LABELS, AGE_CASES, AGE_POPS, strict=True)
    ]
    rows += [  # 2022: one group's count suppressed
        {"state": "Michigan", "year": 2022, "age_group": g, "cases": None if i == 3 else c, "population": n}
        for i, (g, c, n) in enumerate(zip(AGE_LABELS, AGE_CASES, AGE_POPS, strict=True))
    ]
    rows += [  # Ohio 2023: groups that are not the standard's
        {"state": "Ohio", "year": 2023, "age_group": g, "cases": 5, "population": 1000}
        for g in ["0-17 years", "18-64 years", "65 years and over"]
    ]
    return rows


def build(
    tmp: Path, *, rules: str = CASES_RULES, with_pop: bool = True, with_age: bool = True
) -> tuple[Registry, Engine]:
    root = tmp / "reg"
    for d in ("catalog", "contracts", "semantic"):
        (root / d).mkdir(parents=True, exist_ok=True)
    files = {"toy_weekly": (CASES_CONTRACT.replace("RULES_HERE\n", rules), CASES_SEMANTIC, case_records())}
    if with_pop:
        files["toy_pop"] = (POP_CONTRACT, POP_SEMANTIC, pop_records())
    if with_age:
        files["toy_age"] = (AGE_CONTRACT, AGE_SEMANTIC, age_records())
    for ds, (contract, semantic, _) in files.items():
        (root / "catalog" / f"{ds}.yaml").write_text(catalog(ds))
        (root / "contracts" / f"{ds}.odcs.yaml").write_text(textwrap.dedent(contract))
        (root / "semantic" / f"{ds}.yaml").write_text(textwrap.dedent(semantic))
    reg = load_registry(root)
    lake = tmp / "lake"
    lake.mkdir()
    for ds, (_, _, records) in files.items():
        f = tmp / f"{ds}.ndjson"
        write_ndjson(f, records)
        build_snapshot(
            lake, reg.get(ds), [raw_fetch(f"https://data.cdc.gov/resource/{ds}.json", records)], [f]
        )
    return reg, Engine(registry=reg, lake=lake)


@pytest.fixture
def env(tmp_path: Path) -> tuple[Registry, Engine]:
    return build(tmp_path)


def rows_by(out: dict[str, Any], key: str) -> dict[Any, dict[str, Any]]:
    cols = out["columns"]
    return {r[cols.index(key)]: dict(zip(cols, r, strict=True)) for r in out["rows"]}


# ---------------------------------------------------------------------------------- get_rate


def test_rate_matches_hand_computation_and_garwood(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    out = analysis.get_rate(reg, eng, condition="legionellosis", year=2023, fmt="json")
    rows = rows_by(out, "location")
    mi = rows["Michigan"]
    cases, pop = annual("MICHIGAN", 2023), POP["Michigan"] + 80_000
    assert (mi["cases"], mi["population"]) == (cases, pop)
    lo, hi = stats.poisson_exact_ci(cases)
    assert mi["rate_per_100k"] == pytest.approx(cases / pop * 1e5, rel=1e-3)
    assert mi["lower_95"] == pytest.approx(lo / pop * 1e5, rel=1e-3)
    assert mi["upper_95"] == pytest.approx(hi / pop * 1e5, rel=1e-3)
    # The year's own running total at year end: provisional, never swapped for next year's restatement.
    assert mi["period_status"].startswith("provisional: as published at year end")
    assert mi["flags"] == "provisional: may change"
    assert any("year_end_ytd" in n and "never used as a count" in n for n in out["notes"])
    # The national rate uses the US RESIDENTS row and the sum of the state populations.
    us = rows["United States"]
    assert us["cases"] == annual("US RESIDENTS", 2023)
    assert us["population"] == sum(POP.values()) + len(POP) * 80_000
    assert any("Garwood" in n for n in out["notes"]) and any(
        "not age-standardised" in n for n in out["notes"]
    )
    assert any("toy_weekly: unreliable rates are flagged by the source's own rule" in n for n in out["notes"])
    assert any(n.startswith("Source: toy_pop") for n in out["notes"])


def test_rate_flags_unreliable_partial_and_suppressed_never_zero(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    rows = rows_by(analysis.get_rate(reg, eng, condition="legionellosis", year=2023, fmt="json"), "location")
    assert "unreliable by the source's own rule" in rows["Wyoming"]["flags"]
    assert rows["Wyoming"]["rate_per_100k"] is not None
    ny = rows["New York"]
    assert ny["rate_per_100k"] is None and "excludes New York City" in ny["flags"]
    assert "unreliable" not in rows["Michigan"]["flags"]


def test_rate_for_a_suppressed_count_is_empty_not_zero(tmp_path: Path) -> None:
    # A whole year suppressed for one place: the annual total is NULL, so no rate.
    reg, eng = build(tmp_path)
    out = analysis.get_rate(
        reg, eng, condition="legionellosis", year=2024, locations=["Michigan"], fmt="json"
    )
    mi = rows_by(out, "location")["Michigan"]
    assert mi["rate_per_100k"] is None and "no Census population estimate for 2024" in mi["flags"]
    assert "provisional" in mi["flags"]


def test_rate_without_a_rule_says_so_and_never_hard_codes_one(tmp_path: Path) -> None:
    reg, eng = build(tmp_path, rules="")
    out = analysis.get_rate(reg, eng, condition="legionellosis", year=2023, fmt="json")
    assert "unreliable" not in rows_by(out, "location")["Wyoming"]["flags"]
    assert any("records no unreliable-rate rule" in n for n in out["notes"])


def test_relative_width_rule_and_bad_rule(tmp_path: Path) -> None:
    rule = CASES_RULES.replace(
        "{kind: min_count, threshold: 20,", "{kind: max_relative_ci_width, threshold: 1.6,"
    )
    reg, eng = build(tmp_path / "a", rules=rule)
    rows = rows_by(analysis.get_rate(reg, eng, condition="legionellosis", year=2023, fmt="json"), "location")
    wy = rows["Wyoming"]  # 13 cases: (upper - lower) / rate = 1.10, under 1.6
    assert (wy["upper_95"] - wy["lower_95"]) / wy["rate_per_100k"] < 1.6 and "unreliable" not in wy["flags"]
    reg2, eng2 = build(tmp_path / "b", rules=rule.replace("threshold: 1.6", "threshold: 1.0"))
    rows2 = rows_by(
        analysis.get_rate(reg2, eng2, condition="legionellosis", year=2023, fmt="json"), "location"
    )
    assert "unreliable" in rows2["Wyoming"]["flags"] and "unreliable" not in rows2["Michigan"]["flags"]
    bad = CASES_RULES.replace("kind: min_count", "kind: guess")
    reg3, eng3 = build(tmp_path / "c", rules=bad)
    text = analysis.get_rate(reg3, eng3, condition="legionellosis", year=2023)
    assert isinstance(text, str) and "can't read" in text and "Traceback" not in text


def test_rate_refusals_are_plain(tmp_path: Path, env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    assert "as case counts" in str(analysis.get_rate(reg, eng, condition="mumps", year=2023))
    reg2, eng2 = build(tmp_path / "nopop", with_pop=False, with_age=False)
    assert "aren't loaded yet" in str(analysis.get_rate(reg2, eng2, condition="legionellosis", year=2023))
    # tools.get_rate is the same tool
    assert tools.get_rate(reg, eng, condition="legionellosis", year=2023) == analysis.get_rate(
        reg, eng, condition="legionellosis", year=2023
    )
    nothing = analysis.get_rate(
        reg, eng, condition="legionellosis", year=2023, locations=["Texas"], fmt="json"
    )
    assert nothing["rows"] == [] and any("No rows for: Texas" in n for n in nothing["notes"])


# ---------------------------------------------------------------------------------- compare_rates


def test_compare_two_places_two_ways(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    out = analysis.compare_rates(
        reg, eng, condition="legionellosis", location="Ohio", year=2023, other_location="Michigan", fmt="json"
    )
    by = {r[0].split(" (")[0]: r for r in out["rows"]}
    a, pa = annual("OHIO", 2023), POP["Ohio"] + 80_000
    b, pb = annual("MICHIGAN", 2023), POP["Michigan"] + 80_000
    rr, rd = stats.rate_ratio(a, pa, b, pb), stats.rate_difference(a, pa, b, pb)
    assert by["rate ratio"][1:4] == [pytest.approx(rr.value, rel=1e-3), pytest.approx(rr.lower, rel=1e-3),
                                     pytest.approx(rr.upper, rel=1e-3)]  # fmt: skip
    assert by["rate difference per 100,000"][1] == pytest.approx((a / pa - b / pb) * 1e5, rel=1e-3)
    assert by["rate difference per 100,000"][2] == pytest.approx(rd.lower, rel=1e-3)
    assert any("log method" in n for n in out["notes"])


def test_compare_two_years_notes_definition_change(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    out = analysis.compare_rates(
        reg, eng, condition="legionellosis", location="Michigan", year=2021, other_year=2018, fmt="json"
    )
    assert out["notes"][0].startswith("The case definition changed in 2020")
    same_side = analysis.compare_rates(
        reg, eng, condition="legionellosis", location="Michigan", year=2022, other_year=2021, fmt="json"
    )
    assert not any("definition changed" in n for n in same_side["notes"])


def test_compare_refusals(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    same = analysis.compare_rates(reg, eng, condition="legionellosis", location="Michigan", year=2023,
                                  other_location="MI")  # fmt: skip
    assert "Say what to compare with" in str(same)
    ny = analysis.compare_rates(reg, eng, condition="legionellosis", location="New York", year=2023,
                                other_location="Ohio")  # fmt: skip
    assert "can't be worked out" in str(ny) and "New York City" in str(ny)
    later = analysis.compare_rates(
        reg, eng, condition="legionellosis", location="Ohio", year=2023, other_year=2024
    )
    assert "Ohio in 2024" in str(later) and "no Census population estimate for 2024" in str(later)
    assert "as case counts" in str(analysis.compare_rates(reg, eng, condition="mumps", location="Ohio", year=2023,
                                                          other_year=2022))  # fmt: skip


def _compare(reg: Registry, eng: Engine, loc: str, year: int, other: str) -> dict[str, list[Any]]:
    out = analysis.compare_rates(reg, eng, condition="legionellosis", location=loc, year=year, other_location=other,
                                 fmt="json")  # fmt: skip
    return {r[0].split(" (")[0]: r for r in out["rows"]}


def test_compare_sparse_counts_use_exact_ratio_and_no_difference_interval(
    env: tuple[Registry, Engine],
) -> None:
    reg, eng = env
    pv, po = POP["Vermont"] + 80_000, POP["Ohio"] + 80_000
    two = _compare(reg, eng, "Vermont", 2023, "Ohio")  # 2 cases against Ohio's hundreds
    exact = stats.rate_ratio_exact(2, pv, annual("OHIO", 2023), po)
    assert two["rate ratio"][1:4] == [pytest.approx(exact.value, rel=1e-3), pytest.approx(exact.lower, rel=1e-3),
                                      pytest.approx(exact.upper, rel=1e-3)]  # fmt: skip
    assert "exact conditional" in two["rate ratio"][7]
    assert two["rate difference per 100,000"][2:4] == [None, None]
    assert "not trustworthy" in two["rate difference per 100,000"][7]
    zero = _compare(reg, eng, "Vermont", 2022, "Ohio")  # no cases: ratio 0 with a finite exact upper limit
    assert zero["rate ratio"][1] == 0 and zero["rate ratio"][2] == 0 and zero["rate ratio"][3] > 0
    reverse = _compare(reg, eng, "Ohio", 2022, "Vermont")  # no cases in the comparison group: no ratio
    assert reverse["rate ratio"][1] is None and "no upper limit" in reverse["rate ratio"][7]
    big = _compare(reg, eng, "Ohio", 2023, "Michigan")
    assert big["rate ratio"][7] == "" and big["rate difference per 100,000"][2] is not None


# ---------------------------------------------------------------------------------- get_trend


def test_trend_splits_at_definition_change_and_leaves_out_provisional(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    out = analysis.get_trend(reg, eng, condition="legionellosis", location="Michigan", fmt="json")
    rows = rows_by(out, "case_definition_from")
    assert set(rows) == {"", 2020}
    before, after = rows[""], rows[2020]
    assert before["years"] == "2015-2019" and before["points_used"] == 5
    assert after["years"] == "2020-2023" and "2024 provisional" in after["left_out"]
    years = list(range(2020, 2024))
    fit = stats.poisson_trend(
        years,
        [annual("MICHIGAN", y) for y in years],
        [POP["Michigan"] + 10_000 * (y - 2015) for y in years],
    )
    assert after["annual_percent_change"] == pytest.approx(fit.apc, rel=1e-3)
    assert after["lower_95"] == pytest.approx(fit.lower, rel=1e-3)
    assert after["fit_on"].startswith("rates")
    assert any("split there" in n for n in out["notes"])


def test_trend_never_falls_back_from_rates_to_counts(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    refused = analysis.get_trend(reg, eng, condition="legionellosis", location="Michigan", year_start=2020,
                                 include_provisional=True)  # fmt: skip
    assert "needs a Census population for every year" in str(refused) and "Michigan in 2024" in str(refused)
    assert 'fit_on="counts"' in str(refused) and "2020-2023" in str(refused)
    out = analysis.get_trend(reg, eng, condition="legionellosis", location="Michigan", year_start=2020,
                             include_provisional=True, fit_on="counts", fmt="json")  # fmt: skip
    row = dict(zip(out["columns"], out["rows"][0], strict=True))
    assert row["years"] == "2020-2024" and row["fit_on"].startswith("case counts")
    years = list(range(2020, 2025))
    fit = stats.poisson_trend(years, [annual("MICHIGAN", y) for y in years])
    assert row["annual_percent_change"] == pytest.approx(fit.apc, rel=1e-3)
    assert any("population growth or decline is not taken into account" in n for n in out["notes"])
    assert "rates" in str(analysis.get_trend(reg, eng, condition="legionellosis", location="Ohio",
                                             fit_on="odds"))  # type: ignore[arg-type]  # fmt: skip
    ny = analysis.get_trend(
        reg, eng, condition="legionellosis", location="New York", fit_on="counts", fmt="json"
    )
    assert ny["rows"] and any("cover only part" in n for n in ny["notes"])


def test_trend_without_definition_changes_says_it_assumes_none(tmp_path: Path) -> None:
    reg, eng = build(tmp_path, rules="")
    out = analysis.get_trend(reg, eng, condition="legionellosis", location="Ohio", fmt="json")
    assert len(out["rows"]) == 1 and out["rows"][0][2] == "2015-2023"
    assert any("assumes the definition stayed the same" in n for n in out["notes"])


def test_trend_refuses_too_few_points(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    text = analysis.get_trend(reg, eng, condition="legionellosis", location="Michigan", year_start=2022)
    assert "at least 3 usable years" in str(text) and "include_provisional" in str(text)
    ny = analysis.get_trend(reg, eng, condition="legionellosis", location="New York")
    assert "only part of the place" in str(ny)
    assert "as case counts" in str(analysis.get_trend(reg, eng, condition="mumps", location="Ohio"))


# ---------------------------------------------------------------------------------- age_adjust


def test_age_adjust_two_ways(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    out = analysis.age_adjust(reg, eng, condition="salmonellosis", location="Michigan", year=2023, fmt="json")
    row = dict(zip(out["columns"], out["rows"][0], strict=True))
    std = [w for *_, w in stats.US2000_STD_11]
    by_hand = sum(d / n * w / 1e6 for d, n, w in zip(AGE_CASES, AGE_POPS, std, strict=True)) * 1e5
    assert row["age_adjusted_rate_per_100k"] == pytest.approx(by_hand, rel=1e-3)
    est = stats.direct_standardize(AGE_CASES, AGE_POPS, std)
    assert (row["lower_95"], row["upper_95"]) == (
        pytest.approx(est.lower, rel=1e-3),
        pytest.approx(est.upper, rel=1e-3),
    )
    assert row["crude_rate_per_100k"] == pytest.approx(sum(AGE_CASES) / sum(AGE_POPS) * 1e5, rel=1e-3)
    assert "11 age groups" in row["standard"]
    assert any("Never compare this with GBD" in n for n in out["notes"])


def test_age_adjust_refuses_plainly(tmp_path: Path, env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    assert "suppressed or missing" in str(analysis.age_adjust(reg, eng, condition="salmonellosis",
                                                             location="Michigan", year=2022))  # fmt: skip
    assert "same age groups as the 2000 US standard" in str(
        analysis.age_adjust(reg, eng, condition="salmonellosis", location="Ohio", year=2023)
    )
    assert "No age-specific counts" in str(
        analysis.age_adjust(reg, eng, condition="salmonellosis", location="Michigan", year=2019)
    )
    no_age = analysis.age_adjust(reg, eng, condition="legionellosis", location="Michigan", year=2023)
    assert "case counts by age group" in str(no_age) and "get_rate" in str(no_age)
    reg2, eng2 = build(tmp_path / "noage", with_age=False)
    assert "by age group" in str(analysis.age_adjust(reg2, eng2, condition="salmonellosis", location="Ohio",
                                                    year=2023))  # fmt: skip


@pytest.mark.parametrize(
    "label,expected",
    [("<1", (0, 0)), ("Under 1 year", (0, 0)), ("00 years", (0, 0)), ("01-04 years", (1, 4)),
     ("1–4 years", (1, 4)), ("85+", (85, None)), ("85 years and over", (85, None)), ("85+ years", (85, None)),
     ("65 and older", (65, None)), ("unknown", None), ("1, 4", None)],
)  # fmt: skip
def test_parse_age_group(label: str, expected: tuple[int, int | None] | None) -> None:
    assert analysis.parse_age_group(label) == expected


# ---------------------------------------------------------------------------------- check_signal


def test_signal_flags_the_outbreak_by_hand(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    out = analysis.check_signal(reg, eng, condition="legionellosis", location="Michigan", year=2024, week=20,
                                fmt="json")  # fmt: skip
    row = dict(zip(out["columns"], out["rows"][0], strict=True))
    current = sum(weekly_count("MICHIGAN", 2024, w) or 0 for w in range(17, 21))
    hist = [
        float(sum(weekly_count("MICHIGAN", y, w) or 0 for w in range(first, first + 4)))
        for y in range(2019, 2024)
        for first in (13, 17, 21)
    ]
    sig = stats.historical_limits(current, hist)
    assert row["current_4_week_total"] == current and row["weeks"] == "17-20"
    assert row["ratio"] == pytest.approx(sig.ratio, rel=1e-3)
    assert row["signal"] == "above historical limits"
    assert any("Stroup" in n for n in out["notes"]) and any("not an outbreak" in n for n in out["notes"])
    calm = analysis.check_signal(reg, eng, condition="legionellosis", location="Michigan", year=2023, week=20,
                                 fmt="json")  # fmt: skip
    assert calm["rows"][0][calm["columns"].index("signal")] == "within limits"


def test_signal_refusals(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    gap = analysis.check_signal(reg, eng, condition="legionellosis", location="Ohio", year=2024, week=30)
    assert "can't be counted as zero" in str(gap) and "2022 weeks" in str(gap)
    short = analysis.check_signal(
        reg, eng, condition="legionellosis", location="Michigan", year=2018, week=20
    )
    assert "are not in the data" in str(short)
    assert "turn of the year" in str(analysis.check_signal(reg, eng, condition="legionellosis",
                                                           location="Michigan", year=2024, week=3))  # fmt: skip
    assert "turn of the year" in str(analysis.check_signal(reg, eng, condition="legionellosis",
                                                           location="Michigan", year=2024, week=50))  # fmt: skip
    assert "weekly counts" in str(analysis.check_signal(reg, eng, condition="mumps", location="Ohio", year=2024,
                                                        week=20))  # fmt: skip


def test_weekly_series_never_reach_get_series(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    out = tools.get_series(reg, eng, condition="legionellosis", locations=["Michigan"], year_start=2023,
                           year_end=2023, fmt="json")  # fmt: skip
    # One annual value per observation type, not weekly sums: the year's own total and next year's restatement.
    cols = out["columns"]
    by_type = {r[cols.index("observation_type")]: r[cols.index("value")] for r in out["rows"]}
    assert by_type == {
        "year_end_ytd": annual("MICHIGAN", 2023),
        "subsequent_year_restatement": annual("MICHIGAN", 2023),
    }
    assert len(tools.series_for(reg, "legionellosis", kind="weekly")) == 1


def test_no_snapshot_is_a_plain_refusal(tmp_path: Path) -> None:
    reg, _ = build(tmp_path)
    empty = Engine(registry=reg, lake=tmp_path / "empty")
    for text in (
        analysis.get_rate(reg, empty, condition="legionellosis", year=2023),
        analysis.get_trend(reg, empty, condition="legionellosis", location="Ohio"),
        analysis.compare_rates(
            reg, empty, condition="legionellosis", location="Ohio", year=2023, other_year=2022
        ),
        analysis.check_signal(reg, empty, condition="legionellosis", location="Ohio", year=2023, week=20),
        analysis.age_adjust(reg, empty, condition="salmonellosis", location="Michigan", year=2023),
    ):
        assert "hasn't been downloaded" in str(text)


def test_cli_and_mcp_give_identical_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    from health_data_registry import cli
    from health_data_registry.server import build_server

    reg, eng = build(tmp_path)
    monkeypatch.setenv("HDR_REGISTRY", str(reg.root))
    monkeypatch.setenv("HDR_LAKE", str(eng.lake))
    calls = [
        (["get-rate", "legionellosis", "--year", "2023", "--location", "Ohio"],
         "get_rate", {"condition": "legionellosis", "year": 2023, "locations": ["Ohio"]}),
        (["check-signal", "legionellosis", "--location", "Michigan", "--year", "2024", "--week", "20"],
         "check_signal", {"condition": "legionellosis", "location": "Michigan", "year": 2024, "week": 20}),
        (["age-adjust", "legionellosis", "--location", "Michigan", "--year", "2023"],
         "age_adjust", {"condition": "legionellosis", "location": "Michigan", "year": 2023}),
    ]  # fmt: skip
    server = build_server(reg, eng)
    for argv, tool, args in calls:
        assert cli.main(argv) == 0
        printed = capsys.readouterr().out
        mcp = asyncio.run(server.call_tool(tool, args)).content[0].text  # type: ignore[union-attr]
        assert printed == mcp
    assert cli.main(["get-trend", "legionellosis", "--location", "Ohio", "--json"]) == 0
    assert '"annual_percent_change"' in capsys.readouterr().out


def test_exactly_five_cases_uses_the_large_sample_methods(env: tuple[Registry, Engine]) -> None:
    reg, eng = env
    five = _compare(reg, eng, "Maine", 2023, "Ohio")
    assert five["rate ratio"][7] == "" and five["rate difference per 100,000"][2] is not None


def test_definition_change_note_at_the_boundary_years(env: tuple[Registry, Engine]) -> None:
    reg, eng = env

    def notes(y1: int, y2: int) -> list[str]:
        out = analysis.compare_rates(reg, eng, condition="legionellosis", location="Ohio", year=y1, other_year=y2,
                                     fmt="json")  # fmt: skip
        return [n for n in out["notes"] if "definition changed" in n]

    assert notes(2020, 2019) and notes(2019, 2020)  # the 2020 change lies between them
    assert not notes(2020, 2022) and not notes(2022, 2020)  # both years under the 2020 definition


REFUSALS = [
    (lambda r, e: analysis.get_rate(r, e, condition="mumps", year=2023, fmt="json"), "unknown_condition"),
    (lambda r, e: analysis.compare_rates(r, e, condition="legionellosis", location="Ohio", year=2023,
                                         fmt="json"), "nothing_to_compare"),
    (lambda r, e: analysis.compare_rates(r, e, condition="mumps", location="Ohio", year=2023, other_year=2022,
                                         fmt="json"), "unknown_condition"),
    (lambda r, e: analysis.compare_rates(r, e, condition="legionellosis", location="New York", year=2023,
                                         other_location="Ohio", fmt="json"), "no_rate"),
    (lambda r, e: analysis.get_trend(r, e, condition="mumps", location="Ohio", fmt="json"), "unknown_condition"),
    (lambda r, e: analysis.get_trend(r, e, condition="legionellosis", location="Michigan", year_start=2022,
                                     fmt="json"), "too_few_points"),
    (lambda r, e: analysis.get_trend(r, e, condition="legionellosis", location="Michigan", year_start=2020,
                                     include_provisional=True, fmt="json"), "no_denominator"),
    (lambda r, e: analysis.get_trend(r, e, condition="legionellosis", location="Ohio", fit_on="x",
                                     fmt="json"), "bad_fit_on"),
    (lambda r, e: analysis.age_adjust(r, e, condition="legionellosis", location="Ohio", year=2023, fmt="json"),
     "no_age_groups"),
    (lambda r, e: analysis.age_adjust(r, e, condition="salmonellosis", location="Ohio", year=2023, fmt="json"),
     "age_groups_mismatch"),
    (lambda r, e: analysis.age_adjust(r, e, condition="salmonellosis", location="Michigan", year=2022,
                                      fmt="json"), "suppressed_age_group"),
    (lambda r, e: analysis.age_adjust(r, e, condition="salmonellosis", location="Michigan", year=2019,
                                      fmt="json"), "no_rows"),
    (lambda r, e: analysis.check_signal(r, e, condition="mumps", location="Ohio", year=2024, week=20,
                                        fmt="json"), "no_weekly_series"),
    (lambda r, e: analysis.check_signal(r, e, condition="legionellosis", location="Ohio", year=2024, week=7,
                                        fmt="json"), "week_out_of_range"),
    (lambda r, e: analysis.check_signal(r, e, condition="legionellosis", location="Ohio", year=2024, week=30,
                                        fmt="json"), "incomplete_history"),
]  # fmt: skip


@pytest.mark.parametrize("n", range(len(REFUSALS)))
def test_refusals_in_json_carry_their_code(env: tuple[Registry, Engine], n: int) -> None:
    reg, eng = env
    call, code = REFUSALS[n]
    out = call(reg, eng)
    assert isinstance(out, dict) and out["refused"] == code and out["message"]


def test_refusal_without_population_in_json(tmp_path: Path) -> None:
    reg, eng = build(tmp_path, with_pop=False, with_age=False)
    out = analysis.get_rate(reg, eng, condition="legionellosis", year=2023, fmt="json")
    assert out["refused"] == "no_denominator"
    out2 = analysis.compare_rates(reg, eng, condition="legionellosis", location="Ohio", year=2023, other_year=2022,
                                  fmt="json")  # fmt: skip
    assert out2["refused"] == "no_denominator"


def test_rate_for_the_nation_reads_a_population_without_places(env: tuple[Registry, Engine]) -> None:
    """Regression: the national population series has no place breakdown, and must not be place-matched."""
    reg, eng = env
    out = analysis.get_rate(
        reg, eng, condition="legionellosis", year=2023, locations=["United States"], fmt="json"
    )
    rows = rows_by(out, "location")
    us = rows["United States"]
    assert us["cases"] == annual("US RESIDENTS", 2023)
    assert us["population"] == sum(POP.values()) + len(POP) * 80_000
    pop = next(
        s for d in reg for s in d.semantic.series if s.measure == "population" and not s.location_dimension
    )
    got, unmatched = analysis.read_series(eng, reg.get("toy_pop"), pop, ["Michigan"], 2023, 2023)
    assert (got, unmatched) == ([], ["Michigan"])  # a combined figure is never one place's


def test_a_running_total_that_stops_early_is_never_a_year_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Michigan 2022's running total has no number after week 40: not the year's total, so never counted."""
    original = case_records()

    def stopped() -> list[dict[str, Any]]:
        return [
            {**r, "ytd": None} if (r["area"], r["year"]) == ("MICHIGAN", 2022) and r["week"] > 40 else r
            for r in original
        ]

    monkeypatch.setattr(sys.modules[__name__], "case_records", stopped)
    reg, eng = build(tmp_path)
    out = analysis.get_rate(
        reg, eng, condition="legionellosis", year=2022, locations=["Michigan"], fmt="json"
    )
    mi = rows_by(out, "location")["Michigan"]
    assert mi["rate_per_100k"] is None and mi["lower_95"] is None
    assert mi["period_status"].startswith("incomplete: running total last published at week 40 of 52")
    assert mi["flags"].endswith("; no rate")
    trend = analysis.get_trend(
        reg, eng, condition="legionellosis", location="Michigan", fit_on="counts", fmt="json"
    )
    after = rows_by(trend, "case_definition_from")[2020]
    assert after["years"] == "2020-2023" and after["points_used"] == 3
    assert "2022 not the year's total (incomplete" in after["left_out"]


def test_compare_refuses_a_condition_that_matches_two_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sides must come from one series: never one kind of count against another."""
    extra = (
        "  - {condition: legionellosis_travel, label: 'Legionellosis, travel-associated', metric: annual, "
        "measure: cases,\n     location_dimension: state, time_dimension: year}\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "CASES_SEMANTIC", CASES_SEMANTIC + extra)
    reg, eng = build(tmp_path)
    out = analysis.compare_rates(reg, eng, condition="legionellosis", location="Ohio", year=2023,
                                 other_location="Michigan", fmt="json")  # fmt: skip
    assert out["refused"] == "ambiguous_condition"
    assert "Legionellosis from toy_weekly" in out["message"]
    assert "Legionellosis, travel-associated from toy_weekly" in out["message"]
    one = analysis.compare_rates(reg, eng, condition="travel-associated", location="Ohio", year=2023,
                                 other_location="Michigan", fmt="json")  # fmt: skip
    assert "rows" in one and len(one["rows"]) == 4


def test_a_zero_population_leaves_that_year_out_of_a_rate_trend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = pop_records()
    zero = [{**r, "population": 0} if (r["state"], r["year"]) == ("Michigan", 2016) else r for r in original]
    monkeypatch.setattr(sys.modules[__name__], "pop_records", lambda: zero)
    reg, eng = build(tmp_path)
    out = analysis.get_trend(reg, eng, condition="legionellosis", location="Michigan", fmt="json")
    assert "rows" in out, out  # not a StatsError refusal for the whole answer
    assert any("Michigan in 2016" in n for n in out["notes"])


def test_an_empty_population_never_hides_a_real_one(
    env: tuple[Registry, Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    reg, eng = env
    ds = reg.get("toy_pop")
    s = ds.semantic.series[0]
    answers = [None, 5000.0]

    def fake(*a: Any, **k: Any) -> tuple[list[analysis.Obs], list[str]]:
        return [analysis.Obs("Michigan", "MICHIGAN", 2023, answers.pop(0), "")], []

    monkeypatch.setattr(analysis, "read_series", fake)
    found, years = analysis.populations(eng, [(ds, s), (ds, s)], ["Michigan"], 2023, 2023)
    assert found[("Michigan", 2023)][0] == 5000.0 and years == {2023}


def test_a_schema_level_partial_areas_is_refused() -> None:
    import yaml

    from health_data_registry.registry import RegistryError, parse_contract

    bad = CASES_CONTRACT.replace(
        "      - {property: totalRows, value: \"upper(area) = 'US RESIDENTS'\"}\n",
        "      - {property: totalRows, value: \"upper(area) = 'US RESIDENTS'\"}\n"
        '      - {property: partialAreas, value: {"NEW YORK": "excludes New York City"}}\n',
    )
    assert bad != CASES_CONTRACT
    with pytest.raises(RegistryError, match="once, as partialPlaces"):
        parse_contract(yaml.safe_load(bad.replace("RULES_HERE\n", "")), expected_id="toy_weekly")
