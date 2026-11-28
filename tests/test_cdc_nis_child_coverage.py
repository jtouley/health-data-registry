"""NIS-Child vaccination coverage (cdc_nis_child_coverage): the real contract and semantic model on hand-written rows.

Synthetic rows only, laid out like data.cdc.gov fhky-rtsk. They pin hard rule 2 (coverage percentages are read as
published, never added up across places, ages, doses or birth cohorts) and hard rule 15 (CDC's interval text,
'92.2 to 96.1', is kept as sent: splitting it waits for a staging extension, #172).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import quality, tools
from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_nis_child_coverage"
MMR = "≥1 Dose MMR"


def row(
    place: str,
    year: str,
    estimate: str,
    ci: str | None,
    *,
    vaccine: str = MMR,
    dose: str = "",
    age: str = "24 Months",
) -> dict[str, Any]:
    r = {"vaccine": vaccine, "dose": dose, "geography_type": "States/Local Areas", "geography": place,
         "year_season": year, "dimension_type": "Age", "dimension": age, "coverage_estimate": estimate,
         "population_sample_size": None if estimate == "NA" else "300"}  # fmt: skip
    if ci is not None:  # the API leaves the field out when CDC gives no interval
        r["_95_ci"] = ci
    return r


ROWS = [
    row("Michigan", "2021", "91.2", "85.6 to 95.2"),
    row("Michigan", "2022", "93.4", "87.9 to 97.0"),
    row("Michigan", "2021-2022", "92.4", "88.6 to 95.3"),  # a two-year cohort beside its single years
    row("Michigan", "2022", "95.0", "91.0 to 97.5", age="35 Months"),  # includes coverage by 24 months
    row("Ohio", "2022", "90.1", "86.0 to 93.4"),
    row("Michigan", "2022", "80.5", "74.9 to 85.2", vaccine="DTaP", dose="≥4 Doses"),
    row("Michigan", "2022", "93.9", "89.1 to 96.8", vaccine="DTaP", dose="≥3 Doses"),
    row("Michigan", "2020", "NA", None),
]


@pytest.fixture
def built(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "rows.ndjson"
    write_ndjson(f, ROWS)
    build_snapshot(lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/fhky-rtsk.json", ROWS)], [f])
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


def _rows(out: Any) -> list[dict[str, Any]]:
    return [dict(zip(out["columns"], r, strict=True)) for r in out["rows"]]


def test_interval_is_kept_as_published_text() -> None:
    col = load_registry(ROOT).get(DS).contract.column("ci_95")
    assert (col.physical_type, col.source_field, col.role) == ("VARCHAR", "_95_ci", "attribute")


def test_coverage_is_read_as_published_never_summed() -> None:
    ds = load_registry(ROOT).get(DS)
    metric = ds.semantic.metric("vaccination_coverage")
    assert metric.aggregation == "none"
    assert metric.uncertainty == "none" and "ci_95" in (metric.uncertainty_reason or "")
    assert ds.semantic.dimension("birth_year").non_additive  # cohorts include their single years
    cm = compile_metric(ds, "vaccination_coverage", group_by=["place", "birth_year"])
    assert "sum(" not in cm.sql and "not summed" in cm.method


def test_get_series_reads_one_dose_by_24_months(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    out = tools.get_series(
        reg, eng, condition=f"{MMR} vaccination coverage", locations=["Michigan"], fmt="json"
    )
    got = {str(r["year"]): r["value"] for r in _rows(out)}
    # By 35 months (95.0) is never read for the 24-month series; the 2021-2022 cohort overlaps its single years,
    # so the series reads single birth years only; NA is no value.
    assert got == {"2020": None, "2021": 91.2, "2022": 93.4}
    dtap = tools.get_series(
        reg, eng, condition="DTaP vaccination coverage", locations=["Michigan"], fmt="json"
    )
    assert [(r["year"], r["value"]) for r in _rows(dtap)] == [("2022", 80.5)]  # four doses, never three added


def test_the_interval_comes_back_as_sent(built: tuple[Registry, Engine]) -> None:
    _reg, eng = built
    out = tools.query_sql(
        eng,
        f"SELECT year_season, coverage_estimate, ci_95 FROM {DS} WHERE geography = 'Michigan' "
        "AND vaccine = '≥1 Dose MMR' AND dimension = '24 Months' ORDER BY year_season",
        fmt="json",
    )
    got = [(r["year_season"], r["coverage_estimate"], r["ci_95"]) for r in _rows(out)]
    assert got == [
        ("2020", None, None),
        ("2021", 91.2, "85.6 to 95.2"),
        ("2021-2022", 92.4, "88.6 to 95.3"),
        ("2022", 93.4, "87.9 to 97.0"),
    ]


def test_a_year_range_never_returns_a_cohort_that_reaches_outside_it(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    # As text, '2021-2022' sorts inside a 2021-to-2021 period; the series must not return it (2022 births).
    out = tools.get_series(
        reg,
        eng,
        condition=f"{MMR} vaccination coverage",
        locations=["Michigan"],
        year_start=2021,
        year_end=2021,
        fmt="json",
    )
    assert [(str(r["year"]), r["value"]) for r in _rows(out)] == [("2021", 91.2)]


def test_places_are_never_added(built: tuple[Registry, Engine]) -> None:
    _reg, eng = built
    # Every other dimension pinned to one value, so only the place varies (Michigan and Ohio, 2022).
    pinned = {"birth_year": "2022", "vaccine": MMR, "dose": "", "place_type": "States/Local Areas",
              "group_type": "Age", "group": "24 Months"}  # fmt: skip
    with pytest.raises(QueryRefused) as refused:
        eng.query_metric(DS, "vaccination_coverage", filters=pinned)
    assert refused.value.code == "would_combine_rates"
    one = eng.query_metric(DS, "vaccination_coverage", filters={**pinned, "place": "Michigan"})
    assert [r[0] for r in one["rows"]] == [93.4]


def test_the_generic_series_declines_a_vaccine_at_several_doses(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    ds = reg.get(DS)
    (generic,) = ds.condition_series("DTaP")  # doses not pinned: DTaP has three and four doses by 24 months
    with pytest.raises(QueryRefused) as refused:
        tools.series_rows(eng, ds, generic, ["Michigan"], 2022, 2022)
    assert refused.value.code == "would_combine_rates"


def test_a_disease_question_gets_coverage_only_when_it_asks_about_vaccination(
    built: tuple[Registry, Engine],
) -> None:
    # main's asked_with routing (#244): a coverage series answers only a question that asks about vaccination
    # and names the vaccine. A plain disease question, or a bare vaccination question, gets none of it; each row
    # says it is vaccine coverage, in percent, and measure="cases" leaves it out.
    reg, _eng = built
    assert not [ds for ds, _s in tools.series_for(reg, "influenza") if ds.id == DS]
    found = [(ds.id, s) for ds, s in tools.series_for(reg, "influenza vaccination coverage") if ds.id == DS]
    assert [(s.measure, s.label) for _id, s in found] == [
        ("percent", "Influenza vaccine coverage (dose as CDC defines it) by 24 months")
    ]
    assert not [
        ds
        for ds, _s in tools.series_for(reg, "influenza vaccination coverage", measure="cases")
        if ds.id == DS
    ]
    for bare in ("vaccination", "vaccination coverage", "childhood vaccination coverage"):
        assert not [ds for ds, _s in tools.series_for(reg, bare) if ds.id == DS], bare


def test_vaccine_label_rule_lists_the_catalog_labels() -> None:
    # The warning rule and the catalog (which builds flag_unmapped_labels) must name the same vaccines.
    ds = load_registry(ROOT).get(DS)
    (rule,) = [r for r in ds.contract.quality if r.id == "vaccine_labels"]
    assert sorted(rule.valid_values) == sorted(ds.entry.conditions.labels)


def _report(rows: list[dict[str, Any]]) -> dict[str, str]:
    ds = load_registry(ROOT).get(DS)
    con = duckdb.connect()
    cols = {c.name: c.physical_type for c in ds.contract.columns}
    con.execute(f"CREATE TABLE t ({', '.join(f'{n} {t}' for n, t in cols.items())})")
    for r in rows:
        full = {n: r.get(n) for n in cols}
        con.execute(f"INSERT INTO t VALUES ({', '.join('?' for _ in cols)})", list(full.values()))
    return {x["test"]: x["status"] for x in quality.run_rules(con, "t", ds.contract.quality).results}


GOOD = {"vaccine": MMR, "dose": None, "geography_type": "States/Local Areas", "geography": "Michigan",
        "year_season": "2022", "dimension_type": "Age", "dimension": "24 Months", "coverage_estimate": 93.4,
        "ci_95": "87.9 to 97.0", "population_sample_size": 300}  # fmt: skip


def test_quality_rules_pass_on_a_well_formed_row() -> None:
    assert set(_report([GOOD]).values()) == {"pass"}


@pytest.mark.parametrize(
    ("rule", "bad"),
    [
        ("coverage_is_a_percent", {"coverage_estimate": 101.0}),
        ("vaccine_labels", {"vaccine": "≥2 Dose MMR"}),
        ("geography_type_labels", {"geography_type": "Counties"}),
        ("dimension_type_labels", {"dimension_type": "Sex"}),
        ("interval_text_form", {"ci_95": "92.2-96.1"}),
        ("interval_text_form", {"ci_95": "92.2 – 96.1"}),
        # regexp_full_match doesn't trim: a padded interval is a change in form to review too.
        ("interval_text_form", {"ci_95": " 92.2 to 96.1"}),
    ],
)
def test_a_quality_rule_fires_on_a_bad_row(rule: str, bad: dict[str, Any]) -> None:
    status = _report([GOOD, {**GOOD, "geography": "Ohio", **bad}])
    assert status[rule] in ("fail", "warn")
    assert {k for k, v in status.items() if v != "pass"} == {rule}


def test_a_repeated_cell_fires_the_grain_check_with_no_dose_and_an_empty_dose_alike() -> None:
    status = _report([GOOD, {**GOOD, "dose": "", "coverage_estimate": 90.0}])
    assert status["grain_is_unique"] == "warn"


def test_an_estimate_without_an_interval_is_counted_beyond_the_known_25() -> None:
    gaps = [{**GOOD, "geography": f"Place {i}", "ci_95": "" if i % 2 else None} for i in range(26)]
    assert _report(gaps)["interval_filled_where_estimate"] == "warn"
    assert _report(gaps[:25])["interval_filled_where_estimate"] == "pass"
    assert (
        _report([{**GOOD, "coverage_estimate": None, "ci_95": None}])["interval_filled_where_estimate"]
        == "pass"
    )


def test_a_coverage_question_is_routed_here(built: tuple[Registry, Engine]) -> None:
    reg, eng = built
    question = "MMR vaccination coverage among young children in Michigan"
    out = tools.suggest_datasets(reg, question, engine=eng, fmt="json")
    assert {r["source_id"]: r["can_answer"] for r in _rows(out)}[DS] == "yes"
