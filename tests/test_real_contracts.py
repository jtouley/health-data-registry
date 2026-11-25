"""The real contracts' structural facts, checked offline on tiny synthetic tables shaped like the publisher's.

dbt review 4: the NNDSS totalRows predicate decides which rows every sum drops. These unit tests load the real
registry (catalog/, contracts/, semantic/) and assert, on hand-written rows, the structure the contract states:
states, DC and New York City add up to US RESIDENTS; adding territories and non-US residents gives TOTAL; census
divisions are summary rows. The numbers are made up; only the layout follows the publisher's.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]


def nndss(area: str, m1: Any, *, summary: bool = False, flag: str | None = None, week: int = 52,
          m3: Any = None, year: int = 2024, label: str = "Legionellosis") -> dict[str, Any]:  # fmt: skip
    return {
        "states": area, "year": str(year), "week": str(week), "label": label, "m1": m1, "m1_flag": flag,
        "m2": None, "m2_flag": None, "m3": m3, "m3_flag": flag if m3 is None else None, "m4": None,
        "m4_flag": None, "location1": None if summary else area, "location2": area if summary else None,
        "sort_order": None,
    }  # fmt: skip


ROWS = [
    nndss("MICHIGAN", "3", m3="30"),
    nndss("NEW YORK", "2", m3="20"),
    nndss("NEW YORK CITY", "4", m3="40"),
    nndss("DISTRICT OF COLUMBIA", None, flag="-"),  # no reported cases: a zero, by the codebook
    nndss("PUERTO RICO", "1", m3="10"),
    nndss("Non-U.S. Residents", "1", summary=True, m3="10"),  # its own bucket, title case as from 2025
    nndss("EAST NORTH CENTRAL", "3", summary=True, m3="30"),  # a census division: a summary row
    nndss("US RESIDENTS", "9", summary=True, m3="90"),
    nndss("US TERRITORIES", "1", summary=True, m3="10"),
    nndss("TOTAL", "11", summary=True, m3="110"),
]


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    f = tmp_path / "nndss.ndjson"
    write_ndjson(f, ROWS)
    lake.mkdir()
    build_snapshot(
        lake,
        reg.get("cdc_nndss_weekly"),
        [raw_fetch("https://data.cdc.gov/resource/x9gk-5huc.json", ROWS)],
        [f],
    )
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


def one(eng: Engine, metric: str, **kw: Any) -> Any:
    out = eng.query_metric("cdc_nndss_weekly", metric, **kw)
    assert len(out["rows"]) == 1, out["rows"]
    return out["rows"][0][0]


WEEK = {"condition": "Legionellosis", "mmwr_year": 2024, "mmwr_week": 52}


def test_nndss_sum_of_non_total_rows_equals_the_total_row(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    assert one(eng, "weekly_cases", filters=WEEK) == 11  # = TOTAL: states, DC, NYC, territories, non-US
    assert one(eng, "us_resident_weekly_cases", filters=WEEK) == 9  # the published US RESIDENTS row
    assert one(eng, "year_to_date_cases", filters=WEEK) == 110


def test_nndss_year_to_date_is_read_at_one_week(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("cdc_nndss_weekly", "year_to_date_cases", filters={"condition": "Legionellosis"})
    assert e.value.code == "needs_single_value"


def test_nndss_no_reported_cases_reads_as_zero_with_its_flag(real: tuple[Registry, Engine]) -> None:
    _, eng = real
    out = eng.query_metric(
        "cdc_nndss_weekly", "weekly_cases", filters={**WEEK, "reporting_area": "DISTRICT OF COLUMBIA"}
    )
    assert out["rows"] == [[0, 0, None]]  # 0 cases, no empty values: '-' is not a gap
    assert "'-' as no reported cases" in out["method"]


STATUSES = ("Confirmed", "Probable", "Total")


def _status_counts(labels: list[str]) -> dict[str, int]:
    """How many of the publisher's own condition labels end in each case status (', Total' in either case)."""
    return {s: sum(x.casefold().endswith(f", {s.casefold()}") for x in labels) for s in STATUSES}


def test_case_status_text_agrees_with_the_publishers_labels() -> None:
    """#174 item 1: the NNDSS caveat said case status isn't published, but 30 of CDC's own labels carry Confirmed,
    Probable or Total. Wherever labels name a status, no caveat or contract text may say it isn't published, and
    the caveat gives the counts the labels give (hard rule 1: from the publisher's own labels)."""
    reg = load_registry(ROOT)
    for ds in reg:
        counts = _status_counts(list(ds.condition_labels))
        if not counts["Confirmed"] and not counts["Probable"]:
            continue
        texts = [*ds.entry.caveats, *ds.contract.open_questions, str(ds.contract.raw)]
        for text in texts:
            assert "case status (confirmed, probable) is not published" not in text.casefold(), text
            assert "without a confirmed/probable split" not in text, text
        said = [c for c in ds.entry.caveats if "case status" in c.casefold()]
        assert len(said) == 1, said
        for status, n in counts.items():
            assert re.search(rf"\b{n} (names )?end in '{status}'", said[0]), (status, n, said[0])
    assert _status_counts(list(reg.get("cdc_nndss_weekly").condition_labels)) == {
        "Confirmed": 14, "Probable": 11, "Total": 5,
    }  # fmt: skip
