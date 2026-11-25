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


def test_total_cases_two_ways(live):
    _cat, con, sem, rows, _ = live
    # The one published Total cell: grouping 'All cases', never the epi-class rows beside it.
    cells = [
        float(r["value"])
        for r in rows
        if r["yearname"] == "2019" and r["topic"].lower() == "number of cases" and r["viewby"] == "Total"
        and r["series"] == "Total" and r["grouping"].lower() == "all cases"
    ]  # fmt: skip
    assert len(cells) == 1
    assert _live.duckdb_metric(con, sem, rows, "cdi_cases_total", {"yearname": 2019}) == cells[0]


def _stratum(rows, year, viewby, grouping, series):
    return [
        float(r["value"])
        for r in rows
        if r["yearname"] == str(year)
        and r["topic"].lower() == "number of cases"
        and r["viewby"] == viewby
        and r["grouping"].lower() == grouping
        and r["series"] == series
        and r.get("value")
    ]


@pytest.mark.parametrize("year", [2019, 2021])  # 2021 uses the 'Epi class' spelling
@pytest.mark.parametrize("stratum", ["Healthcare-associated", "Community-associated"])
def test_cases_by_epi_class_one_stratum_two_ways(live, year, stratum):
    _cat, con, sem, rows, _ = live
    loop = _stratum(rows, year, "Total", "epi class", stratum)
    assert len(loop) == 1 and loop[0] > 0
    got = _live.duckdb_metric(con, sem, rows, "cdi_cases_by_epi_class", {"yearname": year, "series": stratum})
    assert got == loop[0]


def test_cases_by_epi_class_across_strata_is_refused(live):
    """HA + CA is not added: whether the strata add up to the Total is an open question in the contract."""
    _cat, con, sem, rows, _ = live
    with pytest.raises(_live.NonAdditive, match="stratum"):
        _live.duckdb_metric(con, sem, rows, "cdi_cases_by_epi_class", {"yearname": 2019})


def test_cases_by_sex_one_stratum_two_ways(live):
    _cat, con, sem, rows, _ = live
    sexes = sorted({r["series"] for r in rows if r["viewby"] == "Sex" and r["yearname"] == "2019"})
    assert sexes
    for sex in sexes:
        loop = _stratum(rows, 2019, "Sex", "all cases", sex)
        if not loop:
            continue
        got = _live.duckdb_metric(con, sem, rows, "cdi_cases_by_sex", {"yearname": 2019, "series": sex})
        assert got == sum(loop), sex
    with pytest.raises(_live.NonAdditive, match="stratum"):
        _live.duckdb_metric(con, sem, rows, "cdi_cases_by_sex", {"yearname": 2019})
