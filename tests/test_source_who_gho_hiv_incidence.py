"""who_gho_hiv_incidence (WHO GHO SDGHIV, new HIV infections per 1000 uninfected population) through the real catalog.

WHO's terms forbid keeping a copy, so the source is read live: here a fake publisher answers in memory with
synthetic OData rows (made-up numbers, WHO's field names and codes), and nothing is written to disk.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import tools, transform_gen
from health_data_registry.live import load_live
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.staging import ContractLayoutRefused
from health_data_registry.store import RawFetch

ROOT = Path(__file__).resolve().parents[1]
DS = "who_gho_hiv_incidence"
URL = "https://ghoapi.azureedge.net/api/SDGHIV"


def _row(
    i: int, place: str, kind: str, year: int, sex: str, v: float, lo: float, hi: float
) -> dict[str, Any]:
    parent = None if kind != "COUNTRY" else "AFR"
    return {
        "Id": i, "IndicatorCode": "SDGHIV", "SpatialDimType": kind, "SpatialDim": place, "TimeDimType": "YEAR",
        "ParentLocationCode": parent, "ParentLocation": None if parent is None else "Africa", "Dim1Type": "SEX",
        "TimeDim": year, "Dim1": sex, "Dim2Type": None, "Dim2": None, "Dim3Type": None, "Dim3": None,
        "DataSourceDimType": None, "DataSourceDim": None, "Value": f"{v} [{lo} - {hi}]", "NumericValue": v,
        "Low": lo, "High": hi, "Comments": None, "Date": "2026-07-31T13:20:25.203+02:00",
    }  # fmt: skip


ROWS = [
    _row(1, "ZAF", "COUNTRY", 2023, "SEX_BTSX", 4.0, 3.5, 4.6),
    _row(2, "ZAF", "COUNTRY", 2023, "SEX_FMLE", 5.0, 4.3, 5.8),
    _row(3, "ZAF", "COUNTRY", 2023, "SEX_MLE", 3.0, 2.5, 3.6),
    _row(4, "ZAF", "COUNTRY", 2024, "SEX_BTSX", 3.8, 3.2, 4.4),
    _row(5, "GLOBAL", "GLOBAL", 2024, "SEX_BTSX", 0.2, 0.15, 0.27),
]


def _get(rows: list[dict[str, Any]]) -> Any:
    def get(url: str) -> RawFetch:
        assert url.startswith(URL), url
        body = json.dumps({"@odata.context": "x", "value": rows}).encode()
        return RawFetch(
            url=url, content=body, sha256="0" * 64, retrieved_at=datetime(2026, 11, 25, tzinfo=UTC)
        )

    return get


@pytest.fixture
def live(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    eng = Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=True, getter=_get(ROWS))
    return reg, eng, lake


def _table(out: Any) -> list[dict[str, Any]]:
    assert isinstance(out, dict), out
    return [dict(zip(out["columns"], r, strict=True)) for r in out["rows"]]


def test_read_live_never_stored_and_no_dbt_model() -> None:
    reg = load_registry(ROOT)
    ds = reg.get(DS)
    assert ds.entry.tier == "live" and not ds.entry.licence.redistribution and not ds.entry.hosted
    files = transform_gen.generate(reg)
    assert not any(DS in str(p) for p in files)
    assert DS not in transform_gen.raw_sources(reg)


def test_both_sexes_with_its_bounds_and_whos_terms_on_every_answer(
    live: tuple[Registry, Engine, Path],
) -> None:
    reg, eng, lake = live
    out = tools.get_series(reg, eng, condition="HIV", locations=["ZAF"], source_id=DS, fmt="json")
    rows = _table(out)
    assert [(r["year"], r["value"], r["lower"], r["upper"]) for r in rows] == [
        (2023, 4.0, 3.5, 4.6),
        (2024, 3.8, 3.2, 4.4),
    ]
    assert {r["measure"] for r in rows} == {"estimate"}
    notes = " ".join(out["notes"])
    assert "Terms and Conditions for datasets" in notes and "not stored" in notes
    assert not lake.exists() or not any(lake.rglob("*"))  # nothing written


def test_female_and_male_are_never_added_to_make_both_sexes(live: tuple[Registry, Engine, Path]) -> None:
    _, eng, _ = live
    out = eng.query_metric(
        DS, "hiv_incidence_rate_by_sex", filters={"place": ["ZAF"]}, group_by=["sex", "year"]
    )
    assert "sum(" not in out["sql"].lower()
    at = {c: i for i, c in enumerate(out["columns"])}
    got = sorted((r[at["sex"]], r[at["hiv_incidence_rate_by_sex"]]) for r in out["rows"])
    assert got == [("SEX_FMLE", 5.0), ("SEX_MLE", 3.0)]


def test_a_place_with_no_who_row_gets_nothing_rather_than_a_guess(
    live: tuple[Registry, Engine, Path],
) -> None:
    reg, eng, _ = live
    out = tools.get_series(reg, eng, condition="HIV", locations=["USA"], source_id=DS, fmt="json")
    assert isinstance(out, dict) and out.get("refused") == "filter_matched_nothing", out
    assert "ZAF" in out["available"][f"{DS} places"]


def test_both_sexes_metric_keeps_region_and_global_rows(live: tuple[Registry, Engine, Path]) -> None:
    """totalRows marks the region and GLOBAL rows; the metric must still return them (exclude_total_rows: false)."""
    reg, eng, _ = live
    out = tools.get_series(reg, eng, condition="HIV", locations=["GLOBAL"], source_id=DS, fmt="json")
    assert [(r["location"], r["year"], r["value"]) for r in _table(out)] == [("GLOBAL", 2024, 0.2)]


def test_gbd_alignment_keeps_whos_bounds(live: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 2: the published uncertainty range comes back beside the estimate, never dropped."""
    reg, eng, _ = live
    out = tools.get_aligned_with_gbd(reg, eng, condition="HIV", locations=["ZAF"], fmt="json")
    rows = [r for r in _table(out) if r["source_id"] == DS]
    assert [(r["year"], r["observed_value"], r["lower"], r["upper"]) for r in rows] == [
        (2023, 4.0, 3.5, 4.6),
        (2024, 3.8, 3.2, 4.4),
    ]
    assert any("published bounds are in lower and upper" in n for n in out["notes"])


