"""Hard rule 2: a summed metric never adds up values of a dimension whose values overlap or are different measures,
a published cell that is empty is never a silent blank, and a percent, estimate or rate never drops its published
uncertainty silently.

Synthetic rows only, shaped like NNDSS's weekly table and HAICViz's CDI table (the real registry: catalog/,
contracts/, semantic/). One NNDSS place and week carries overlapping case-status labels for one condition
(Confirmed + Probable = Total) and a label that is not cases in people at all (Rabies, Animal). Adding the
condition labels up would count the same cases twice and mix animal rabies into human counts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from pydantic import ValidationError

from health_data_registry import tools
from health_data_registry.query import (
    Engine,
    QueryRefused,
    compile_metric,
    next_step_for,
    non_additive_columns,
)
from health_data_registry.registry import (
    Registry,
    RegistryError,
    SemanticModel,
    Series,
    filter_pins,
    is_single_value,
    load_registry,
    unpinned_non_additive,
)
from health_data_registry.sql_guard import SqlRefused, guard_sql
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
    # Another week and condition, with a flag code: a full load has some, so the codebook rule has a value to judge.
    nndss("DISTRICT OF COLUMBIA", None, flag="-", week=51),
]
WEEK = {"mmwr_year": 2024, "mmwr_week": 52}
JARGON = ("Traceback", "Error", "SQL", "non_additive", "weekly_cases", "reporting_area", "stratum", "mmwr")


def _lake(tmp_path: Path, dataset: str, rows: list[dict[str, Any]]) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "rows.ndjson"
    write_ndjson(f, rows)
    build_snapshot(lake, reg.get(dataset), [raw_fetch("https://data.cdc.gov/resource/x.json", rows)], [f])
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


@pytest.fixture
def nndss_lake(tmp_path: Path) -> tuple[Registry, Engine]:
    return _lake(tmp_path, "cdc_nndss_weekly", ROWS)


def cdi(
    year: int, viewby: str, grouping: str, series: str, value: str | None, topic: str = "Number of Cases"
) -> dict:
    return {"yearname": str(year), "topic": topic, "viewby": viewby, "grouping": grouping, "series": series,
            "value": value}  # fmt: skip


# The shape of the live table (checked 2026-11-25): the Total cell sits under 'All cases', the epi-class rows beside
# it under 'Epi Class' or 'Epi class'. 2021's Total cell is empty here, to stand for a suppressed or unpublished cell.
CDI_ROWS = [
    cdi(2019, "Total", "All cases", "Total", "14612"),
    cdi(2019, "Total", "Epi Class", "Healthcare-associated", "6985"),
    cdi(2019, "Total", "Epi Class", "Community-associated", "7627"),
    cdi(2020, "Total", "All cases", "Total", "12260"),
    cdi(2020, "Total", "Epi class", "Healthcare-associated", "6062"),
    cdi(2020, "Total", "Epi class", "Community-associated", "6198"),
    cdi(2021, "Total", "All cases", "Total", None),
    cdi(2021, "Total", "Epi class", "Healthcare-associated", "6579"),
    cdi(2019, "Sex", "All cases", "All cases - Female", "8000"),
    cdi(2019, "Sex", "All cases", "All cases - Male", "6612"),
    cdi(2019, "Total", "All cases", "Total", "121.2", topic="Case rates (cases per 100,000)"),
]


@pytest.fixture
def cdi_lake(tmp_path: Path) -> tuple[Registry, Engine]:
    return _lake(tmp_path, "cdc_haicviz_cdi", CDI_ROWS)


# ------------------------------------------------------------------------------------------ the bug


def test_weekly_cases_unpinned_condition_is_refused(nndss_lake: tuple[Registry, Engine]) -> None:
    """Before the fix this returned 3 + 4 + 2 + 5 + 7 = 21: overlapping labels and animal rabies added up."""
    reg, eng = nndss_lake
    ds = reg.get("cdc_nndss_weekly")
    with pytest.raises(QueryRefused) as e:
        compile_metric(ds, "weekly_cases", filters=WEEK)
    assert e.value.code == "non_additive"
    assert str(e.value) == (
        "These counts would be added up across groups that overlap or count different things: Nationally "
        "notifiable condition, e.g. Legionellosis or Candida auris, clinical (the labels overlap, as Confirmed + "
        "Probable = Total, and some are not cases in people, such as animal rabies). Adding them together would "
        "count the same cases twice or mix different things into one number, so nothing is added. Ask for one of "
        "them, or ask to see each one on its own row."
    )
    assert e.value.next_step == (
        "Use describe_dataset on cdc_nndss_weekly to see the values you can choose, then ask again for one of "
        "them, or ask to see each one on its own row."
    )
    for jargon in JARGON:  # hard rule 12: no internal metric or dimension names
        assert jargon not in str(e.value) and jargon not in e.value.next_step
    with pytest.raises(QueryRefused, match="would be added up across groups"):
        eng.query_metric("cdc_nndss_weekly", "weekly_cases", filters=WEEK)
    # Two labels in one filter are still two labels added together.
    two = {**WEEK, "condition": ["Coccidioidomycosis, Confirmed", "Coccidioidomycosis, Probable"]}
    with pytest.raises(QueryRefused, match="would be added up across groups"):
        compile_metric(ds, "weekly_cases", filters=two)
    # Grouping by place alone still adds every label within each place.
    with pytest.raises(QueryRefused, match="would be added up across groups"):
        compile_metric(ds, "weekly_cases", filters=WEEK, group_by=["reporting_area"])


def test_refusal_naming_several_groups_asks_for_one_value_of_each(tmp_path: Path) -> None:
    two = SEMANTIC.replace(
        "{name: state, column: reporting_area, type: categorical, non_additive: false, description: Reporting area}",
        "{name: state, column: reporting_area, type: categorical, non_additive: true, non_additive_reason: areas "
        "overlap, description: Reporting area}",
    ).replace("    location_dimension: state\n", "    location_dimension: state\n", 1)
    reg = load_registry(write_registry(tmp_path / "r", semantic=two))
    with pytest.raises(QueryRefused) as e:
        compile_metric(reg.get("toy_cases"), "case_count", group_by=["year"])
    assert "Reporting area (areas overlap); Condition (each is a different condition)" in str(e.value)
    assert (
        "Ask for one value of each" in str(e.value) and "ask again for one value of each" in e.value.next_step
    )
    with pytest.raises(QueryRefused) as e:
        compile_metric(reg.get("toy_cases"), "case_count", filters={"state": "OHIO"}, group_by=["year"])
    assert "Ask for one of them" in str(e.value)


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
    cdi_ds = reg.get("cdc_haicviz_cdi")
    # topic, view and grouping are fixed by the metric's reviewed filters; stratum must still be chosen.
    with pytest.raises(QueryRefused, match="The patient group the figure is for") as e:
        compile_metric(cdi_ds, "cdi_cases_by_epi_class", filters={"year": 2019})
    assert "What the figure measures" not in str(
        e.value
    )  # only stratum: the filters fix topic, view, grouping
    by_stratum = compile_metric(
        cdi_ds, "cdi_cases_by_epi_class", filters={"year": 2019}, group_by=["stratum"]
    )
    assert "sum(" in by_stratum.sql
    # The total row is one published cell, read as published.
    assert "sum(" not in compile_metric(cdi_ds, "cdi_cases_total", group_by=["year"]).sql
    census = reg.get("census_county_pop")
    assert "sum(" in compile_metric(census, "national_population", group_by=["year"]).sql


def test_rates_and_percents_are_never_summed_whatever_is_pinned() -> None:
    reg = load_registry(ROOT)
    resp = reg.get("cdc_respnet")
    cm = compile_metric(resp, "weekly_hospitalization_rate", group_by=["week_ending"])
    assert "sum(" not in cm.sql and "not summed" in cm.method
    assert unpinned_non_additive(resp.semantic, resp.semantic.metric("weekly_hospitalization_rate"), []) == []


# ------------------------------------------------------------------------------------------ CDI on rows


def test_cdi_total_reads_the_one_published_cell(cdi_lake: tuple[Registry, Engine]) -> None:
    _, eng = cdi_lake
    out = eng.query_metric("cdc_haicviz_cdi", "cdi_cases_total", group_by=["year"])
    # The published Total, not HA + CA; 2020's epi-class rows ('Epi class') are never read as a second Total.
    assert out["rows"] == [[2019, 14612.0, False], [2020, 12260.0, False], [2021, None, True]]


def test_cdi_empty_total_says_suppressed_or_not_published(cdi_lake: tuple[Registry, Engine]) -> None:
    reg, eng = cdi_lake
    out: Any = tools.get_series(reg, eng, condition="Clostridioides difficile infection", measure="cases",
                                source_id="cdc_haicviz_cdi", fmt="json")  # fmt: skip
    by_year = {r[1]: (r[2], r[6]) for r in out["rows"]}
    assert by_year[2021] == (None, "suppressed or not published: the publisher's cell for this row is empty")
    assert by_year[2019] == (14612.0, "the publisher marks no provisional or final status for this row")


def test_cdi_second_total_row_is_refused_in_plain_words(tmp_path: Path) -> None:
    _, eng = _lake(tmp_path, "cdc_haicviz_cdi", [*CDI_ROWS, cdi(2019, "Total", "all cases", "Total", "1")])
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("cdc_haicviz_cdi", "cdi_cases_total", group_by=["year"])
    assert e.value.code == "would_combine_rates"
    assert "Rates and estimates" not in str(e.value) and "cdi_cases_total" not in str(e.value)
    assert str(e.value).startswith("More than one published figure matches")


def test_cdi_epi_class_counts_each_stratum_with_total_excluded(cdi_lake: tuple[Registry, Engine]) -> None:
    _, eng = cdi_lake
    out = eng.query_metric("cdc_haicviz_cdi", "cdi_cases_by_epi_class", group_by=["year", "stratum"])
    assert [r[:3] for r in out["rows"]] == [
        [2019, "Community-associated", 7627],
        [2019, "Healthcare-associated", 6985],
        [2020, "Community-associated", 6198],  # 'Epi class' spelling read too
        [2020, "Healthcare-associated", 6062],
        [2021, "Healthcare-associated", 6579],
    ]
    sex = eng.query_metric(
        "cdc_haicviz_cdi", "cdi_cases_by_sex", filters={"year": 2019}, group_by=["stratum"]
    )
    assert [r[:2] for r in sex["rows"]] == [["All cases - Female", 8000], ["All cases - Male", 6612]]


def test_cdi_rate_says_no_interval() -> None:
    cm = compile_metric(load_registry(ROOT).get("cdc_haicviz_cdi"), "cdi_incidence_rate", group_by=["year"])
    assert cm.method.endswith(
        "No uncertainty bounds: CDC's table gives no confidence interval for these rates."
    )


# ------------------------------------------------------------------------------------------ series_rows statuses


class _Fake:
    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.out = {"columns": columns, "rows": rows, "method": "m"}

    def query_metric(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        return self.out


def test_a_missing_interval_is_said_per_row() -> None:
    ds = load_registry(ROOT).get("cdc_nhsn_ar_hai")
    s = Series(condition="x", label="x", metric="percent_resistant_overall", measure="percent",
               time_dimension="year", place_coverage="the sites")  # fmt: skip
    fake = _Fake(["year", "v", "lower", "upper", "flag", "not_published"],
                 [[2020, 12.0, 10.0, 14.0, None, False], [2021, 12.0, None, None, None, False],
                  [2022, None, None, None, True, True]])  # fmt: skip
    rows, _, _ = tools.series_rows(fake, ds, s, [], None, None)  # type: ignore[arg-type]
    assert [r.status for r in rows] == [
        "the publisher marks no provisional or final status for this row",
        "the publisher marks no provisional or final status for this row; the publisher gives no uncertainty "
        "interval for this row",
        "suppressed or not published: the publisher's cell for this row is empty",
    ]
    assert rows[2].flag is True


# ------------------------------------------------------------------------------------------ filter_pins


@pytest.mark.parametrize(
    ("predicate", "pins"),
    [
        ("lower(topic) = 'number of cases'", {"topic"}),
        ("'Total' = viewby", {"viewby"}),
        ("trim(upper(series)) = 'TOTAL'", {"series"}),
        ("series IN ('Total')", {"series"}),
        ("topic = 'a' AND (viewby = 'Total')", {"topic", "viewby"}),
        # One label spelled two ways (case and dots) is still one value.
        ("upper(grouping) IN ('Epi Class', 'EPI CLASS', 'Epi. class')", {"grouping"}),
        ("series IN ('Total', 'All')", set()),
        ("series IN ('Total', viewby)", set()),
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


@pytest.mark.parametrize(
    ("value", "single"),
    [
        ("a", True),
        (3, True),
        (["a"], True),
        (("a",), True),
        (["a", "b"], False),
        ([], False),
        (None, False),
    ],  # fmt: skip
)
def test_is_single_value(value: Any, single: bool) -> None:
    assert is_single_value(value) is single


# ------------------------------------------------------------------------------------------ query_sql


TABLES = {"t": frozenset({"label", "states", "m1", "year"}), "u": frozenset({"label", "m1"})}
LOOSE = {"t": frozenset({"label"})}


def _guard(sql: str) -> str:
    joins = frozenset({frozenset({"t", "u"})})
    return guard_sql(sql, allowed_tables=TABLES, declared_joins=joins, non_additive=LOOSE).sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sum(m1) FROM t WHERE label = 'Legionellosis'",
        "SELECT sum(m1) FROM t WHERE lower(label) = 'legionellosis' AND year = 2024",
        "SELECT sum(m1) FROM t WHERE upper(label) IN ('US RESIDENTS', 'U.S. RESIDENTS')",
        "SELECT label, sum(m1) FROM t GROUP BY label",
        "SELECT label AS l, sum(m1) FROM t GROUP BY l",
        "SELECT label, sum(m1) FROM t GROUP BY 1",
        "SELECT label, sum(m1) FROM t GROUP BY ALL",
        "SELECT x.label, sum(x.m1) FROM t AS x GROUP BY x.label",
        "SELECT sum(x.m1) FROM t AS x WHERE x.label = 'a'",
        "SELECT sum(t.m1) FROM t JOIN u ON t.m1 = u.m1 WHERE t.label = 'a'",
        "SELECT label, sum(m1) OVER (PARTITION BY label) FROM t",
        "SELECT count(*), avg(m1), max(m1) FROM t",  # nothing added up
        "SELECT sum(m1) FROM u",  # no column of u is marked
    ],
)
def test_query_sql_allows_a_pinned_or_grouped_sum(sql: str) -> None:
    assert _guard(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sum(m1) FROM t WHERE year = 2024",
        "SELECT sum(m1) FROM t WHERE label IN ('A', 'B')",
        "SELECT sum(m1) FROM t WHERE label = 'A' OR year = 1",
        "SELECT year, sum(m1) FROM t GROUP BY year",
        "SELECT fsum(m1) FROM t",
        "SELECT kahan_sum(m1) FROM t",
        "SELECT sum(m1) OVER (PARTITION BY year) FROM t",
        "SELECT sum(t.m1) FROM t JOIN u ON t.m1 = u.m1 WHERE u.label = 'a'",
        "SELECT (SELECT sum(m1) FROM t) AS s",
    ],
)
def test_query_sql_refuses_an_unpinned_sum(sql: str) -> None:
    with pytest.raises(SqlRefused) as e:
        _guard(sql)
    assert e.value.code == "non_additive"
    assert "Filter each of them to one value in WHERE, or add it to GROUP BY" in e.value.message


@pytest.mark.parametrize(
    "sql",
    [
        "WITH w AS (SELECT label, m1 FROM t WHERE label = 'a') SELECT sum(m1) FROM w",
        "SELECT sum(m1) FROM (SELECT m1 FROM t) AS d",
    ],
)
def test_query_sql_refuses_a_sum_it_cant_trace(sql: str) -> None:
    with pytest.raises(SqlRefused, match="Add up directly from the catalogued table instead") as e:
        _guard(sql)
    assert e.value.code == "non_additive"


def test_query_sql_without_flags_is_unchanged() -> None:
    assert guard_sql("SELECT sum(m1) FROM t", allowed_tables=TABLES).sql


def test_engine_query_sql_uses_the_models_flags(nndss_lake: tuple[Registry, Engine]) -> None:
    reg, eng = nndss_lake
    with pytest.raises(SqlRefused, match="across label"):
        eng.query_sql("SELECT sum(m1) FROM cdc_nndss_weekly WHERE year = 2024 AND week = 52")
    out = eng.query_sql(
        "SELECT label, sum(m1) FROM cdc_nndss_weekly WHERE year = 2024 AND week = 52 AND location2 IS NULL "
        "GROUP BY label ORDER BY label"
    )
    assert out["rows"][0] == ["Coccidioidomycosis, Confirmed", 7]
    # Declared columns, and every categorical column a model leaves undeclared (fail closed).
    assert non_additive_columns(reg.get("cdc_nndss_weekly")) == frozenset({"label"})
    assert non_additive_columns(reg.get("cdc_respnet")) >= {"surveillance_network", "date"}
    assert "season" not in non_additive_columns(reg.get("cdc_respnet"))
    assert non_additive_columns(reg.get("who_gho_tb_incidence")) == frozenset(
        {"spatial_dim", "spatial_dim_type"}
    )


# ------------------------------------------------------------------------------------------ the validators


def test_real_models_declare_every_dimension_where_they_sum_or_declare() -> None:
    for ds in load_registry(ROOT):
        dims = ds.semantic.dimensions
        if any(m.aggregation == "sum" for m in ds.semantic.metrics) or any(
            d.non_additive is not None for d in dims
        ):
            assert all(d.non_additive is not None for d in dims), ds.id
        for d in dims:  # every flag says why, in words a clinician reads in the refusal
            assert bool(d.non_additive) == bool((d.non_additive_reason or "").strip()), (ds.id, d.name)
    reg = load_registry(ROOT)
    nndss_dims = {d.name: d.non_additive for d in reg.get("cdc_nndss_weekly").semantic.dimensions}
    assert nndss_dims["condition"] is True and nndss_dims["reporting_area"] is False
    assert reg.get("cdc_nhsn_ar_hai").semantic.dimension("phenotype").non_additive is True
    assert reg.get("cdc_respnet").semantic.dimension("virus_network").non_additive is True


def test_real_percents_estimates_and_rates_say_where_their_uncertainty_is() -> None:
    for ds in load_registry(ROOT):
        for m in ds.semantic.metrics:
            if ds.semantic.measure(m.measure).kind in ("estimate", "percent", "rate"):
                assert m.uncertainty is not None, (ds.id, m.name)
                if m.uncertainty == "none":
                    reason = (m.uncertainty_reason or "").strip()
                    assert reason, (ds.id, m.name)
                    assert "contract" not in reason and "open question" not in reason, (ds.id, m.name)
    icu = load_registry(ROOT).get("cdc_respnet")
    cm = compile_metric(icu, "percent_admitted_to_icu", group_by=["season"])
    assert (
        "No uncertainty bounds: CDC's RESP-NET table gives no confidence interval for these percents"
        in cm.method
    )


def test_changed_models_carry_a_new_version() -> None:
    """Provenance reports the semantic model's version (ADR 0003): answers from before and after this change differ."""
    reg = load_registry(ROOT)
    for ds_id in ("cdc_haicviz_cdi", "cdc_nndss_weekly", "cdc_respnet"):
        assert reg.get(ds_id).semantic.version == "0.2.0", ds_id
    for ds_id in ("detroit_dhd_epi_annual", "detroit_dhd_resp_ed_weekly"):
        assert reg.get(ds_id).semantic.version == "0.1.1", ds_id


