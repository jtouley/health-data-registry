"""Live contract test for cdc_haicviz_cdi (HAICViz - CDI, data.cdc.gov abgz-qs4g)."""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "cdc_haicviz_cdi"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    rows = _live.get_json(
        cat,
        _live.with_query(
            cat["source"]["api_url"],
            {"$query": ("SELECT * WHERE yearname IN (2019, 2021) AND viewby IN ('Total', 'Sex') LIMIT 200")},
        ),
    )
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


def _counts(rows, year, viewby, grouping):
    return [
        float(r["value"])
        for r in rows
        if r["yearname"] == str(year)
        and r["topic"].lower() == "number of cases"
        and r["viewby"] == viewby
        and r["grouping"].lower() == grouping
        and r.get("value")
    ]


def test_total_cases_two_ways(live):
    _cat, con, sem, rows, _ = live
    loop = sum(
        float(r["value"])
        for r in rows
        if r["yearname"] == "2019" and r["topic"].lower() == "number of cases" and r["series"] == "Total"
    )
    assert _live.duckdb_metric(con, sem, rows, "cdi_cases_total", {"yearname": 2019}) == loop


@pytest.mark.parametrize("year", [2019, 2021])  # 2021 uses the 'Epi class' spelling
def test_cases_by_epi_class_two_ways(live, year):
    _cat, con, sem, rows, _ = live
    loop = sum(_counts(rows, year, "Total", "epi class"))
    assert loop > 0
    assert _live.duckdb_metric(con, sem, rows, "cdi_cases_by_epi_class", {"yearname": year}) == loop


def test_cases_by_sex_two_ways(live):
    _cat, con, sem, rows, _ = live
    loop = sum(_counts(rows, 2019, "Sex", "all cases"))
    assert _live.duckdb_metric(con, sem, rows, "cdi_cases_by_sex", {"yearname": 2019}) == loop
