"""Issue #173: numbers the served path returned wrong, each reproduced on synthetic rows shaped like NNDSS.

The real catalog, contracts and semantic models read hand-written rows in the publisher's layout (no real data,
hard rule 5). Every number is made up. Each test names the wrong number the server used to return:

1. A '-' (no reported cases) running total read as 0, and turned into a rate of 0.0.
2. Weekly counts added up into a year, which undercounts it (late reports land in the year-to-date count).
3. Areas added up with the US and region total rows still in them.
4. A rate per 100,000 people worked out from C. auris screening (colonisation) detections.
5. `refresh --all` exiting 0 when a load failed a blocking data check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import analysis, tools
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch
from tests.smoke.fixtures import CENSUS, CENSUS_URL, NNDSS, NNDSS_URL, ROOT, nndss

CLIN = "Candida auris, clinical"
SCREEN = "Candida auris, screening"
LEGIO = "Legionellosis"


def census(state: str, fips: str, year: int, pop: str) -> dict[str, Any]:
    return {"SUMLEV": "040", "REGION": "1", "DIVISION": "2", "STATE": fips, "COUNTY": "000", "STNAME": state,
            "CTYNAME": state, "YEAR": str(year), "POPESTIMATE": pop}  # fmt: skip


def weeks(area: str, label: str, year: int, counts: list[int | None], *, summary: bool = False) -> list[Any]:
    """Weekly rows 1..n with a running total; None is a week the publisher left empty with '-'."""
    out, total = [], 0
    for w, c in enumerate(counts, start=1):
        total += c or 0
        out.append(
            nndss(
                area,
                label,
                year,
                w,
                m1=None if c is None else str(c),
                m3=str(total),
                flag="-",
                summary=summary,
            )
        )
    return out


NNDSS_ROWS: list[dict[str, Any]] = [
    # New Jersey 2024: '-' in every week, both the weekly count and the running total (no number at all).
    *(nndss("NEW JERSEY", CLIN, 2024, w, flag="-") for w in (50, 51, 52)),
    *(nndss("NEW JERSEY", CLIN, 2023, w, flag="-") for w in (50, 51, 52)),
    nndss("NEW JERSEY", CLIN, 2022, 52, m3="4"),
    nndss("New Jersey", CLIN, 2025, 52, m3="6"),
    nndss("NEW JERSEY", CLIN, 2021, 52, m3="5"),
    # Michigan 2024, clinical and screening, at year end.
    nndss("MICHIGAN", CLIN, 2024, 52, m1="2", m3="21"),
    nndss("MICHIGAN", SCREEN, 2024, 52, m1="3", m3="57"),
    nndss("MICHIGAN", SCREEN, 2023, 52, m1="3", m3="40"),
    nndss("MICHIGAN", SCREEN, 2022, 52, m1="3", m3="30"),
    nndss("MICHIGAN", SCREEN, 2021, 52, m1="3", m3="20"),
    # Total rows beside the states, for the same label and week.
    nndss("US RESIDENTS", CLIN, 2024, 52, m1="40", m3="400", summary=True),
    nndss("EAST NORTH CENTRAL", CLIN, 2024, 52, m1="10", m3="100", summary=True),
    nndss("TOTAL", CLIN, 2024, 52, m1="41", m3="401", summary=True),
    # Legionellosis in Ohio, weeks 1-20 of 2019-2024, for the weekly sums and the signal check. In 2024 week 16 is
    # '-' and week 21 is the year's last published week: its year-to-date count (51) is larger than the sum of the
    # year's weeks (39), because 12 late reports reached only the running total.
    *(r for y in range(2019, 2024) for r in weeks("OHIO", LEGIO, y, [2] * 20)),
    *weeks("OHIO", LEGIO, 2024, [2] * 15 + [None] + [2] * 4),
    nndss("OHIO", LEGIO, 2024, 21, m1="1", m3="51"),
    *weeks("US RESIDENTS", LEGIO, 2024, [9] * 21, summary=True),
]

CENSUS_ROWS = (
    [census("New Jersey", "34", y, "900000") for y in (2021, 2022, 2023, 2024, 2025)]
    + [census("Michigan", "26", y, "1000000") for y in (2021, 2022, 2023, 2024)]
    + [census("Ohio", "39", 2024, "1200000")]
)


def _lake(tmp_path: Path, reg: Registry, nndss_rows: list[dict[str, Any]]) -> Engine:
    lake = tmp_path / "lake"
    lake.mkdir()
    for ds_id, url, rows in ((NNDSS, NNDSS_URL, nndss_rows), (CENSUS, CENSUS_URL, CENSUS_ROWS)):
        f = tmp_path / f"{ds_id}.ndjson"
        write_ndjson(f, rows)
        build_snapshot(lake, reg.get(ds_id), [raw_fetch(url, rows)], [f])
    return Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    return reg, _lake(tmp_path_factory.mktemp("blockers"), reg, NNDSS_ROWS)


def rows(out: Any) -> list[dict[str, Any]]:
    assert isinstance(out, dict) and "rows" in out, out
    return [dict(zip(out["columns"], r, strict=True)) for r in out["rows"]]


def refused(out: Any) -> str:
    assert isinstance(out, dict) and "refused" in out and "rows" not in out, out
    assert out["next_step"], out  # every refusal says what to do next (hard rule 12)
    return str(out["refused"])


# ------------------------------------------------------------------------------- 1. '-' is never 0, never a rate


def test_a_dash_year_comes_back_as_not_reported_never_zero(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    got = rows(tools.get_series(reg, eng, condition=CLIN, locations=["New Jersey"], year_start=2024,
                                year_end=2024, fmt="json"))  # fmt: skip
    (own,) = [r for r in got if r["observation_type"] == "year_end_ytd"]
    assert own["value"] is None  # was 0
    assert own["flag"] == "-"
    assert "no reported cases" in own["period_status"] and "never counted as 0" in own["period_status"]


def test_get_rate_gives_no_rate_for_a_dash_year(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    (row,) = rows(
        analysis.get_rate(reg, eng, condition=CLIN, year=2024, locations=["New Jersey"], fmt="json")
    )
    assert row["cases"] is None and row["rate_per_100k"] is None  # was cases 0, rate 0.0
    assert row["lower_95"] is None and row["upper_95"] is None
    assert "no rate" in row["flags"] and "'-' = no reported cases" in row["flags"]


def test_compare_rates_refuses_a_dash_year(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    out = analysis.compare_rates(reg, eng, condition=CLIN, location="New Jersey", year=2024,
                                 other_location="Michigan", fmt="json")  # fmt: skip
    assert refused(out) == "no_rate"  # was a rate ratio of 0 against Michigan


def test_get_trend_leaves_dash_years_out(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    out = analysis.get_trend(reg, eng, condition=CLIN, location="New Jersey", year_start=2021, year_end=2025,
                             fit_on="counts", include_provisional=True, fmt="json")  # fmt: skip
    (row,) = rows(out)
    assert row["points_used"] == 3  # was 5: 2023 and 2024 fitted as zero cases
    assert "2023 not the year's total" in row["left_out"] and "2024 not the year's total" in row["left_out"]


def test_check_signal_never_counts_a_dash_week_as_zero(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    out = analysis.check_signal(reg, eng, condition=LEGIO, location="Ohio", year=2024, week=16, fmt="json")
    assert refused(out) == "incomplete_history"  # was a 4-week total of 6, with week 16 read as 0
    assert "'-' = no reported cases" in out["message"]


def test_query_sql_never_adds_a_dash_value_as_zero(served: tuple[Registry, Engine]) -> None:
    _reg, eng = served
    sql = (
        "SELECT sum(m3) AS cases FROM cdc_nndss_weekly WHERE label = 'Candida auris, clinical' AND year = 2024 "
        "AND week = 52 AND states IN ('NEW JERSEY', 'MICHIGAN')"
    )
    out = tools.query_sql(eng, sql, fmt="json")
    assert refused(out) == "empty_in_sum"  # was 21: New Jersey's '-' added as 0
    assert "never counted as 0" in out["message"]
    # The values themselves, with their flags, read as published.
    plain = rows(tools.query_sql(eng, sql.replace("sum(m3) AS cases", "states, m3, m3_flag"), fmt="json"))
    assert {(r["states"], r["m3"], r["m3_flag"]) for r in plain} == {("NEW JERSEY", None, "-"),
                                                                    ("MICHIGAN", 21, "-")}  # fmt: skip


# ------------------------------------------------------------------------------- 2. weeks never added into a year


def test_query_sql_refuses_adding_weeks_into_a_year(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    sql = "SELECT sum(m1) FROM cdc_nndss_weekly WHERE label = 'Legionellosis' AND year = 2024 AND states = 'OHIO'"
    out = tools.query_sql(eng, sql, fmt="json")
    assert refused(out) == "weekly_sum"  # weeks 1-21 summed to 39 (or stopped at week 16's '-')
    assert "late" in out["message"] and "year-to-date count at the year's last week" in out["next_step"]
    # The right answer is there: the year-to-date count at the year's last published week, 51.
    got = rows(tools.get_series(reg, eng, condition=LEGIO, locations=["Ohio"], year_start=2024, year_end=2024,
                                fmt="json"))  # fmt: skip
    (own,) = [r for r in got if r["observation_type"] == "year_end_ytd"]
    assert own["value"] == 51
    sql = sql.replace("2024", "2023")
    # Running totals added across weeks count the same cases again and again: refused too.
    out = tools.query_sql(eng, sql.replace("sum(m1)", "sum(m3)"), fmt="json")
    assert refused(out) == "weekly_sum"
    # One week, or one row per week, is fine: nothing is added across weeks.
    one_week = sql.replace("sum(m1)", "sum(m1) AS n") + " AND week = 20"
    assert rows(tools.query_sql(eng, one_week, fmt="json")) == [{"n": 2}]
    assert rows(
        tools.query_sql(eng, sql.replace("sum(m1)", "sum(m1) AS n") + " AND week IN (20)", fmt="json")
    ) == [{"n": 2}]
    by_week = rows(tools.query_sql(eng, sql.replace("sum(m1)", "week, sum(m1) AS n") + " GROUP BY week",
                                   fmt="json"))  # fmt: skip
    assert len(by_week) == 20


def test_the_weekly_metric_is_never_added_into_a_year(served: tuple[Registry, Engine]) -> None:
    _reg, eng = served
    with pytest.raises(QueryRefused) as e:
        eng.query_metric(NNDSS, "weekly_cases", filters={"condition": LEGIO, "reporting_area": "OHIO"},
                         group_by=["mmwr_year"])  # fmt: skip
    assert e.value.code == "weekly_sum"  # was 40 for each full year
    assert (
        "late" in str(e.value) and "last week" in e.value.next_step
    )  # the reason, then the action, once each
    assert "last week" not in str(e.value)
    # By week, as check_signal reads it, is unchanged.
    out = eng.query_metric(NNDSS, "weekly_cases", filters={"condition": LEGIO, "reporting_area": "OHIO",
                           "mmwr_year": 2023}, group_by=["mmwr_week"])  # fmt: skip
    assert len(out["rows"]) == 20


def test_a_year_with_one_dash_week_still_has_its_count_and_rate(served: tuple[Registry, Engine]) -> None:
    """Not over-refusing: Ohio 2024 has '-' in week 16, but its year-to-date count at week 21 has a number."""
    reg, eng = served
    got = rows(tools.get_series(reg, eng, condition=LEGIO, locations=["Ohio"], year_start=2024, year_end=2024,
                                fmt="json"))  # fmt: skip
    (own,) = [r for r in got if r["observation_type"] == "year_end_ytd"]
    assert own["value"] == 51 and not own["period_status"].startswith(("incomplete", "no number"))
    (rate,) = rows(analysis.get_rate(reg, eng, condition=LEGIO, year=2024, locations=["Ohio"], fmt="json"))
    assert rate["cases"] == 51 and rate["rate_per_100k"] == 4.25  # 51 / 1,200,000 x 100,000


@pytest.mark.parametrize(
    ("filters", "group_by", "allowed"),
    [
        ({"mmwr_year": 2024, "mmwr_week": [20]}, [], True),
        ({"mmwr_year": 2024, "mmwr_week": [19, 20]}, [], False),
        ({"mmwr_year": 2024}, ["mmwr_week"], True),
    ],
)
def test_the_weekly_rule_reads_one_week_however_it_is_pinned(
    served: tuple[Registry, Engine], filters: dict[str, Any], group_by: list[str], allowed: bool
) -> None:
    _reg, eng = served
    for metric in ("weekly_cases", "us_resident_weekly_cases"):
        f = {
            "condition": LEGIO,
            **({"reporting_area": "OHIO"} if metric == "weekly_cases" else {}),
            **filters,
        }
        if allowed:
            assert eng.query_metric(NNDSS, metric, filters=f, group_by=group_by)["rows"]
        else:
            with pytest.raises(QueryRefused) as e:
                eng.query_metric(NNDSS, metric, filters=f, group_by=group_by)
            assert e.value.code == "weekly_sum", metric
    with pytest.raises(QueryRefused) as e:
        eng.query_metric(
            NNDSS, "us_resident_weekly_cases", filters={"condition": LEGIO}, group_by=["mmwr_year"]
        )
    assert e.value.code == "weekly_sum"


# ------------------------------------------------------------------------------- 3. total rows never in a sum


def test_query_sql_leaves_total_rows_out_of_a_sum_across_areas(served: tuple[Registry, Engine]) -> None:
    _reg, eng = served
    sql = (
        "SELECT sum(m3) AS cases FROM cdc_nndss_weekly WHERE label = 'Candida auris, clinical' AND year = 2024 "
        "AND week = 52 AND states <> 'NEW JERSEY'"
    )
    out = tools.query_sql(eng, sql, fmt="json")
    assert rows(out) == [{"cases": 21}]  # was 922: Michigan plus US RESIDENTS, a division and TOTAL
    assert any("total rows" in n and "left out" in n for n in out["notes"]), out["notes"]
    grouped = tools.query_sql(eng, sql.replace("sum(m3) AS cases", "label, sum(m3) AS cases") + " GROUP BY label",
                              fmt="json")  # fmt: skip
    assert rows(grouped) == [{"label": CLIN, "cases": 21}]
    # A summary row asked for by name is read as published, and so is one row per area.
    us = "SELECT sum(m3) AS cases FROM cdc_nndss_weekly WHERE label = 'Candida auris, clinical' AND year = 2024 "
    us += "AND week = 52 AND states = 'US RESIDENTS'"
    assert rows(tools.query_sql(eng, us, fmt="json")) == [{"cases": 400}]
    per_area = sql.replace("sum(m3) AS cases", "states, sum(m3) AS cases") + " GROUP BY states"
    assert {r["states"]: r["cases"] for r in rows(tools.query_sql(eng, per_area, fmt="json"))}["TOTAL"] == 401
    # Two different areas in IN, one of them a total row, cross areas: the total row is left out, with the note.
    two = sql.replace("states <> 'NEW JERSEY'", "states IN ('MICHIGAN', 'TOTAL')")
    out = tools.query_sql(eng, two, fmt="json")
    assert rows(out) == [{"cases": 21}] and any("left out" in n for n in out["notes"])
    # A listing beside a window sum keeps every row it lists; only the sum leaves the total rows out.
    listing = (
        "SELECT states, m3, sum(m3) OVER (PARTITION BY week) AS cases FROM cdc_nndss_weekly WHERE label = "
        "'Candida auris, clinical' AND year = 2024 AND week = 52 AND states <> 'NEW JERSEY'"
    )
    got = {r["states"]: r for r in rows(tools.query_sql(eng, listing, fmt="json"))}
    assert set(got) == {"MICHIGAN", "US RESIDENTS", "EAST NORTH CENTRAL", "TOTAL"}
    assert {r["cases"] for r in got.values()} == {21}


def test_query_sql_never_skips_an_empty_value_in_a_sum(served: tuple[Registry, Engine]) -> None:
    _reg, eng = served
    base = (
        "FROM cdc_nndss_weekly WHERE label = 'Candida auris, clinical' AND year = 2024 AND week = 52 "
        "AND states IN ('NEW JERSEY', 'MICHIGAN')"
    )
    # coalesce(m3, 0) is the old 0 reading by hand: still stopped.
    assert refused(tools.query_sql(eng, f"SELECT sum(coalesce(m3, 0)) {base}", fmt="json")) == "empty_in_sum"
    # Dropping the empty values first would make a partial total look whole: refused.
    out = tools.query_sql(eng, f"SELECT sum(m3) {base} AND m3 IS NOT NULL", fmt="json")
    assert refused(out) == "empty_in_sum" and "filters on the count it adds up" in out["message"]
    # Counting the empty values tests them, it doesn't add them up: answered.
    counted = rows(tools.query_sql(eng, f"SELECT sum(CASE WHEN m3 IS NULL THEN 1 ELSE 0 END) AS n {base}",
                                   fmt="json"))  # fmt: skip
    assert counted == [{"n": 1}]


# ------------------------------------------------------------------------------- 4. no rate from screening


def test_get_rate_gives_no_rate_for_screening_detections(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    out = analysis.get_rate(reg, eng, condition=SCREEN, year=2024, locations=["Michigan"], fmt="json")
    assert refused(out) == "not_incidence"  # was 5.7 per 100,000
    assert "colonisation" in out["message"] and "not new cases" in out["message"]
    # Asked as "C. auris", the clinical series keeps its rate and the screening row says why it has none.
    both = {r["condition"]: r for r in rows(analysis.get_rate(reg, eng, condition="C. auris", year=2024,
                                                              locations=["Michigan"], fmt="json"))}  # fmt: skip
    assert both["C. auris (clinical cases)"]["rate_per_100k"] == 2.1
    screening = both["C. auris (screening, colonisation)"]
    assert screening["cases"] == 57 and screening["rate_per_100k"] is None
    assert "not new cases" in screening["flags"]


def test_compare_rates_and_get_trend_give_no_screening_rate(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    out = analysis.compare_rates(reg, eng, condition=SCREEN, location="Michigan", year=2024, other_year=2023,
                                 fmt="json")  # fmt: skip
    assert refused(out) == "not_incidence"
    out = analysis.get_trend(reg, eng, condition=SCREEN, location="Michigan", year_start=2021, year_end=2024,
                             include_provisional=True, fmt="json")  # fmt: skip
    assert refused(out) == "not_incidence"
    counts = analysis.get_trend(reg, eng, condition=SCREEN, location="Michigan", year_start=2021, year_end=2024,
                                include_provisional=True, fit_on="counts", fmt="json")  # fmt: skip
    assert rows(counts)[0]["points_used"] == 4  # a trend in the counts themselves is still given


def test_the_screening_declaration_comes_from_the_contract() -> None:
    reg = load_registry(ROOT)
    ds = reg.get(NNDSS)
    assert ds.not_incidence(SCREEN) and ds.not_incidence(CLIN) is None
    assert ds.contract.column("label").not_incidence == ((SCREEN, ds.not_incidence(SCREEN)),)


# ------------------------------------------------------------------------------- 5. refresh fails closed


def test_refresh_all_exits_non_zero_when_a_load_fails_a_blocking_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from health_data_registry import cli
    from tests.conftest import RECORDS
    from tests.test_publish import write_hub_registry
    from tests.test_quality import RULED

    root = write_hub_registry(tmp_path / "reg")
    (root / "contracts" / "toy_cases.odcs.yaml").write_text(RULED)  # an error-severity rule the rows fail
    monkeypatch.setenv("HDR_REGISTRY", str(root))
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "lake"))
    monkeypatch.setenv("HDR_HUB", "off")
    monkeypatch.setattr(cli, "_fetcher", lambda reg, log=None: lambda url: raw_fetch(url, RECORDS))
    assert cli.main(["refresh", "--all"]) == 1
    assert (
        "FAIL toy_cases: toy_cases: the error-severity check cases_under_5000 failed"
        in capsys.readouterr().err
    )
    # A development sample is kept for local use, but a failed blocking check still fails the command.
    assert cli.main(["refresh", "--all", "--max-rows", "5"]) == 1  # was 0
    err = capsys.readouterr().err
    assert "FAIL toy_cases: " in err and "cases_under_5000 failed" in err and "development sample" in err


# ------------------------------------------------------------------------------- the guard's sum rules, directly


def _guard(sql: str) -> Any:
    from health_data_registry.sql_guard import SumRules, guard_sql

    rule = SumRules(
        total_rows="location2 IS NOT NULL",
        areas=frozenset({"states", "location2"}),
        measures=frozenset({"m1", "m3"}),
        per_week=frozenset({"m1", "m3"}),
        within="week",
    )
    tables = {"cdc_nndss_weekly": frozenset({"states", "location2", "label", "year", "week", "m1", "m3"}),
              "census_county_pop": frozenset({"stname", "popestimate"})}  # fmt: skip
    return guard_sql(sql, allowed_tables=tables, sum_rules={"cdc_nndss_weekly": rule}, max_rows=10,
                     declared_joins=frozenset({frozenset(tables)}))  # fmt: skip


def test_the_guard_applies_each_sum_rule() -> None:
    from health_data_registry.sql_guard import SqlRefused

    q = _guard("SELECT sum(m3) FROM cdc_nndss_weekly WHERE week = 52")
    assert "FILTER(WHERE NOT COALESCE((NOT cdc_nndss_weekly.location2 IS NULL), FALSE))" in q.sql and q.notes
    assert "ERROR('hdr-empty-value-in-sum')" in q.sql
    # One area, or one row per area (a window partition too): nothing to leave out.
    assert not _guard("SELECT sum(m3) FROM cdc_nndss_weekly WHERE week = 52 AND states = 'TOTAL'").notes
    window = _guard("SELECT states, sum(m1) OVER (PARTITION BY states, week) FROM cdc_nndss_weekly")
    assert not window.notes and "ERROR(" in window.sql
    # Weeks are pinned or grouped, or the sum is refused, under a window too.
    for sql in ("SELECT sum(m1) OVER (PARTITION BY states) FROM cdc_nndss_weekly",
                "SELECT n.states, sum(n.m3) FROM cdc_nndss_weekly AS n WHERE n.week IN (51, 52) GROUP BY 1"):  # fmt: skip
        with pytest.raises(SqlRefused) as e:
            _guard(sql)
        assert e.value.code == "weekly_sum"
    # A sum over a WITH table built from a ruled table can't be checked, so it is refused.
    with pytest.raises(SqlRefused) as e:
        _guard("WITH w AS (SELECT * FROM cdc_nndss_weekly) SELECT sum(m3) FROM w")
    assert e.value.code == "sum_over_subquery"
    # A table with no sum rules (an empty SumRules) leaves a sum over a WITH table alone.
    from health_data_registry.sql_guard import SumRules, guard_sql

    plain = guard_sql("WITH w AS (SELECT n FROM t) SELECT sum(n) FROM w", allowed_tables={"t": frozenset({"n"})},
                      sum_rules={"t": SumRules()})  # fmt: skip
    assert "SUM(n)" in plain.sql and not SumRules()
    # A per-week column only tested inside a sum is not added up across weeks.
    assert _guard("SELECT sum(CASE WHEN m1 IS NULL THEN 1 ELSE 0 END) FROM cdc_nndss_weekly").sql
    # In a join, only the ruled table's columns are wrapped, and its total rows are left out only when a sum
    # reads it; a sum of the other table's column alone leaves the join's rows as they are.
    joined = _guard("SELECT sum(m3), sum(popestimate) FROM cdc_nndss_weekly AS n JOIN census_county_pop AS c "
                    "ON n.states = c.stname WHERE n.week = 52")  # fmt: skip
    assert "n.location2" in joined.sql and "popestimate IS NULL" not in joined.sql
    other = _guard("SELECT sum(popestimate) FROM cdc_nndss_weekly AS n JOIN census_county_pop AS c "
                   "ON n.states = c.stname")  # fmt: skip
    assert not other.notes and "ERROR(" not in other.sql


def test_not_incidence_is_declared_on_the_condition_column_for_catalogued_labels(tmp_path: Path) -> None:
    import shutil

    from health_data_registry.registry import RegistryError

    real = (ROOT / "contracts" / "cdc_nndss_weekly.odcs.yaml").read_text()
    on_m1 = real.replace("property: notIncidence\n", "property: notIncidenceDraft\n", 1).replace(
        "          - {property: sourceField, value: m1}\n",
        "          - {property: sourceField, value: m1}\n"
        '          - {property: notIncidence, value: {"Candida auris, screening": "colonisation"}}\n',
        1,
    )
    unknown = real.replace(
        '{"Candida auris, screening": "people found', '{"Measles, Other": "people found', 1
    )
    for contract, why in ((on_m1, "belongs on the condition column"), (unknown, "not in the catalog")):
        assert contract != real
        root = tmp_path / why.split()[0]
        for d, name in (("catalog", "cdc_nndss_weekly.yaml"), ("semantic", "cdc_nndss_weekly.yaml")):
            (root / d).mkdir(parents=True)
            shutil.copy(ROOT / d / name, root / d / name)
        (root / "contracts").mkdir()
        (root / "contracts" / "cdc_nndss_weekly.odcs.yaml").write_text(contract)
        with pytest.raises(RegistryError, match=why):
            load_registry(root)


def test_only_the_guards_own_error_is_read_as_an_empty_value() -> None:
    """A caller's literal that quotes the sentinel in some other engine error is not an empty_in_sum refusal."""
    import duckdb

    from health_data_registry.sql_guard import EMPTY_IN_SUM, GuardedQuery, SqlRefused, harden, run_guarded

    con = duckdb.connect()
    harden(con)
    spoof = GuardedQuery(sql=f"SELECT CAST('{EMPTY_IN_SUM}' AS INTEGER)", tables=frozenset(), limit=1)
    with pytest.raises(SqlRefused) as e:
        run_guarded(con, spoof)
    assert e.value.code != "empty_in_sum"


