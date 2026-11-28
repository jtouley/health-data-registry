"""Issue #173: numbers the served path returned wrong, each reproduced on synthetic rows shaped like NNDSS.

The real catalog, contracts and semantic models read hand-written rows in the publisher's layout (no real data,
hard rule 5). Every number is made up. Each test names the wrong number the server used to return:

1. A '-' (no reported cases) running total read as 0, and turned into a rate of 0.0.
2. Weekly counts added up into a year, which undercounts it (late reports land in the year-to-date count).
3. Areas added up with the US and region total rows still in them.
4. A rate per 100,000 people worked out from C. auris screening (colonisation) detections.
5. `refresh --all` exiting 0 when a load failed a blocking data check.

#186 fixed 2-4 by refusing. Under hard rule 2, "show everything; qualify, don't decide" (ADR 0018, issue #226),
each is now worked out when asked: the figure comes with a plain-words note citing the contract or semantic-model
rule behind it and, where there is one, the figure without that problem beside it. '-' is still never 0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import analysis, tools
from health_data_registry.query import Engine
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
    # Legionellosis in Michigan, weeks 1-20 of 2019-2024, with '-' in 2022 week 14: a historical 4-week period
    # with a week that has no number, for check_signal's second row.
    *(r for y in (2019, 2020, 2021, 2023, 2024) for r in weeks("MICHIGAN", LEGIO, y, [3] * 20)),
    *weeks("MICHIGAN", LEGIO, 2022, [3] * 13 + [None] + [3] * 6),
    # Animal rabies in Michigan, and Legionellosis in New York State (which excludes New York City).
    nndss("MICHIGAN", "Rabies, Animal", 2024, 52, m1="1", m3="30"),
    nndss("NEW YORK", LEGIO, 2024, 52, m1="1", m3="50"),
]

CENSUS_ROWS = (
    [census("New Jersey", "34", y, "900000") for y in (2021, 2022, 2023, 2024, 2025)]
    + [census("Michigan", "26", y, "1000000") for y in (2021, 2022, 2023, 2024)]
    + [census("Ohio", "39", 2024, "1200000"), census("New York", "36", 2024, "2000000")]
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


def method(out: Any) -> str:
    """The method note of a query_sql answer, which carries the guard's notes on its sums."""
    (line,) = [n for n in out["notes"] if n.startswith("Method: ")]
    return str(line)


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
    # Was a rate ratio of 0 against Michigan. Now no ratio: what each side holds comes back, with the next question.
    got = {r["what"]: r for r in rows(out)}
    nj = got["rate per 100,000: New Jersey 2024"]
    assert nj["estimate"] is None and nj["cases"] is None and "'-' = no reported cases" in nj["flags"]
    assert got["rate per 100,000: Michigan 2024"]["estimate"] == 2.1
    assert not any(k.startswith("rate ratio") for k in got)
    assert any(n.startswith("No rate ratio or difference is worked out") for n in out["notes"])
    assert any(n.startswith("Next question:") for n in out["notes"])


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
    # Weeks 13-15 have a number, week 16 is '-': the total adds the reported weeks only (was: refused).
    (row,) = rows(out)
    assert (
        row["current_4_week_total"] == 6
        and row["historical_mean"] == 8
        and row["signal"] == "below historical limits"
    )
    assert "reported weeks only" in row["periods_used"]
    (note,) = [n for n in out["notes"] if n.startswith("Some 4-week totals")]
    assert "2024 weeks 13-16: week 16 with no number (publisher flag '-' = no reported cases)" in note
    assert "never counted as 0" in note and "the contract's noCasesCode" in note


