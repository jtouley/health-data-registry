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
    assert isinstance(out, dict) and out.get("rows") in ([], None)


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
