"""Exact compiled SQL, methods, envelopes and manifests (unit tests for metric logic; they kill mutants).

These pin what the read path sends to DuckDB and what it says about it, for each aggregation: a sum of
counts, a published estimate with its bounds and flag, a date and a text period, and a running total with
its next-period figure. A change to any of them should be a reviewed change to these strings.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import query
from health_data_registry.query import Engine, QueryRefused, compile_metric, envelope
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import Manifest, build_snapshot, latest_snapshot, write_ndjson
from tests.conftest import CATALOG, raw_fetch, write_registry

EST_CATALOG = CATALOG.replace("shape: surveillance_series", "shape: estimate_table").replace(
    "location_column: reporting_area", "location_column: place"
)

EST_CONTRACT = """\
apiVersion: v3.0.2
kind: DataContract
id: toy_cases
version: 0.2.0
status: draft
schema:
  - name: toy_cases
    physicalName: toy_cases
    customProperties: [{property: totalRows, value: "place = 'ALL'"}]
    properties:
      - {name: place, physicalType: VARCHAR, logicalType: string, required: true, description: Place., customProperties: [{property: role, value: dimension}]}
      - {name: day, physicalType: DATE, logicalType: date, description: Week ending., customProperties: [{property: role, value: dimension}]}
      - {name: season, physicalType: VARCHAR, logicalType: string, description: Season or week text., customProperties: [{property: role, value: dimension}]}
      - {name: n, physicalType: BIGINT, logicalType: integer, description: Cases., customProperties: [{property: role, value: measure}]}
      - {name: n_flag, physicalType: VARCHAR, logicalType: string, description: Why n is empty., customProperties: [{property: role, value: attribute}, {property: flagFor, value: n}, {property: flagCodes, value: {"S": "suppressed"}}]}
      - {name: est, physicalType: DOUBLE, logicalType: number, description: Estimate., customProperties: [{property: role, value: measure}]}
      - {name: lo, physicalType: DOUBLE, logicalType: number, description: Lower bound., customProperties: [{property: role, value: measure}]}
      - {name: hi, physicalType: DOUBLE, logicalType: number, description: Upper bound., customProperties: [{property: role, value: measure}]}
      - {name: est_flag, physicalType: VARCHAR, logicalType: string, description: Est flag., customProperties: [{property: role, value: attribute}, {property: flagFor, value: est}]}
"""

EST_SEMANTIC = """\
dataset: toy_cases
version: 0.2.0
dimensions:
  - {name: where, column: place, type: categorical}
  - {name: week_ending, column: day, type: time}
  - {name: season, column: season, type: time}
measures:
  - {name: cases, column: n, kind: count, description: cases reported}
  - {name: estimate, column: est, kind: estimate}
  - {name: low, column: lo, kind: lower_bound}
  - {name: high, column: hi, kind: upper_bound}
metrics:
  - {name: total, description: Cases, measure: cases, aggregation: sum}
  - {name: total_all_rows, description: Cases with totals, measure: cases, aggregation: sum, exclude_total_rows: false, filters: ["place <> 'X'"]}
  - name: est_value
    description: Estimate
    measure: estimate
    aggregation: none
    exclude_total_rows: false
    uncertainty: {lower: low, upper: high}