def test_query_sql_never_adds_a_dash_value_as_zero(served: tuple[Registry, Engine]) -> None:
    _reg, eng = served
    sql = (
        "SELECT sum(m3) AS cases FROM cdc_nndss_weekly WHERE label = 'Candida auris, clinical' AND year = 2024 "
        "AND week = 52 AND states IN ('NEW JERSEY', 'MICHIGAN')"
    )
    out = tools.query_sql(eng, sql, fmt="json")
    # The sum of the reported values (Michigan's 21), and how many had no number beside it (New Jersey's '-').
    assert rows(out) == [{"cases": 21, "cases_without_total_rows": 21, "cases_values_with_no_number": 1}]
    note = method(out).split("A count in cdc_nndss_weekly with no number")[1]
    assert "never counted as 0" in note and "reported values only" in note
    assert "'-'" in note and "(noCasesCode)" in note and "cases_values_with_no_number says" in note
    # The values themselves, with their flags, read as published.
    plain = rows(tools.query_sql(eng, sql.replace("sum(m3) AS cases", "states, m3, m3_flag"), fmt="json"))
    assert {(r["states"], r["m3"], r["m3_flag"]) for r in plain} == {("NEW JERSEY", None, "-"),
                                                                    ("MICHIGAN", 21, "-")}  # fmt: skip


# ------------------------------------------------------------------------------- 2. weeks never added into a year


