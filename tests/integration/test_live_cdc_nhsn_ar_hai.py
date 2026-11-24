"""Live contract test for cdc_nhsn_ar_hai (NHSN Antimicrobial Resistance, data.cdc.gov 7wz3-n6um)."""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "cdc_nhsn_ar_hai"
pytestmark = pytest.mark.network
MRSA = "Methicillin-resistant Staphylococcus aureus"


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    rows = _live.get_json(
        cat,
        _live.with_query(
            cat["source"]["api_url"],
            {
                "$query": (
                    f"SELECT * WHERE geography IN ('Michigan','United States') AND phenotype = '{MRSA}' AND year = 2023 "
                    "AND event_type = 'All Infectious Event Type Data' AND age_category = 'All Ages Data' "
                    "AND sex = 'All Sexes (total)' AND teaching_hospital = 'All Teaching Hospitals Data' "
                    "AND hospital_size = 'All Hospital Size Data' AND facility = 'All Facility Data' "
                    "AND procedure = 'All Procedure Data' AND location = 'All Location within Hospital Data' LIMIT 20"
                )
            },
        ),
    )
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


@pytest.mark.parametrize("geo", ["Michigan", "United States"])
def test_percent_resistant_overall_two_ways(live, geo):
    _cat, con, sem, rows, _ = live
    hits = [
        r for r in rows if r["geography"] == geo and not (geo == "United States" and r["source"] == "state")
    ]
    assert len(hits) == 1
    loop = float(hits[0]["percent_resistant"])
    sql = _live.duckdb_metric(
        con, sem, rows, "percent_resistant_overall", {"geography": geo, "year": 2023, "phenotype": MRSA}
    )
    assert sql == loop
    # The publisher's formula holds for the row it publishes (percent = resistant / tested x 100).
    r = hits[0]
    assert abs(loop - 100 * float(r["resistant_isolates"]) / float(r["isolates_tested"])) <= 0.06