def test_a_failed_data_check_reaches_the_get_series_answer(tmp_path: Path) -> None:
    """A live read has no release gate: a failed check must reach the clinician as a caveat on the answer."""
    reg = load_registry(ROOT)
    bad = [*ROWS[:3], _row(4, "ZAF", "COUNTRY", 2024, "SEX_BTSX", -1.0, 3.2, 4.4), ROWS[4]]
    eng = Engine(registry=reg, lake=tmp_path / "lake", hub=None, fetch_at_run_time=True, getter=_get(bad))
    out = tools.get_series(reg, eng, condition="HIV", locations=["ZAF"], source_id=DS, fmt="json")
    assert (2024, -1.0) in [(r["year"], r["value"]) for r in _table(out)]  # shown as published
    assert any("A data check on this source failed" in n and "ever negative" in n for n in out["notes"]), out[
        "notes"
    ]


def _status(rows: list[dict[str, Any]]) -> dict[str, str]:
    reg = load_registry(ROOT)
    got = load_live(
        duckdb.connect(),
        reg.get(DS),
        [{k: (None if v is None else str(v)) for k, v in r.items()} for r in rows],
    )
    return {r["test"]: r["status"] for r in got.quality.results}


@pytest.mark.parametrize(
    ("rule", "status", "rows"),
    [
        ("one_row_per_place_year_and_sex", "fail", [*ROWS, ROWS[0] | {"Id": 9}]),
        ("indicator_is_sdghiv", "fail", [*ROWS[:4], ROWS[4] | {"IndicatorCode": "HIV_0000000026"}]),
        ("sex_is_a_who_sex_code", "warn", [*ROWS, _row(9, "ZAF", "COUNTRY", 2025, "SEX_NOA", 3.0, 2.5, 3.6)]),
        ("rate_not_negative", "fail", [*ROWS[:4], _row(9, "GLOBAL", "GLOBAL", 2024, "SEX_BTSX", -0.1, 0.1, 0.2)]),
        ("rate_not_negative", "fail", [*ROWS[:4], _row(9, "GLOBAL", "GLOBAL", 2024, "SEX_BTSX", 0.1, -0.1, 0.2)]),
        ("bounds_hold_the_estimate", "warn", [*ROWS[:4], ROWS[4] | {"NumericValue": None, "Low": 0.3}]),
        ("some_estimates_present", "fail", [r | {"NumericValue": None} for r in ROWS]),
    ],
    ids=["repeated_cell", "other_indicator", "sex_noa", "negative_estimate", "negative_bound", "inverted_bounds",
         "no_estimates"],
)  # fmt: skip
def test_each_rule_fails_when_it_should(rule: str, status: str, rows: list[dict[str, Any]]) -> None:
    assert _status(ROWS)[rule] == "pass"
    assert _status(rows)[rule] == status


