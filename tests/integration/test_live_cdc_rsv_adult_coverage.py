"""Live contract test for cdc_rsv_adult_coverage (data.cdc.gov qeq7-f3ir).

Socrata leaves a null field out of a row, and CDC suppresses small-sample estimates, so every optional value is read
with .get() and an empty one is skipped, as the contract's own rules skip it.
"""

from __future__ import annotations

import pytest

from tests.integration import _live

DS = "cdc_rsv_adult_coverage"
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    rows = _live.get_json(
        cat,
        _live.with_query(
            cat["source"]["api_url"],
            {
                "$query": "SELECT * WHERE geography_name IN ('National', 'Region 5') ORDER BY week_ending DESC LIMIT 500"
            },
        ),
    )
    return cat, con, sem, rows, meta


def _number(r: dict, field: str) -> float | None:
    v = r.get(field)
    return None if v in (None, "") else float(v)


def _distinct(cat: dict, field: str) -> set:
    query = {"$query": f"SELECT {field} GROUP BY {field} LIMIT 100"}
    return {r.get(field) for r in _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))}


def test_contract_structure(live):
    _cat, con, _sem, rows, meta = live
    _live.check_structure(con, rows, [c["fieldName"] for c in meta["columns"]])


def test_the_metadata_still_states_the_facts_the_contract_quotes(live):
    _cat, _con, _sem, _rows, meta = live
    desc = {c["fieldName"]: " ".join(str(c.get("description") or "").split()) for c in meta["columns"]}
    assert "Estimate (%)" in desc["estimate"]
    assert "95% Confidence Interval Half Width" in desc["ci_half_width_95pct"]
    assert "(National, Regional, State)" in desc["geography_level"]
    notes = " ".join(str(meta["metadata"]["custom_fields"]["Data Quality"]["Footnotes"]).split())
    assert "suppressed when the cumulative sample size is <30" in notes
    assert "NIS-FRVM" in str(meta.get("description"))


def test_labels_are_the_ones_the_contract_lists(live):
    cat, _con, _sem, _rows, _ = live
    assert _distinct(cat, "vaccine") <= set(cat["conditions"]["labels"])
    assert _distinct(cat, "indicator_label") <= {"Up-to-date", "4-level vaccination and intent"}


def test_the_grain_is_unique(live):
    cat, _con, _sem, _rows, _ = live
    key = "age_group, geography_name, demographic_level, demographic_name, indicator_label, indicator_category_label, week_ending"
    query = {"$query": f"SELECT {key}, count(*) AS n GROUP BY {key} HAVING count(*) > 1 LIMIT 5"}
    assert _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query)) == []


def test_codebook_facts_hold_on_the_latest_rows(live):
    _cat, _con, _sem, rows, _ = live
    for r in rows:
        est, hw = _number(r, "estimate"), _number(r, "ci_half_width_95pct")
        if est is not None:
            assert 0 <= est <= 100, r
            assert hw is not None and hw >= 0, r
        if r.get("suppression_flag") == "1":
            assert est is None, r


def test_latest_coverage_two_ways(live):
    _cat, con, sem, rows, _ = live
    want = [
        r
        for r in rows
        if r.get("age_group") == "75+ years"
        and r.get("geography_name") == "National"
        and r.get("demographic_level") == "Overall"
        and r.get("indicator_label") == "Up-to-date"
        and r.get("indicator_category_label") == "Yes"
        and _number(r, "estimate") is not None
    ]
    latest = max(want, key=lambda r: r["week_ending"])
    value = _live.duckdb_metric(
        con,
        sem,
        rows,
        "weekly_vaccination_coverage",
        {
            **{
                "age_group": "75+ years",
                "geography_name": "National",
                "demographic_level": "Overall",
                "indicator_category_label": "Yes",
            },
            "week_ending": latest["week_ending"][:10],
        },
    )
    assert value == float(latest["estimate"])