def test_query_sql_refuses_adding_weeks_into_a_year(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    sql = "SELECT sum(m1) FROM cdc_nndss_weekly WHERE label = 'Legionellosis' AND year = 2024 AND states = 'OHIO'"
    out = tools.query_sql(eng, sql, fmt="json")
    # Worked out as asked (was refused): weeks 1-21 add up to 39, week 16's '-' not counted, with the note.
    assert rows(out) == [{"sum(m1)": 39, "sum_values_with_no_number": 1}]
    note = method(out).split("This adds up weekly counts")[1]
    assert "Weekly counts undercount the year" in note and "late" in note
    assert "semantic model for cdc_nndss_weekly: weeks are non-additive" in note
    assert "the year-to-date count (m3) at the year's last week" in note
    # The figure without that problem: the year-to-date count at the year's last published week, 51.
    got = rows(tools.get_series(reg, eng, condition=LEGIO, locations=["Ohio"], year_start=2024, year_end=2024,
                                fmt="json"))  # fmt: skip
    (own,) = [r for r in got if r["observation_type"] == "year_end_ytd"]
    assert own["value"] == 51
    sql = sql.replace("2024", "2023")
    # Running totals added across weeks count the same cases again and again: worked out, with that note.
    out = tools.query_sql(eng, sql.replace("sum(m1)", "sum(m3)"), fmt="json")
    assert rows(out)[0]["sum(m3)"] == sum(range(2, 41, 2))  # 2 + 4 + ... + 40
    assert "counted again in every later week" in method(out) and "non-additive" in method(out)
    # One week, or one row per week, is fine: nothing is added across weeks.
    one_week = sql.replace("sum(m1)", "sum(m1) AS n") + " AND week = 20"
    out = tools.query_sql(eng, one_week, fmt="json")
    assert rows(out) == [{"n": 2, "n_values_with_no_number": 0}]
    assert "This adds up" not in method(out)
    assert rows(
        tools.query_sql(eng, sql.replace("sum(m1)", "sum(m1) AS n") + " AND week IN (20)", fmt="json")
    ) == [{"n": 2, "n_values_with_no_number": 0}]
    by_week = rows(tools.query_sql(eng, sql.replace("sum(m1)", "week, sum(m1) AS n") + " GROUP BY week",
                                   fmt="json"))  # fmt: skip
    assert len(by_week) == 20


def test_the_weekly_metric_is_never_added_into_a_year(served: tuple[Registry, Engine]) -> None:
    _reg, eng = served
    out = eng.query_metric(NNDSS, "weekly_cases", filters={"condition": LEGIO, "reporting_area": "OHIO"},
                           group_by=["mmwr_year"])  # fmt: skip
    # Worked out as asked (was refused), with CDC's year-to-date count beside it: in 2024 the weeks add up to 39
    # (week 16's '-' not counted, and named), the running total at the year's last week to 51.
    got = {r["mmwr_year"]: r for r in rows(out)}
    assert got[2023] == {"mmwr_year": 2023, "weekly_cases": 40, "suppressed_rows": 0, "flag": None,
                         "year_to_date_at_last_week": 40}  # fmt: skip
    assert got[2024] == {"mmwr_year": 2024, "weekly_cases": 39, "suppressed_rows": 1, "flag": "-",
                         "year_to_date_at_last_week": 51}  # fmt: skip
    method = out["method"]
    assert "Weekly counts undercount the year" in method and "late" in method
    assert "semantic model for cdc_nndss_weekly: weeks are non-additive, because late reports" in method
    assert "year_to_date_at_last_week is CDC's year-to-date count at the last week asked for" in method
    assert "safer" not in method and "right" not in method
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
    """One week, or one row per week, adds nothing across weeks; several weeks are added with the note."""
    _reg, eng = served
    for metric in ("weekly_cases", "us_resident_weekly_cases"):
        f = {
            "condition": LEGIO,
            **({"reporting_area": "OHIO"} if metric == "weekly_cases" else {}),
            **filters,
        }
        out = eng.query_metric(NNDSS, metric, filters=f, group_by=group_by)
        assert out["rows"]
        assert ("year_to_date_at_last_week" in out["columns"]) is not allowed, metric
        assert ("Weekly counts undercount the year" in out["method"]) is not allowed, metric
    out = eng.query_metric(
        NNDSS, "us_resident_weekly_cases", filters={"condition": LEGIO}, group_by=["mmwr_year"]
    )
    # US RESIDENTS: 21 weeks of 9 = 189, and its own year-to-date count at week 21 (the weeks' running total).
    assert rows(out) == [{"mmwr_year": 2024, "us_resident_weekly_cases": 189, "suppressed_rows": 0, "flag": None,
                          "year_to_date_at_last_week": 189}]  # fmt: skip


# ------------------------------------------------------------------------------- 3. total rows never in a sum


def test_query_sql_leaves_total_rows_out_of_a_sum_across_areas(served: tuple[Registry, Engine]) -> None:
    _reg, eng = served
    sql = (
        "SELECT sum(m3) AS cases FROM cdc_nndss_weekly WHERE label = 'Candida auris, clinical' AND year = 2024 "
        "AND week = 52 AND states <> 'NEW JERSEY'"
    )
    out = tools.query_sql(eng, sql, fmt="json")
    # The sum asked for keeps the total rows (Michigan plus US RESIDENTS, a division and TOTAL: 922), with the note
    # and the sum without them beside it (was 21 alone, with the total rows left out).
    assert rows(out) == [{"cases": 922, "cases_without_total_rows": 21, "cases_values_with_no_number": 0}]
    note = method(out).split("This sum adds cdc_nndss_weekly's total rows")[1]
    assert "counted more than once" in note and "totalRows" in note
    assert "cases_without_total_rows beside it gives the same sum without them" in note
    grouped = tools.query_sql(eng, sql.replace("sum(m3) AS cases", "label, sum(m3) AS cases") + " GROUP BY label",
                              fmt="json")  # fmt: skip
    assert rows(grouped) == [{"label": CLIN, "cases": 922, "cases_without_total_rows": 21,
                              "cases_values_with_no_number": 0}]  # fmt: skip
    # A summary row asked for by name is read as published, and so is one row per area.
    us = "SELECT sum(m3) AS cases FROM cdc_nndss_weekly WHERE label = 'Candida auris, clinical' AND year = 2024 "
    us += "AND week = 52 AND states = 'US RESIDENTS'"
    out = tools.query_sql(eng, us, fmt="json")
    assert rows(out) == [{"cases": 400, "cases_values_with_no_number": 0}]
    assert "This sum adds" not in method(out)
    per_area = sql.replace("sum(m3) AS cases", "states, sum(m3) AS cases") + " GROUP BY states"
    assert {r["states"]: r["cases"] for r in rows(tools.query_sql(eng, per_area, fmt="json"))}["TOTAL"] == 401
    # Two different areas in IN, one of them a total row, cross areas: kept in, with the note and the figure without.
    two = sql.replace("states <> 'NEW JERSEY'", "states IN ('MICHIGAN', 'TOTAL')")
    out = tools.query_sql(eng, two, fmt="json")
    assert rows(out) == [{"cases": 422, "cases_without_total_rows": 21, "cases_values_with_no_number": 0}]
    # A window sum keeps the total rows too; it gets no column beside it, so the note says how to leave them out.
    listing = (
        "SELECT states, m3, sum(m3) OVER (PARTITION BY week) AS cases FROM cdc_nndss_weekly WHERE label = "
        "'Candida auris, clinical' AND year = 2024 AND week = 52 AND states <> 'NEW JERSEY'"
    )
    out = tools.query_sql(eng, listing, fmt="json")
    got = {r["states"]: r for r in rows(out)}
    assert set(got) == {"MICHIGAN", "US RESIDENTS", "EAST NORTH CENTRAL", "TOTAL"}
    assert {r["cases"] for r in got.values()} == {922}
    assert "add FILTER (WHERE NOT coalesce((" in method(out)


def test_query_sql_never_skips_an_empty_value_in_a_sum(served: tuple[Registry, Engine]) -> None:
    """An empty value is never skipped unseen: the sum adds the reported values and says how many had no number."""
    _reg, eng = served
    base = (
        "FROM cdc_nndss_weekly WHERE label = 'Candida auris, clinical' AND year = 2024 AND week = 52 "
        "AND states IN ('NEW JERSEY', 'MICHIGAN')"
    )
    # coalesce(m3, 0) is the old 0 reading by hand: the empty value is still counted beside the sum.
    got = rows(tools.query_sql(eng, f"SELECT sum(coalesce(m3, 0)) AS n {base}", fmt="json"))
    assert got == [{"n": 21, "n_without_total_rows": 21, "n_values_with_no_number": 1}]
    # Dropping the empty values first hides them from that count: answered, with a note saying so.
    out = tools.query_sql(eng, f"SELECT sum(m3) AS n {base} AND m3 IS NOT NULL", fmt="json")
    assert rows(out)[0]["n"] == 21 and rows(out)[0]["n_values_with_no_number"] == 0
    assert "WHERE tests the count it adds up (m3)" in method(out)
    # Counting the empty values tests them, it doesn't add them up: no count beside it.
    counted = rows(tools.query_sql(eng, f"SELECT sum(CASE WHEN m3 IS NULL THEN 1 ELSE 0 END) AS n {base}",
                                   fmt="json"))  # fmt: skip
    assert counted == [{"n": 1, "n_without_total_rows": 1}]


# ------------------------------------------------------------------------------- 4. no rate from screening


def test_get_rate_gives_no_rate_for_screening_detections(served: tuple[Registry, Engine]) -> None:
    """A screening rate is worked out, labelled 'screening detections, not incidence' (was refused), with the
    clinical cases' rate beside it."""
    reg, eng = served
    out = analysis.get_rate(reg, eng, condition=SCREEN, year=2024, locations=["Michigan"], fmt="json")
    got = {r["condition"]: r for r in rows(out)}
    screening = got["C. auris (screening, colonisation)"]
    assert screening["cases"] == 57 and screening["rate_per_100k"] == 5.7  # 57 / 1,000,000 x 100,000
    assert screening["lower_95"] is not None and screening["upper_95"] is not None
    assert screening["flags"].startswith("screening detections, not incidence: ")
    assert (
        "colonisation" in screening["flags"]
        and "the contract's notIncidence for cdc_nndss_weekly" in screening["flags"]
    )
    # Beside it, the counterpart the contract names (incidenceCounterpart), described by what it is.
    clinical = got["C. auris (clinical cases)"]
    assert clinical["rate_per_100k"] == 2.1 and "screening" not in clinical["flags"]
    assert any(
        "screening detections, not incidence" in n
        and "The figure for 'C. auris (clinical cases)' is beside it" in n
        for n in out["notes"]
    )
    assert not any("safer" in n or "right one" in n for n in out["notes"])
    # Asked as "C. auris", both series come back once each.
    both = rows(
        analysis.get_rate(reg, eng, condition="C. auris", year=2024, locations=["Michigan"], fmt="json")
    )
    assert sorted(r["condition"] for r in both) == [
        "C. auris (clinical cases)",
        "C. auris (screening, colonisation)",
    ]


def test_compare_rates_and_get_trend_give_no_screening_rate(served: tuple[Registry, Engine]) -> None:
    """compare_rates and get_trend work out screening rates (were refused), each with the note and its citation."""
    reg, eng = served
    out = analysis.compare_rates(reg, eng, condition=SCREEN, location="Michigan", year=2024, other_year=2023,
                                 fmt="json")  # fmt: skip
    got = {r["what"]: r for r in rows(out)}
    assert got["rate per 100,000: Michigan 2024"]["estimate"] == 5.7
    assert got["rate per 100,000: Michigan 2023"]["estimate"] == 4.0
    assert got["rate ratio (Michigan 2024 / Michigan 2023)"]["estimate"] == 1.425  # 57 / 40
    assert "screening detections, not incidence" in got["rate per 100,000: Michigan 2024"]["flags"]
    # The clinical cases' rate beside it, for the year the data has (Michigan 2024: 21 per 1,000,000).
    assert got["rate per 100,000 of C. auris (clinical cases): Michigan 2024"]["estimate"] == 2.1
    assert any("the contract's notIncidence for cdc_nndss_weekly" in n for n in out["notes"])
    out = analysis.get_trend(reg, eng, condition=SCREEN, location="Michigan", year_start=2021, year_end=2024,
                             include_provisional=True, fmt="json")  # fmt: skip
    (trend,) = [r for r in rows(out) if r["condition"] == "C. auris (screening, colonisation)"]
    assert trend["points_used"] == 4 and trend["fit_on"] == "rates (cases per Census population)"
    assert trend["annual_percent_change"] is not None
    assert any("screening detections, not incidence" in n and "notIncidence" in n for n in out["notes"])
    counts = analysis.get_trend(reg, eng, condition=SCREEN, location="Michigan", year_start=2021, year_end=2024,
                                include_provisional=True, fit_on="counts", fmt="json")  # fmt: skip
    assert rows(counts)[0]["points_used"] == 4  # a trend in the counts themselves is still given


def test_the_screening_declaration_comes_from_the_contract() -> None:
    reg = load_registry(ROOT)
    ds = reg.get(NNDSS)
    assert ds.not_incidence(SCREEN) and ds.not_incidence(CLIN) is None
    assert ds.contract.column("label").not_incidence == ((SCREEN, ds.not_incidence(SCREEN)),)
    assert ds.incidence_counterpart(SCREEN) == CLIN and ds.incidence_counterpart(CLIN) is None


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
        running=frozenset({"m3"}),
        within_reason="late reports reach only the year-to-date count",
        year_to_date="m3",
    )
    tables = {"cdc_nndss_weekly": frozenset({"states", "location2", "label", "year", "week", "m1", "m3"}),
              "census_county_pop": frozenset({"stname", "popestimate"})}  # fmt: skip
    return guard_sql(sql, allowed_tables=tables, sum_rules={"cdc_nndss_weekly": rule}, max_rows=10,
                     declared_joins=frozenset({frozenset(tables)}))  # fmt: skip