PCT = SEMANTIC.replace(
    "  - {name: rate, column: rate_per_100k, kind: rate, unit: /100000, description: rate per 100k}\n",
    "  - {name: rate, column: rate_per_100k, kind: rate, unit: /100000, description: rate per 100k}\n"
    "  - {name: pct, column: rate_per_100k, kind: percent}\n"
    "__BOUNDS__",
).replace("series:\n", "  - {name: share, description: Share, measure: pct, aggregation: none, exclude_total_rows: false"
          "__UNC__}\nseries:\n")  # fmt: skip
BOUNDS = (
    "  - {name: pct_lo, column: rate_per_100k, kind: lower_bound}\n"
    "  - {name: pct_hi, column: rate_per_100k, kind: upper_bound}\n"
)


def pct(unc: str, bounds: str = BOUNDS) -> str:
    return PCT.replace("__BOUNDS__", bounds).replace("__UNC__", unc)


RATE_NONE = ', uncertainty: none, uncertainty_reason: "The toy table gives no interval."'


MODEL, CROSS = "model", "cross-check"


@pytest.mark.parametrize(
    ("semantic", "error", "expect"),
    [
        (SEMANTIC.replace(", non_additive: false, description: MMWR year", ", description: MMWR year"),
         MODEL, "every dimension must say non_additive: true .* or false; not declared on: year"),
        (SEMANTIC + "  - condition: all\n    label: All conditions\n    metric: case_count\n    measure: cases\n"
         "    location_dimension: state\n    time_dimension: year\n", CROSS,
         "series All conditions adds up case_count across condition"),
        # A series that pins the condition to two labels still adds them together.
        (SEMANTIC + "  - condition: two\n    label: Two\n    metric: case_count\n    measure: cases\n"
         "    filters: {condition: [A, B]}\n    location_dimension: state\n    time_dimension: year\n",
         CROSS, "series Two adds up case_count across condition"),
        (pct(""), MODEL, "reads a percent: name its uncertainty bound measures"),
        (pct(", uncertainty: none"), MODEL, "uncertainty: none needs an uncertainty_reason"),
        (pct(", uncertainty: none, uncertainty_reason: ' '", ""), MODEL, "needs an uncertainty_reason"),
        (pct(", uncertainty: {lower: pct_lo, upper: pct_hi}, uncertainty_reason: x"), MODEL,
         "uncertainty_reason only goes with uncertainty: none"),
        (pct(", uncertainty: {lower: pct_hi, upper: pct_hi}"), MODEL,
         "pct_hi is a upper_bound, not a lower_bound"),
        (pct(", uncertainty: {lower: pct_lo, upper: pct_lo}"), MODEL,
         "pct_lo is a lower_bound, not a upper_bound"),
        (pct(", uncertainty: {lower: nope, upper: pct_hi}"), MODEL, "names unknown measure nope"),
        (pct(", uncertainty: maybe"), MODEL, "Input should be 'none'"),
        # A bound in other units than its percent, or a bound no metric returns, is a model error.
        (pct(", uncertainty: {lower: pct_lo, upper: pct_hi}", BOUNDS.replace("kind: upper_bound", "kind: upper_bound, "
                                                                              "unit: '%'")),
         MODEL, r"uncertainty pct_hi is in '%', but pct is in ''"),
        (pct(", uncertainty: none, uncertainty_reason: none published"), MODEL,
         "bound measure pct_lo is named by no metric's uncertainty"),
        # Rates name their interval too.
        (SEMANTIC.replace(RATE_NONE, ""), MODEL, "reads a rate: name its uncertainty bound measures"),
        # A flag says why, and only a true flag has a reason.
        (SEMANTIC.replace(', non_additive_reason: "each is a different condition"', ""), MODEL,
         "dimension condition: non_additive: true needs a non_additive_reason"),
        (SEMANTIC.replace("non_additive: false, description: MMWR year",
                          "non_additive: false, non_additive_reason: x, description: MMWR year"),
         MODEL, "dimension year: non_additive_reason only goes with non_additive: true"),
        # A summed metric that keeps the total rows may not add up a dimension declared additive.
        (SEMANTIC.replace("aggregation: sum, exclude_total_rows: true}", "aggregation: sum, exclude_total_rows: false}"),
         CROSS, "metric case_count keeps the publisher's total rows .* but adds up state"),
    ],
)  # fmt: skip
def test_validators_refuse_a_model_that_forgets(
    tmp_path: Path, semantic: str, error: str, expect: str
) -> None:
    with pytest.raises(RegistryError, match=expect) as e:
        load_registry(write_registry(tmp_path / "r", semantic=semantic))
    # A model error (the semantic model alone) wraps pydantic's ValidationError; a cross-check against the contract
    # or catalog is raised by the loader itself. Both are load errors, never data failures.
    if error == MODEL:
        assert isinstance(e.value.__cause__, ValidationError)
    else:
        assert e.value.__cause__ is None


