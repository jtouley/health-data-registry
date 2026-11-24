"""Running totals with late reports and a later restatement (the shape of the NNDSS weekly tables).

Regression: adding up weekly counts under-counts badly, because late reports land in the year-to-date
column, not in the week. Michigan C. auris 2023 read 62 that way; the publisher's restated total is 110.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from health_data_registry import tools
from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import RegistryError, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import CATALOG as BASE_CATALOG
from tests.conftest import raw_fetch, write_registry

CATALOG = BASE_CATALOG.replace("location_column: reporting_area", "location_column: area")

CONTRACT = """\
apiVersion: v3.0.2
kind: DataContract
id: toy_cases
version: 0.1.0
status: draft
schema:
  - name: toy_cases
    physicalName: toy_cases
    customProperties: [{property: totalRows, value: "upper(area) = 'US RESIDENTS'"}]
    properties:
      - {name: area, physicalType: VARCHAR, logicalType: string, required: true, description: Reporting area., customProperties: [{property: role, value: dimension}]}
      - {name: year, physicalType: INTEGER, logicalType: integer, required: true, description: MMWR year., customProperties: [{property: role, value: dimension}]}
      - {name: week, physicalType: INTEGER, logicalType: integer, required: true, description: MMWR week., customProperties: [{property: role, value: dimension}]}
      - {name: wk, physicalType: BIGINT, logicalType: integer, description: Cases first reported for the week., customProperties: [{property: role, value: measure}]}
      - {name: ytd, physicalType: BIGINT, logicalType: integer, description: Year-to-date cases., customProperties: [{property: role, value: measure}]}
      - {name: prior_ytd, physicalType: BIGINT, logicalType: integer, description: Last year's cases to the same week, restated., customProperties: [{property: role, value: measure}]}
"""

SEMANTIC = """\
dataset: toy_cases
version: 0.1.0
dimensions:
  - {name: state, column: area, type: categorical}
  - {name: year, column: year, type: time}
  - {name: week, column: week, type: time}
measures:
  - {name: wk, column: wk, kind: count}
  - {name: ytd, column: ytd, kind: count}
  - {name: prior_ytd, column: prior_ytd, kind: count}
metrics:
  - name: annual
    description: Annual cases
    measure: ytd
    aggregation: cumulative_latest
    order_by: week
    restated_by: {measure: prior_ytd, complete_at: 3}
series:
  - {condition: legionellosis, label: Legionellosis, metric: annual, measure: cases, location_dimension: state, time_dimension: year}
"""


def rec(area: str, year: int, week: int, wk: int | None, ytd: int, prior: int | None) -> dict[str, object]:
    return {"area": area, "year": year, "week": week, "wk": wk, "ytd": ytd, "prior_ytd": prior}


# Three-week "years". 2023: weekly counts add to 3, but late reports bring year-to-date to 10, and the 2024
# table restates 2023 as 12 by week 3. 2024 is still provisional (its next table hasn't reached week 3).
RECORDS = [
    rec("MICHIGAN", 2023, 1, 1, 4, None),
    rec("MICHIGAN", 2023, 2, 1, 7, None),
    rec("MICHIGAN", 2023, 3, 1, 10, None),
    rec("Michigan", 2024, 1, 2, 5, 6),  # the publisher re-cased the label this year
    rec("Michigan", 2024, 2, None, 9, 9),
    rec("Michigan", 2024, 3, 1, 15, 12),
    rec("Michigan", 2025, 1, 1, 2, 7),
    rec("US RESIDENTS", 2023, 3, 9, 999, None),
]


def _setup(tmp_path: Path) -> tuple[object, Engine]:
    reg = load_registry(
        write_registry(tmp_path / "reg", catalog=CATALOG, contract=CONTRACT, semantic=SEMANTIC)
    )
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/x.json", RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake)


def test_restated_total_wins_and_status_says_so(tmp_path: Path) -> None:
    reg, eng = _setup(tmp_path)
    out = tools.get_series(reg, eng, condition="legionellosis", locations=["Michigan"], fmt="json")  # type: ignore[arg-type]
    assert isinstance(out, dict)
    assert [[*r[:3], r[5]] for r in out["rows"]] == [
        ["Michigan", 2023, 12, "final: restated in the next table"],  # not 3 (weekly sum) nor 10 (stale YTD)
        ["Michigan", 2024, 15, "provisional: running total as of 3"],
        ["Michigan", 2025, 2, "provisional: running total as of 1"],
    ]


def test_period_filter_still_sees_next_years_restatement(tmp_path: Path) -> None:
    reg, eng = _setup(tmp_path)
    out = tools.get_series(reg, eng, condition="legionellosis", locations=["Michigan"], year_start=2023,  # type: ignore[arg-type]
                           year_end=2023, fmt="json")  # fmt: skip
    assert isinstance(out, dict)
    assert [r[:3] for r in out["rows"]] == [["Michigan", 2023, 12]]


def test_cumulative_needs_the_period_in_group_by(tmp_path: Path) -> None:
    reg, _ = _setup(tmp_path)
    with pytest.raises(QueryRefused, match="group by year"):
        compile_metric(reg.get("toy_cases"), "annual", group_by=["state"])  # type: ignore[attr-defined]


def test_cumulative_refused_on_rates(tmp_path: Path) -> None:
    bad = SEMANTIC.replace(
        "- {name: ytd, column: ytd, kind: count}", "- {name: ytd, column: ytd, kind: rate}"
    )
    with pytest.raises(RegistryError, match="running count"):
        load_registry(write_registry(tmp_path, catalog=CATALOG, contract=CONTRACT, semantic=bad))