def test_the_guard_applies_each_sum_rule() -> None:
    """Nothing is refused: each rule becomes a note, and a sum in the outer select list gets its columns beside."""
    from health_data_registry.sql_guard import SqlRefused

    q = _guard("SELECT sum(m3) FROM cdc_nndss_weekly WHERE week = 52")
    assert '"sum_without_total_rows"' in q.sql and '"sum_values_with_no_number"' in q.sql
    assert "FILTER(WHERE NOT COALESCE((NOT cdc_nndss_weekly.location2 IS NULL), FALSE))" in q.sql
    assert "ERROR(" not in q.sql
    assert any("total rows" in n and "totalRows" in n for n in q.notes)
    assert any("never counted as 0" in n for n in q.notes)
    # One area, or one row per area (a window partition too): no total rows in the sum.
    assert not any(
        "total rows" in n
        for n in _guard("SELECT sum(m3) FROM cdc_nndss_weekly WHERE week = 52 AND states = 'TOTAL'").notes
    )
    window = _guard("SELECT states, sum(m1) OVER (PARTITION BY states, week) FROM cdc_nndss_weekly")
    assert "values_with_no_number" not in window.sql  # a window sum gets no column beside it, only the note
    assert any("count(*) FILTER (WHERE <the count> IS NULL) counts them" in n for n in window.notes)
    # Weeks neither pinned nor grouped: worked out, with the note citing the semantic model, under a window too.
    for sql, says in (("SELECT sum(m1) OVER (PARTITION BY states) FROM cdc_nndss_weekly", "undercount the year"),
                      ("SELECT n.states, sum(n.m3) FROM cdc_nndss_weekly AS n WHERE n.week IN (51, 52) GROUP BY 1",
                       "counted again in every later week")):  # fmt: skip
        notes = _guard(sql).notes
        assert any(says in n and "weeks are non-additive, because late reports" in n for n in notes), notes
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
    tested = _guard("SELECT sum(CASE WHEN m1 IS NULL THEN 1 ELSE 0 END) FROM cdc_nndss_weekly")
    assert not any("across weeks" in n for n in tested.notes)
    # In a join, only the ruled table's columns are counted, and its total rows are named only when a sum reads
    # it; a sum of the other table's column alone gets nothing beside it.
    joined = _guard("SELECT sum(m3), sum(popestimate) FROM cdc_nndss_weekly AS n JOIN census_county_pop AS c "
                    "ON n.states = c.stname WHERE n.week = 52")  # fmt: skip
    assert "n.location2" in joined.sql and "popestimate IS NULL" not in joined.sql
    other = _guard("SELECT sum(popestimate) FROM cdc_nndss_weekly AS n JOIN census_county_pop AS c "
                   "ON n.states = c.stname")  # fmt: skip
    assert not other.notes and "FILTER" not in other.sql


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
    """A caller's literal that quotes the old empty-value sentinel is an ordinary failed query, never read as a
    statement about empty values (the guard no longer stops a sum on one; ADR 0018)."""
    import duckdb

    from health_data_registry.sql_guard import GuardedQuery, SqlRefused, harden, run_guarded

    con = duckdb.connect()
    harden(con)
    spoof = GuardedQuery(sql="SELECT CAST('hdr-empty-value-in-sum' AS INTEGER)", tables=frozenset(), limit=1)
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