@pytest.mark.parametrize(
    ("unc", "bounds"),
    [
        (", uncertainty: {lower: pct_lo, upper: pct_hi}", BOUNDS),
        (", uncertainty: none, uncertainty_reason: none published", ""),
    ],
)
def test_validators_accept_a_declared_uncertainty(tmp_path: Path, unc: str, bounds: str) -> None:
    reg = load_registry(write_registry(tmp_path / "r", semantic=pct(unc, bounds)))
    m = reg.get("toy_cases").semantic.metric("share")
    assert m.uncertainty is not None


def test_totals_rule_accepts_a_metric_that_says_which_side_it_reads(tmp_path: Path) -> None:
    # Pinned to one area by the metric's own filter: it reads one side of the total-row split.
    pinned = SEMANTIC.replace(
        "aggregation: sum, exclude_total_rows: true}",
        "aggregation: sum, exclude_total_rows: false, filters: [\"reporting_area = 'US RESIDENTS'\"]}",
    )
    reg = load_registry(write_registry(tmp_path / "r", semantic=pinned))
    assert reg.get("toy_cases").semantic.metric("case_count").exclude_total_rows is False
    # Census pins the level the totalRows predicate tests (sumlev = '040') and adds the states up.
    census = load_registry(ROOT).get("census_county_pop").semantic
    assert census.metric("national_population").exclude_total_rows is False
    assert census.dimension("state").non_additive is False