"""

RECS = [
    {"place": "A", "day": "2025-01-04T00:00:00.000", "season": "2024-25", "n": "3", "n_flag": None,
     "est": "1.5", "lo": "1.0", "hi": "2.0", "est_flag": None},
    {"place": "A", "day": "2025-01-11T00:00:00.000", "season": "2024-25", "n": None, "n_flag": "S",
     "est": "2.5", "lo": "2.0", "hi": "3.0", "est_flag": "*"},
    {"place": "B", "day": "2026-01-03T00:00:00.000", "season": "2025-26", "n": "4", "n_flag": None,
     "est": "0.5", "lo": "0.1", "hi": "0.9", "est_flag": None},
    {"place": "ALL", "day": "2025-01-04T00:00:00.000", "season": "2024-25", "n": "70", "n_flag": None,
     "est": "9.0", "lo": "8.0", "hi": "10.0", "est_flag": None},
]  # fmt: skip

WHEN = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def est(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(
        write_registry(tmp_path / "reg", catalog=EST_CATALOG, contract=EST_CONTRACT, semantic=EST_SEMANTIC)
    )
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "e.ndjson"
    write_ndjson(f, RECS)
    build_snapshot(lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/e.json", RECS)], [f])
    return reg, Engine(registry=reg, lake=lake), lake


def test_sum_sql_and_method_are_exact(est: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = est
    cm = compile_metric(reg.get("toy_cases"), "total", filters={"where": ["A", "B"]}, group_by=["where"],
                        period={"start": 2025, "end": 2026})  # fmt: skip
    assert cm.sql == (
        'SELECT "place" AS "where", sum("n") AS "total", string_agg(DISTINCT "n_flag", \', \' ORDER BY "n_flag") '
        'FILTER (WHERE "n" IS NULL) AS flag FROM "toy_cases" WHERE NOT coalesce((place = \'ALL\'), false) AND '
        '"place" IN (?, ?) AND "day" >= CAST(? AS DATE) AND "day" <= CAST(? AS DATE) GROUP BY ALL ORDER BY ALL'
    )
    assert cm.params == ["A", "B", "2025-01-01", "2026-12-31"]
    assert cm.columns == ["where", "total", "flag"]
    assert cm.method == "Sum of cases (cases reported) by where, publisher total rows excluded."
    out = eng.query_metric("toy_cases", "total", group_by=["where"], period={"start": 2025, "end": 2025})
    assert out["rows"] == [["A", 3, "S"]]  # the suppressed week is named, never silently left out
    whole = eng.query_metric("toy_cases", "total", filters={"where": "B"})
    assert whole["rows"] == [[4, None]]
    assert whole["sql"].endswith("WHERE NOT coalesce((place = 'ALL'), false) AND \"place\" = ?")


def test_sum_with_total_rows_kept_and_metric_filter(est: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = est
    cm = compile_metric(reg.get("toy_cases"), "total_all_rows")
    assert cm.sql == (
        'SELECT sum("n") AS "total_all_rows", string_agg(DISTINCT "n_flag", \', \' ORDER BY "n_flag") FILTER '
        '(WHERE "n" IS NULL) AS flag FROM "toy_cases" WHERE (place <> \'X\')'
    )
    assert cm.method == "Sum of cases (cases reported)."
    assert eng.query_metric("toy_cases", "total_all_rows")["rows"] == [[77, "S"]]


def test_estimates_are_read_with_their_bounds_and_flag(est: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = est
    cm = compile_metric(reg.get("toy_cases"), "est_value", group_by=["where", "week_ending"],
                        period={"dimension": "week_ending", "start": "2025-01-05"})  # fmt: skip
    assert cm.sql == (
        'SELECT "place" AS "where", "day" AS "week_ending", "est" AS "est_value", "lo" AS "lower", "hi" AS "upper", '
        '"est_flag" AS flag FROM "toy_cases" WHERE "day" >= CAST(? AS DATE) ORDER BY ALL'
    )
    assert cm.params == ["2025-01-05"]
    assert cm.columns == ["where", "week_ending", "est_value", "lower", "upper", "flag"]
    assert cm.method == (
        "estimate as published (estimate); not summed. Lower and upper are the publisher's own uncertainty bounds."
    )
    out = eng.query_metric("toy_cases", "est_value", group_by=["where", "week_ending"],
                           period={"dimension": "week_ending", "start": "2025-01-05"})  # fmt: skip
    assert [r[:2] + r[2:] for r in out["rows"]] == [
        ["A", out["rows"][0][1], 2.5, 2.0, 3.0, "*"],
        ["B", out["rows"][1][1], 0.5, 0.1, 0.9, None],
    ]
    assert str(out["rows"][0][1]) == "2025-01-11"
    assert out["uncertainty"] == "Uncertainty bounds are shown only where the publisher gives them."


def test_text_periods_compare_as_text(est: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = est
    cm = compile_metric(reg.get("toy_cases"), "total", group_by=["season"],
                        period={"dimension": "season", "start": 2025, "end": 2025})  # fmt: skip
    assert cm.params == ["2025", "2025￿"]
    assert '"season" >= ? AND "season" <= ?' in cm.sql
    out = eng.query_metric(
        "toy_cases", "total", group_by=["season"], period={"dimension": "season", "end": 2024}
    )
    assert out["rows"] == [["2024-25", 3, "S"]]
    assert compile_metric(reg.get("toy_cases"), "total", period={"dimension": "season"}).params == []


def test_period_dimension_defaults_to_the_first_time_dimension(est: tuple[Registry, Engine, Path]) -> None:
    reg, _, _ = est
    cm = compile_metric(reg.get("toy_cases"), "total", period={"end": 2025})
    assert cm.params == ["2025-12-31"] and '"day" <= CAST(? AS DATE)' in cm.sql


@pytest.mark.parametrize(
    ("kwargs", "code", "msg"),
    [
        ({"metric_name": "nope"}, "unknown_metric",
         "toy_cases has no metric 'nope'. Metrics: total, total_all_rows, est_value"),
        ({"metric_name": "total", "group_by": ["county"]}, "unknown_dimension",
         "toy_cases has no dimension 'county'. Dimensions: where, week_ending, season"),
        ({"metric_name": "total", "filters": {"where": []}}, "empty_filter", "filter where has no values"),
        ({"metric_name": "total", "period": {"dimension": "where"}}, "no_time_dimension",
         "toy_cases: period needs a time dimension"),
        ({"metric_name": "total", "period": {"dimension": "nope"}}, "no_time_dimension",
         "toy_cases: period needs a time dimension"),
    ],
)  # fmt: skip
def test_refusals_say_exactly_why(est: tuple[Registry, Engine, Path], kwargs: dict[str, Any], code: str,
                                  msg: str) -> None:  # fmt: skip
    reg, _, _ = est
    with pytest.raises(QueryRefused) as e:
        compile_metric(reg.get("toy_cases"), **kwargs)
    assert (e.value.code, str(e.value)) == (code, msg)


def test_rates_refusals_name_the_dimensions(est: tuple[Registry, Engine, Path]) -> None:
    _, eng, _ = est
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("toy_cases", "est_value")
    assert (e.value.code, str(e.value)) == (
        "would_combine_rates",
        "est_value is published per group and can't be added up; choose group_by from: where, week_ending, season.",
    )
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("toy_cases", "est_value", group_by=["where"])
    assert str(e.value) == (
        "est_value is a estimate and more than one published value matches some where groups. Rates and "
        "estimates can't be added up: filter or group by the other dimensions too (describe_dataset lists them)."
    )
    one = eng.query_metric("toy_cases", "est_value", filters={"where": "B"})
    assert one["rows"] == [[0.5, 0.1, 0.9, None]]


def test_envelope_and_source_are_exact(est: tuple[Registry, Engine, Path]) -> None:
    reg, eng, lake = est
    ds = reg.get("toy_cases")
    m = eng.manifest("toy_cases")
    urls = [f"https://data.cdc.gov/resource/p{i}.json" for i in range(4)]
    m4 = Manifest(**{**m.__dict__, "source_urls": urls, "partial": True, "catalog_status": "draft"})
    out = envelope(ds, m4, ["a"], [[1], [2]], sql="S", params=[1], method="M")
    assert out == {
        "columns": ["a"],
        "rows": [[1], [2]],
        "row_count": 2,
        "truncated": False,
        "sql": "S",
        "params": [1],
        "method": "M",
        "source": {
            "dataset": "toy_cases",
            "title": ds.entry.title,
            "publisher": ds.entry.publisher,
            "source_urls": [*urls[:3], "..."],
            "landing_url": "https://data.cdc.gov/d/toy0-toy0",
            "retrieved_at": m.retrieved_at,
            "snapshot_id": m.snapshot_id,
            "contract_version": "0.2.0",
            "review_status": "draft",
            "partial_snapshot": True,
            "licence": "public-domain-us-federal",
            "terms_url": "https://www.cdc.gov/other/agencymaterials.html",
            "citation": ds.entry.citation,
        },
        "caveats": [
            "This snapshot is a partial sample, not the full dataset.",
            "This dataset's contract is a draft that no maintainer has reviewed yet.",
            "Counts for the latest year are provisional.",
        ],
        "uncertainty": "Uncertainty bounds are shown only where the publisher gives them.",
    }
    reviewed = Manifest(**{**m.__dict__, "catalog_status": "reviewed"})
    small = envelope(ds, reviewed, ["a"], [[1]], sql="S", params=[], method="M")
    assert small["caveats"] == ["Counts for the latest year are provisional."]
    assert small["source"]["source_urls"] == ["https://data.cdc.gov/resource/e.json"]


def test_envelope_cuts_at_the_cap_and_says_so(
    est: tuple[Registry, Engine, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    reg, eng, _ = est
    monkeypatch.setattr(query, "MAX_ROWS", 2)
    out = envelope(reg.get("toy_cases"), eng.manifest("toy_cases"), ["a"], [[1], [2], [3]], sql="", params=[],
                   method="")  # fmt: skip
    assert (out["rows"], out["row_count"], out["truncated"]) == ([[1], [2]], 2, True)
    assert out["caveats"][0] == "Only the first 2 rows are shown."
    exact = envelope(reg.get("toy_cases"), eng.manifest("toy_cases"), ["a"], [[1], [2]], sql="", params=[],
                     method="")  # fmt: skip
    assert exact["truncated"] is False


def test_surveillance_counts_say_they_have_no_interval(registry: Registry, lake: Path) -> None:
    out = Engine(registry=registry, lake=lake).query_metric("toy_cases", "case_count")
    assert out["uncertainty"] == "Observed counts carry no uncertainty interval."


# ---------------------------------------------------------------- the snapshot store


def test_build_snapshot_manifest_and_pointer(est: tuple[Registry, Engine, Path], tmp_path: Path) -> None:
    reg, _, _ = est
    lake = tmp_path / "lake2"
    lake.mkdir()
    f = tmp_path / "e2.ndjson"
    write_ndjson(f, RECS)
    early = raw_fetch(
        "https://data.cdc.gov/resource/p1.json", RECS[:1], when=datetime(2026, 11, 23, tzinfo=UTC)
    )
    late = raw_fetch("https://data.cdc.gov/resource/p2.json", RECS[1:], when=WHEN)
    m = build_snapshot(lake, reg.get("toy_cases"), [early, late], [f], partial=True)
    digest = hashlib.sha256((early.sha256 + late.sha256).encode()).hexdigest()[:12]
    assert m.snapshot_id == f"20261124T120000Z-{digest}"  # the latest fetch time, then the content hash
    found = latest_snapshot(lake, "toy_cases")
    assert found is not None
    path, read = found
    assert read == m
    assert json.loads((path / "manifest.json").read_text()) == {
        "dataset": "toy_cases",
        "snapshot_id": m.snapshot_id,
        "contract_version": "0.2.0",
        "semantic_version": "0.2.0",
        "catalog_status": "draft",
        "retrieved_at": "2026-11-24T12:00:00+00:00",
        "source_urls": ["https://data.cdc.gov/resource/p1.json", "https://data.cdc.gov/resource/p2.json"],
        "raw_sha256": [early.sha256, late.sha256],
        "row_count": 4,
        "licence": "public-domain-us-federal",
        "redistribution": True,
        "partial": True,
    }
    assert (lake / "datasets" / "toy_cases" / "LATEST").read_text() == m.snapshot_id + "\n"
    assert sorted(p.name for p in lake.iterdir()) == ["datasets", "raw"]  # no temporary folder left behind
    assert (path / "data.parquet").stat().st_size > 0
    again = build_snapshot(lake, reg.get("toy_cases"), [early, late], [f])
    assert again.snapshot_id == m.snapshot_id and again.partial is False
    assert sorted(p.name for p in (lake / "datasets" / "toy_cases").iterdir()) == sorted(
        ["LATEST", m.snapshot_id]
    )


def test_build_snapshot_refusals(est: tuple[Registry, Engine, Path], tmp_path: Path) -> None:
    reg, _, _ = est
    with pytest.raises(ValueError, match="^a snapshot needs at least one fetch$"):
        build_snapshot(tmp_path, reg.get("toy_cases"), [], [])
    f = tmp_path / "bad.ndjson"
    write_ndjson(f, [{**RECS[0], "place": None}, {**RECS[1], "place": None}])
    with pytest.raises(
        ValueError, match="^toy_cases: required column place has 2 empty values; check the contract$"
    ):
        build_snapshot(tmp_path, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/x.json", [])], [f])
    assert not list(tmp_path.glob(".toy_cases-*"))
    assert latest_snapshot(tmp_path, "toy_cases") is None


def test_flags_and_whole_counts_load_as_published(est: tuple[Registry, Engine, Path]) -> None:
    _, eng, _ = est
    rows = eng.connect().execute("SELECT place, n, n_flag FROM toy_cases ORDER BY day, place").fetchall()
    assert rows == [("A", 3, None), ("ALL", 70, None), ("A", None, "S"), ("B", 4, None)]