# ------------------------------------------------------------------------------- qualified answers (ADR 0018, #226)


def test_check_signal_puts_the_check_on_complete_periods_beside(served: tuple[Registry, Engine]) -> None:
    """A historical period with a '-' week adds its reported weeks only; the check on the complete periods only
    comes back beside it, described by what it is."""
    reg, eng = served
    out = analysis.check_signal(
        reg, eng, condition=LEGIO, location="Michigan", year=2024, week=16, fmt="json"
    )
    first, second = rows(out)
    assert first["current_4_week_total"] == 12 and first["historical_mean"] == 11.8  # (14 x 12 + 9) / 15
    assert (
        "15 historical 4-week totals" in first["periods_used"]
        and "reported weeks only" in first["periods_used"]
    )
    assert second["current_4_week_total"] == 12 and second["historical_mean"] == 12
    assert second["periods_used"] == "the 14 historical 4-week totals with a number for every week"
    (note,) = [n for n in out["notes"] if n.startswith("Some 4-week totals")]
    assert "2022 weeks 13-16: week 14 with no number (publisher flag '-' = no reported cases)" in note
    assert "The second row is the same check" in note and "noCasesCode" in note


def test_check_signal_refuses_only_when_no_current_week_has_a_number(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    """Even then, no bare refusal: the weekly counts held for those weeks, and the question to ask next."""
    out = analysis.check_signal(reg, eng, condition=LEGIO, location="Ohio", year=2025, week=16, fmt="json")
    assert "refused" not in out and rows(out) == []  # Ohio has no 2025 weeks in the data
    assert any("No signal is worked out" in n and "never counted as 0" in n for n in out["notes"])
    assert any(n.startswith("Next question:") for n in out["notes"])


def test_get_rate_labels_animal_counts(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    (row,) = rows(analysis.get_rate(reg, eng, condition="Rabies, Animal", year=2024, locations=["Michigan"],
                                    fmt="json"))  # fmt: skip
    assert row["cases"] == 30 and row["rate_per_100k"] == 3.0  # was refused: no rate for animal counts
    assert row["flags"].startswith("not a rate of disease in people: 'Rabies, Animal' counts rabid animals")
    assert "the catalog's not_a_human_case_count for cdc_nndss_weekly" in row["flags"]


def test_get_rate_labels_a_count_that_covers_part_of_a_place(served: tuple[Registry, Engine]) -> None:
    reg, eng = served
    (row,) = rows(analysis.get_rate(reg, eng, condition=LEGIO, year=2024, locations=["New York"], fmt="json"))
    assert row["cases"] == 50 and row["rate_per_100k"] == 2.5  # was no rate against New York's population
    assert "catchment area only: the count covers only part of New York" in row["flags"]
    assert "the contract's partialPlaces for cdc_nndss_weekly" in row["flags"]
    assert "overstates the denominator" in row["flags"]


def test_get_series_never_calls_either_figure_right(served: tuple[Registry, Engine]) -> None:
    """The notes on condition labels and mixed measures say what a sum of them is not, and never 'never add'."""
    reg, eng = served
    out = tools.get_series(reg, eng, condition="C. auris", locations=["Michigan"], year_start=2024, year_end=2024,
                           fmt="json")  # fmt: skip
    (note,) = [n for n in out["notes"] if "different condition labels" in n]
    assert "screening detections, not incidence" in note and "never add" not in note


def test_a_running_total_across_places_is_added_up_with_a_note(served: tuple[Registry, Engine]) -> None:
    """annual_cases read for several places without grouping by place: each place's year-end figure, added."""
    _reg, eng = served
    out = eng.query_metric(NNDSS, "annual_cases", filters={"condition": CLIN, "reporting_area": ["MICHIGAN", "NEW JERSEY"]},
                           group_by=["mmwr_year"], period={"start": 2024, "end": 2024})  # fmt: skip
    # Michigan's 21, New Jersey's '-' not counted and named (was refused: read once per place).
    assert rows(out) == [{"mmwr_year": 2024, "annual_cases": 21, "suppressed_rows": 1}]
    assert "Then added up across reporting_area" in out["method"]
    assert "suppressed_rows says how many had none" in out["method"]


def test_incidence_counterpart_names_a_declared_label_and_an_incidence_one(tmp_path: Path) -> None:
    import shutil

    from health_data_registry.registry import RegistryError

    real = (ROOT / "contracts" / "cdc_nndss_weekly.odcs.yaml").read_text()
    pair = '{"Candida auris, screening": "Candida auris, clinical"}'
    cases = (
        (pair.replace("Candida auris, screening", "Legionellosis"), "which notIncidence on the same column"),
        (pair.replace("Candida auris, clinical", "Measles, Other"), "must be a catalogued condition label"),
        (
            pair.replace("Candida auris, clinical", "Candida auris, screening"),
            "not itself declared notIncidence",
        ),
    )
    for n, (value, why) in enumerate(cases):
        contract = real.replace(pair, value, 1)
        assert contract != real
        root = tmp_path / str(n)
        for d, name in (("catalog", "cdc_nndss_weekly.yaml"), ("semantic", "cdc_nndss_weekly.yaml")):
            (root / d).mkdir(parents=True)
            shutil.copy(ROOT / d / name, root / d / name)
        (root / "contracts").mkdir()
        (root / "contracts" / "cdc_nndss_weekly.odcs.yaml").write_text(contract)
        with pytest.raises(RegistryError, match=why):
            load_registry(root)