def test_a_model_without_a_sum_or_a_flag_need_not_declare() -> None:
    sem = SemanticModel.model_validate(
        {
            "dataset": "x",
            "version": "1",
            "dimensions": [{"name": "y", "column": "y", "type": "time"}],
            "measures": [{"name": "r", "column": "r", "kind": "rate"}],
            "metrics": [{"name": "m", "description": "d", "measure": "r", "aggregation": "none",
                         "uncertainty": "none", "uncertainty_reason": "none published"}],
        }
    )  # fmt: skip
    assert sem.dimensions[0].non_additive is None


def test_a_model_that_declares_one_flag_declares_every_one() -> None:
    with pytest.raises(ValidationError, match="the model declares non_additive, so every dimension"):
        SemanticModel.model_validate(
            {
                "dataset": "x",
                "version": "1",
                "dimensions": [
                    {"name": "y", "column": "y", "type": "time", "non_additive": False},
                    {"name": "p", "column": "p", "type": "categorical"},
                ],
                "measures": [{"name": "r", "column": "r", "kind": "count"}],
                "metrics": [{"name": "m", "description": "d", "measure": "r", "aggregation": "none"}],
            }
        )


# ------------------------------------------------------------------------------------------ load-time series check


def _series_model(series: str, generic: str = "") -> str:
    """The toy model with a week dimension whose values overlap, and only the series given."""
    week = SEMANTIC.replace(
        "  - {name: year, column: year, type: time, non_additive: false, description: MMWR year}\n",
        "  - {name: year, column: year, type: time, non_additive: false, description: MMWR year}\n"
        "  - {name: week, column: year, type: time, non_additive: true, non_additive_reason: weeks overlap, "
        "description: Week}\n",
    )
    return week[: week.index("series:\n")] + ("series:\n" + series if series else "series: []\n") + generic