def test_a_row_without_a_sex_is_kept_and_named_in_a_caveat() -> None:
    """Sex is part of the grain and both metrics select on it: a row without one fails a check, never refuses."""
    reg = load_registry(ROOT)
    rows = [*ROWS[:4], ROWS[4] | {"Dim1": None}]
    got = load_live(
        duckdb.connect(),
        reg.get(DS),
        [{k: (None if v is None else str(v)) for k, v in r.items()} for r in rows],
    )
    assert got.staging.row_count == len(rows)
    assert {r["test"]: r["status"] for r in got.quality.results}["sex_given_on_every_row"] == "fail"
    assert any("sex code is in no answer" in n for n in got.quality.caveats())


def test_a_us_state_question_is_told_no() -> None:
    reg = load_registry(ROOT)
    text = tools.suggest_datasets(reg, "new HIV infections in Michigan")
    assert isinstance(text, str)
    rows = {r["source_id"]: r for r in csv.DictReader(x for x in io.StringIO(text) if not x.startswith("#"))}
    assert rows[DS]["can_answer"] == "no"
    assert "not state" in rows[DS]["why"]


def test_an_omitted_field_refuses_the_source_in_memory_only() -> None:
    """A publisher that stops sending a declared field refuses the source for the session; nothing is kept."""
    reg = load_registry(ROOT)
    con = duckdb.connect()
    good = load_live(
        con, reg.get(DS), [{k: (None if v is None else str(v)) for k, v in r.items()} for r in ROWS]
    )
    assert good.staging.row_count == len(ROWS)
    assert all(r["status"] == "pass" for r in good.quality.results), good.quality.results
    con.execute(f'DROP TABLE "{DS}"')
    with pytest.raises(ContractLayoutRefused):
        load_live(con, reg.get(DS), [{k: str(v) for k, v in r.items() if k != "Dim1"} for r in ROWS])


def test_an_estimate_outside_its_bounds_warns_and_is_shown_as_published() -> None:
    reg = load_registry(ROOT)
    con = duckdb.connect()
    bad = [*ROWS[:4], _row(9, "GLOBAL", "GLOBAL", 2024, "SEX_BTSX", 0.5, 0.15, 0.27)]
    got = load_live(
        con, reg.get(DS), [{k: (None if v is None else str(v)) for k, v in r.items()} for r in bad]
    )
    status = {r["test"]: r["status"] for r in got.quality.results}
    assert status["bounds_hold_the_estimate"] == "warn"
    assert status["rate_not_negative"] == "pass"
