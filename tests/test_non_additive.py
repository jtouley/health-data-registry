"""Hard rule 2: a summed metric never adds up values of a dimension whose values overlap or are different measures.

Synthetic rows only, shaped like NNDSS's weekly table (the real registry: catalog/, contracts/, semantic/). One
place and week carries overlapping case-status labels for one condition (Confirmed + Probable = Total) and a
label that is not cases in people at all (Rabies, Animal). Adding the condition labels up would count the same
cases twice and mix animal rabies into human counts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import tools
from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch
from tests.test_real_contracts import nndss

ROOT = Path(__file__).resolve().parents[1]

ROWS = [
    nndss("KANSAS", "3", m3="30", label="Coccidioidomycosis, Confirmed"),
    nndss("KANSAS", "2", m3="20", label="Coccidioidomycosis, Probable"),
    nndss("KANSAS", "5", m3="50", label="Coccidioidomycosis, Total"),
    nndss("KANSAS", "7", m3="70", label="Rabies, Animal"),
]
WEEK = {"mmwr_year": 2024, "mmwr_week": 52}


@pytest.fixture
def nndss_lake(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "n.ndjson"
    write_ndjson(f, ROWS)
    build_snapshot(
        lake, reg.get("cdc_nndss_weekly"), [raw_fetch("https://data.cdc.gov/resource/x.json", ROWS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False)


@pytest.mark.xfail(
    strict=True,
    reason="hard rule 2 bug: a summed metric (weekly_cases) compiles without the condition dimension pinned, so "
    "it adds every condition label: overlapping Confirmed/Probable/Total and Rabies, Animal",
)
def test_weekly_cases_unpinned_condition_is_refused(nndss_lake: tuple[Registry, Engine]) -> None:
    reg, eng = nndss_lake
    ds = reg.get("cdc_nndss_weekly")
    # The real compiler: year and week pinned, condition not.
    with pytest.raises(QueryRefused):
        compile_metric(ds, "weekly_cases", filters=WEEK)
    # The engine the tools call: today it returns 3 + 2 + 5 + 7 = 17, a number that means nothing.
    with pytest.raises(QueryRefused):
        eng.query_metric("cdc_nndss_weekly", "weekly_cases", filters=WEEK)


def test_tools_pin_one_label_per_series(nndss_lake: tuple[Registry, Engine]) -> None:
    """get_series and the generic series bind the condition to one label each, so the tools never add labels."""
    reg, eng = nndss_lake
    ds = reg.get("cdc_nndss_weekly")
    for label in ("Coccidioidomycosis, Confirmed", "Rabies, Animal"):
        assert all(s.filters["condition"] == label for s in ds.condition_series(label))
    got = []
    for label in (
        "Coccidioidomycosis, Confirmed",
        "Coccidioidomycosis, Probable",
        "Coccidioidomycosis, Total",
    ):
        out: Any = tools.get_series(reg, eng, condition=label, locations=["Kansas"],
                                    source_id="cdc_nndss_weekly", fmt="json")  # fmt: skip
        got += [(r[9], r[2]) for r in out["rows"] if r[5] == "year_end_ytd"]
    assert got == [
        ("Coccidioidomycosis, Confirmed", 30),
        ("Coccidioidomycosis, Probable", 20),
        ("Coccidioidomycosis, Total", 50),
    ]