GROUPED_BY_WEEK = (
    "  - condition: wk\n    label: Weekly\n    metric: case_count\n    measure: cases\n"
    "    filters: {condition: A}\n    location_dimension: state\n    time_dimension: year\n    week_dimension: week\n"
)


def test_grouping_by_the_week_dimension_holds_it(tmp_path: Path) -> None:
    reg = load_registry(write_registry(tmp_path / "r", semantic=_series_model(GROUPED_BY_WEEK)))
    assert "week" in reg.get("toy_cases").semantic.series[-1].held_dimensions()


def test_a_series_that_neither_groups_nor_pins_the_week_is_refused(tmp_path: Path) -> None:
    no_week = GROUPED_BY_WEEK.replace("    week_dimension: week\n", "")
    with pytest.raises(RegistryError, match="series Weekly adds up case_count across week"):
        load_registry(write_registry(tmp_path / "r", semantic=_series_model(no_week)))


def test_age_dimension_counts_as_grouped() -> None:
    s = Series(condition="c", label="l", metric="m", measure="cases", time_dimension="year",
               age_dimension="age", filters={"condition": ["A"], "sex": ["F", "M"]})  # fmt: skip
    assert s.held_dimensions() == ["condition", "year", "age"]


def test_generic_series_must_pin_every_other_non_additive_dimension(tmp_path: Path) -> None:
    generic = "generic_series:\n  - {metric: case_count, measure: cases, location_dimension: state, time_dimension: year}\n"
    catalog_dim = "conditions:\n  dimension: condition\n  source: toy codebook\n  labels: [Legionellosis]\n"
    from tests.conftest import CATALOG

    catalog = CATALOG + catalog_dim if "conditions:" not in CATALOG else CATALOG
    reg = load_registry(write_registry(tmp_path / "ok", catalog=catalog, semantic=_series_model("", generic)
                                       .replace("week, column: year, type: time, non_additive: true, "
                                                "non_additive_reason: weeks overlap",
                                                "week, column: year, type: time, non_additive: false")))  # fmt: skip
    assert reg.get("toy_cases").semantic.generic_series
    with pytest.raises(
        RegistryError, match=r"series \(any catalogued condition\) adds up case_count across week"
    ):
        load_registry(write_registry(tmp_path / "bad", catalog=catalog, semantic=_series_model("", generic)))


