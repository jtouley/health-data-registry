"""Hard rule 2: a summed metric never adds up values of a dimension whose values overlap or are different measures,
and a percent or estimate never drops its published uncertainty silently.

Synthetic rows only, shaped like NNDSS's weekly table (the real registry: catalog/, contracts/, semantic/). One
place and week carries overlapping case-status labels for one condition (Confirmed + Probable = Total) and a
label that is not cases in people at all (Rabies, Animal). Adding the condition labels up would count the same
cases twice and mix animal rabies into human counts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import tools
from health_data_registry.query import Engine, QueryRefused, compile_metric, next_step_for
from health_data_registry.registry import (
    Registry,
    RegistryError,
    SemanticModel,
    filter_pins,
    load_registry,
    unpinned_non_additive,
)
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import SEMANTIC, raw_fetch, write_registry
from tests.test_real_contracts import nndss

ROOT = Path(__file__).resolve().parents[1]

ROWS = [
    nndss("KANSAS", "3", m3="30", label="Coccidioidomycosis, Confirmed"),
    nndss("OHIO", "4", m3="40", label="Coccidioidomycosis, Confirmed"),
    nndss("KANSAS", "2", m3="20", label="Coccidioidomycosis, Probable"),
    nndss("KANSAS", "5", m3="50", label="Coccidioidomycosis, Total"),
    nndss("KANSAS", "7", m3="70", label="Rabies, Animal"),
    # A summary row: the contract's totalRows drops it from every sum that excludes totals.
    nndss("US RESIDENTS", "99", summary=True, m3="990", label="Coccidioidomycosis, Confirmed"),
]
WEEK = {"mmwr_year": 2024, "mmwr_week": 52}


@pytest.fixture
def nndss_lake(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "n.ndjson"
    write_ndjson(f, ROWS)
    build_snapshot(
        lake, reg.get("cdc_nndss_weekly"), [raw_fetch("https://data.cdc.gov/resource/x.json", ROWS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


# ------------------------------------------------------------------------------------------ the bug


def test_weekly_cases_unpinned_condition_is_refused(nndss_lake: tuple[Registry, Engine]) -> None:
    """Before the fix this returned 3 + 4 + 2 + 5 + 7 = 21: overlapping labels and animal rabies added up."""
    reg, eng = nndss_lake
    ds = reg.get("cdc_nndss_weekly")
    with pytest.raises(QueryRefused) as e:
        compile_metric(ds, "weekly_cases", filters=WEEK)
    assert e.value.code == "non_additive"
    assert "condition (Nationally notifiable condition" in str(e.value)
    assert "Choose one value of condition, or show each value on its own row." in str(e.value)
    assert e.value.next_step.startswith(
        "Use describe_dataset on cdc_nndss_weekly to see the values of condition"
    )
    for jargon in ("Traceback", "Error", "SQL", "non_additive"):
        assert jargon not in str(e.value) and jargon not in e.value.next_step
    with pytest.raises(QueryRefused, match="adds up counts"):
        eng.query_metric("cdc_nndss_weekly", "weekly_cases", filters=WEEK)
    # Two labels in one filter are still two labels added together.
    two = {**WEEK, "condition": ["Coccidioidomycosis, Confirmed", "Coccidioidomycosis, Probable"]}
    with pytest.raises(QueryRefused, match="adds up counts"):
        compile_metric(ds, "weekly_cases", filters=two)
    # Grouping by place alone still adds every label within each place.
    with pytest.raises(QueryRefused, match="adds up counts"):
        compile_metric(ds, "weekly_cases", filters=WEEK, group_by=["reporting_area"])


def test_refusal_next_step_is_registered() -> None:
    assert next_step_for("non_additive").startswith("Use describe_dataset")


# ------------------------------------------------------------------------------------------ allowed paths


def test_pinned_condition_sums_places_and_excludes_totals(nndss_lake: tuple[Registry, Engine]) -> None:
    _, eng = nndss_lake
    one = {**WEEK, "condition": "Coccidioidomycosis, Confirmed"}
    out = eng.query_metric("cdc_nndss_weekly", "weekly_cases", filters=one)
    assert [r[:2] for r in out["rows"]] == [[7, 0]]  # Kansas 3 + Ohio 4; US RESIDENTS (99) never added
    # A one-item list pins the condition just as a single value does.
    listed = {**WEEK, "condition": ["Coccidioidomycosis, Confirmed"]}
    assert eng.query_metric("cdc_nndss_weekly", "weekly_cases", filters=listed)["rows"] == out["rows"]


def test_grouped_by_condition_keeps_every_label_apart(nndss_lake: tuple[Registry, Engine]) -> None:
    _, eng = nndss_lake
    out = eng.query_metric("cdc_nndss_weekly", "weekly_cases", filters=WEEK, group_by=["condition"])
    assert [r[:2] for r in out["rows"]] == [
        ["Coccidioidomycosis, Confirmed", 7],
        ["Coccidioidomycosis, Probable", 2],
        ["Coccidioidomycosis, Total", 5],  # the publisher's own total, never Confirmed + Probable re-added
        ["Rabies, Animal", 7],
    ]


def test_tools_pin_one_label_per_series(nndss_lake: tuple[Registry, Engine]) -> None:
    """get_series and the generic series bind the condition to one label each, so the tools still answer."""
    reg, eng = nndss_lake
    ds = reg.get("cdc_nndss_weekly")
    for label in ("Coccidioidomycosis, Confirmed", "Rabies, Animal"):
        assert all(s.filters["condition"] == label for s in ds.condition_series(label))
    got = []
    for label in (
        "Coccidioidomycosis, Confirmed",
        "Coccidioidomycosis, Probable",
        "Coccidioidomycosis, Total",
    ):
        out: Any = tools.get_series(reg, eng, condition=label, locations=["Kansas"],
                                    source_id="cdc_nndss_weekly", fmt="json")  # fmt: skip
        got += [(r[9], r[2]) for r in out["rows"] if r[5] == "year_end_ytd"]
    assert got == [
        ("Coccidioidomycosis, Confirmed", 30),
        ("Coccidioidomycosis, Probable", 20),
        ("Coccidioidomycosis, Total", 50),
    ]


def test_metric_filters_pin_their_own_dimensions() -> None:
    reg = load_registry(ROOT)
    cdi = reg.get("cdc_haicviz_cdi")
    # topic, view and grouping are fixed by the metric's reviewed filters; stratum must still be chosen.
    with pytest.raises(QueryRefused, match="Choose one value of stratum, or") as e:
        compile_metric(cdi, "cdi_cases_by_epi_class", filters={"year": 2019})
    assert "things: stratum (" in str(e.value)  # only stratum: the filters fix topic, view and grouping
    by_stratum = compile_metric(cdi, "cdi_cases_by_epi_class", filters={"year": 2019}, group_by=["stratum"])
    assert "sum(" in by_stratum.sql
    # The total row is one published cell, read as published.
    assert "sum(" not in compile_metric(cdi, "cdi_cases_total", group_by=["year"]).sql
    census = reg.get("census_county_pop")
    assert "sum(" in compile_metric(census, "national_population", group_by=["year"]).sql


def test_rates_and_percents_are_never_summed_whatever_is_pinned() -> None:
    reg = load_registry(ROOT)
    resp = reg.get("cdc_respnet")
    cm = compile_metric(resp, "weekly_hospitalization_rate", group_by=["week_ending"])
    assert "sum(" not in cm.sql and "not summed" in cm.method
    assert unpinned_non_additive(resp.semantic, resp.semantic.metric("weekly_hospitalization_rate"), []) == []


# ------------------------------------------------------------------------------------------ filter_pins


@pytest.mark.parametrize(
    ("predicate", "pins"),
    [
        ("lower(topic) = 'number of cases'", {"topic"}),
        ("'Total' = viewby", {"viewby"}),
        ("trim(upper(series)) = 'TOTAL'", {"series"}),
        ("series IN ('Total')", {"series"}),
        ("topic = 'a' AND (viewby = 'Total')", {"topic", "viewby"}),
        ("series IN ('Total', 'All')", set()),
        ("topic = 'a' OR viewby = 'b'", set()),
        ("NOT (topic = 'a')", set()),
        ("topic <> 'a'", set()),
        ("topic = viewby", set()),
        ("starts_with(topic, 'a')", set()),
    ],
)
def test_filter_pins(predicate: str, pins: set[str]) -> None:
    sem = load_registry(ROOT).get("cdc_haicviz_cdi").semantic
    columns = {d.column: d.name for d in sem.dimensions}
    assert filter_pins([predicate], sem.dimensions) == {columns[c] for c in pins}


# ------------------------------------------------------------------------------------------ the validators


def test_real_models_declare_every_dimension_where_they_sum() -> None:
    for ds in load_registry(ROOT):
        if any(m.aggregation == "sum" for m in ds.semantic.metrics):
            assert all(d.non_additive is not None for d in ds.semantic.dimensions), ds.id
    nndss_dims = {
        d.name: d.non_additive for d in load_registry(ROOT).get("cdc_nndss_weekly").semantic.dimensions
    }
    assert nndss_dims["condition"] is True and nndss_dims["reporting_area"] is False
    reg = load_registry(ROOT)
    assert reg.get("cdc_nhsn_ar_hai").semantic.dimension("phenotype").non_additive is True
    assert reg.get("cdc_respnet").semantic.dimension("virus_network").non_additive is True


def test_real_percents_and_estimates_say_where_their_uncertainty_is() -> None:
    for ds in load_registry(ROOT):
        for m in ds.semantic.metrics:
            if ds.semantic.measure(m.measure).kind in ("estimate", "percent"):
                assert m.uncertainty is not None, (ds.id, m.name)
                if m.uncertainty == "none":
                    assert (m.uncertainty_reason or "").strip(), (ds.id, m.name)
    icu = load_registry(ROOT).get("cdc_respnet")
    cm = compile_metric(icu, "percent_admitted_to_icu", group_by=["season"])
    assert "No uncertainty bounds: the publisher's table has no interval column" in cm.method


PCT = SEMANTIC.replace(
    "  - {name: rate, column: rate_per_100k, kind: rate, unit: /100000, description: rate per 100k}\n",
    "  - {name: rate, column: rate_per_100k, kind: rate, unit: /100000, description: rate per 100k}\n"
    "  - {name: pct, column: rate_per_100k, kind: percent}\n"
    "  - {name: pct_lo, column: rate_per_100k, kind: lower_bound}\n"
    "  - {name: pct_hi, column: rate_per_100k, kind: upper_bound}\n",
).replace("series:\n", "  - {name: share, description: Share, measure: pct, aggregation: none, exclude_total_rows: false"
          "__UNC__}\nseries:\n")  # fmt: skip


@pytest.mark.parametrize(
    ("semantic", "expect"),
    [
        (SEMANTIC.replace(", non_additive: false, description: MMWR year", ", description: MMWR year"),
         "every dimension must say non_additive: true .* or false; not declared on: year"),
        (SEMANTIC + "  - condition: all\n    label: All conditions\n    metric: case_count\n    measure: cases\n"
         "    location_dimension: state\n    time_dimension: year\n", "series All conditions adds up case_count "
         "across condition"),
        (PCT.replace("__UNC__", ""), "reads a percent: name its uncertainty bound measures"),
        (PCT.replace("__UNC__", ", uncertainty: none"), "uncertainty: none needs an uncertainty_reason"),
        (PCT.replace("__UNC__", ", uncertainty: none, uncertainty_reason: ' '"), "needs an uncertainty_reason"),
        (PCT.replace("__UNC__", ", uncertainty: {lower: pct_lo, upper: pct_hi}, uncertainty_reason: x"),
         "uncertainty_reason only goes with uncertainty: none"),
        (PCT.replace("__UNC__", ", uncertainty: {lower: pct_hi, upper: pct_hi}"), "pct_hi is a upper_bound, not a "
         "lower_bound"),
        (PCT.replace("__UNC__", ", uncertainty: {lower: pct_lo, upper: pct_lo}"), "pct_lo is a lower_bound, not a "
         "upper_bound"),
        (PCT.replace("__UNC__", ", uncertainty: {lower: nope, upper: pct_hi}"), "names unknown measure nope"),
        (PCT.replace("__UNC__", ", uncertainty: maybe"), "uncertainty"),
    ],
)  # fmt: skip
def test_validators_refuse_a_model_that_forgets(tmp_path: Path, semantic: str, expect: str) -> None:
    with pytest.raises((RegistryError, ValueError), match=expect):
        load_registry(write_registry(tmp_path / "r", semantic=semantic))


@pytest.mark.parametrize(
    "unc",
    [
        ", uncertainty: {lower: pct_lo, upper: pct_hi}",
        ", uncertainty: none, uncertainty_reason: none published",
    ],
)
def test_validators_accept_a_declared_uncertainty(tmp_path: Path, unc: str) -> None:
    reg = load_registry(write_registry(tmp_path / "r", semantic=PCT.replace("__UNC__", unc)))
    m = reg.get("toy_cases").semantic.metric("share")
    assert m.uncertainty is not None


def test_a_model_without_a_sum_need_not_declare() -> None:
    sem = SemanticModel.model_validate(
        {
            "dataset": "x",
            "version": "1",
            "dimensions": [{"name": "y", "column": "y", "type": "time"}],
            "measures": [{"name": "r", "column": "r", "kind": "rate"}],
            "metrics": [{"name": "m", "description": "d", "measure": "r", "aggregation": "none"}],
        }
    )
    assert sem.dimensions[0].non_additive is None