@pytest.mark.parametrize(
    ("quality", "says"),
    [
        ({"tool": "contract-rules", "results": [{"test": "r1", "status": "fail", "severity": "error"}]},
         "r1 failed"),
        ({"tool": "contract-rules", "rule_errors": [{"test": "r2", "error": "x", "severity": "error"}]},
         "r2 could not run"),
        ({}, "no data checks have run on this development sample"),
    ],
)  # fmt: skip
def test_a_blocked_sample_names_its_own_kind(quality: dict[str, Any], says: str) -> None:
    from health_data_registry.quality import sample_blocked_message

    msg = sample_blocked_message("toy", quality, expected=())
    assert msg and says in msg and "never published" in msg
    assert ("--max-rows rows" in msg) == (quality.get("tool") == "contract-rules")


def test_running_totals_along_a_dimension_make_it_non_additive() -> None:
    import yaml

    from health_data_registry.registry import SemanticModel

    doc = yaml.safe_load((ROOT / "semantic" / "cdc_nndss_weekly.yaml").read_text())
    week = next(d for d in doc["dimensions"] if d["name"] == "mmwr_week")
    week["non_additive"] = False
    del week["non_additive_reason"]
    with pytest.raises(ValueError, match="declare non_additive: true"):
        SemanticModel.model_validate(doc)