# ------------------------------------------------------------------------------------------ NHSN data checks


def _nhsn_rules() -> dict[str, str]:
    doc = yaml.safe_load((ROOT / "contracts" / "cdc_nhsn_ar_hai.odcs.yaml").read_text(encoding="utf-8"))
    rules: dict[str, str] = {}
    for prop in doc["schema"][0]["properties"]:
        for q in prop.get("quality") or []:
            rules[q["id"]] = q["query"]
    return rules


@pytest.mark.parametrize(
    ("rows", "failing"),
    [
        ([(12.0, 10.0, 14.0), (None, None, None)], set()),
        # An all-empty bound column passes every value check; the fill rule catches it.
        ([(12.0, None, None), (30.0, None, None)], {"interval_filled_where_percent"}),
        ([(12.0, 10.0, None)], {"interval_filled_where_percent"}),
        ([(None, 1.0, 2.0)], {"interval_only_with_percent"}),
    ],
)
def test_nhsn_interval_is_filled_wherever_the_percent_is(rows: list[tuple], failing: set[str]) -> None:
    rules = {k: v for k, v in _nhsn_rules().items() if k.startswith("interval_")}
    assert set(rules) == {"interval_filled_where_percent", "interval_only_with_percent"}
    db = duckdb.connect()
    db.execute("CREATE TABLE t (percent_resistant DOUBLE, lower_confidence_interval DOUBLE, "
               "upper_confidence_interval DOUBLE)")  # fmt: skip
    db.executemany("INSERT INTO t VALUES (?, ?, ?)", rows)
    got = {k for k, q in rules.items() if db.execute(q.replace("{object}", "t")).fetchone()[0] != 0}
    assert got == failing
