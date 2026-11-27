"""Live contract test for cdc_heptracker (HepTracker Dashboard, data.cdc.gov jkcx-ndu8)."""

from __future__ import annotations

from collections import Counter

import pytest

from tests.integration import _live

DS = "cdc_heptracker"
LIMIT = 50000
CODES = {"—", "U", "N", "S", "UR"}
pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def live():
    cat, con, sem = _live.load(DS)
    meta = _live.get_json(cat, f"https://{cat['source']['host']}/api/views/{cat['source']['dataset']}.json")
    # The whole table (8,588 rows on 2026-11-25) in one ordered page.
    query = {"$limit": str(LIMIT), "$order": "event, year, character, value, type"}
    rows = _live.get_json(cat, _live.with_query(cat["source"]["api_url"], query))
    assert len(rows) < LIMIT, "the table outgrew one page; page through it before trusting these checks"
    return cat, con, sem, rows, [c["fieldName"] for c in meta["columns"]]


def _number(text):
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return None


def test_contract_structure(live):
    _cat, con, _sem, rows, served = live
    _live.check_structure(con, rows, served)


def test_conditions_and_types_are_the_ones_the_catalog_lists(live):
    cat, _con, _sem, rows, _ = live
    assert {r.get("event") for r in rows} <= set(cat["conditions"]["labels"])
    assert {r.get("type") for r in rows} <= {"Case", "Death"}


def test_every_count_and_rate_is_a_number_or_a_listed_code(live):
    _cat, _con, _sem, rows, _ = live
    for field in ("count", "rate"):
        texts = {r.get(field) for r in rows if r.get(field) not in (None, "")}
        assert {t for t in texts if _number(t) is None} <= CODES, field
        assert all(_number(t) >= 0 for t in texts if _number(t) is not None), field


def test_every_row_has_a_count_or_a_code(live):
    _cat, _con, _sem, rows, _ = live
    assert all(r.get("count") not in (None, "") for r in rows)


def test_one_row_per_cell(live):
    _cat, _con, _sem, rows, _ = live
    keys = Counter(tuple(r.get(k) for k in ("event", "year", "character", "value", "type")) for r in rows)
    assert max(keys.values()) == 1


def test_the_nationwide_total_row_is_still_labelled_total(live):
    _cat, _con, _sem, rows, _ = live
    assert any(r.get("character") == "Area" and r.get("value") == "Total" for r in rows)


@pytest.mark.parametrize(
    ("metric", "event", "typ", "field"),
    [
        ("national_case_count", "Acute Hepatitis C", "Case", "count"),
        ("national_case_rate", "Acute Hepatitis B", "Case", "rate"),
        ("national_death_count", "Hepatitis C", "Death", "count"),
    ],
)
def test_latest_national_figure_two_ways(live, metric, event, typ, field):
    _cat, con, sem, rows, _ = live
    total = [
        r for r in rows
        if r.get("event") == event and r.get("type") == typ and r.get("character") == "Area"
        and r.get("value") == "Total" and _number(r.get(field)) is not None
    ]  # fmt: skip
    assert total, f"no nationwide {field} for {event}"
    last = max(total, key=lambda r: int(r["year"]))
    got = _live.duckdb_metric(con, sem, rows, metric, {"year": int(last["year"]), "condition": event})
    assert got == _number(last[field])
