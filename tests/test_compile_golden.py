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
from health_data_registry.adapters import method_digest
from health_data_registry.query import Engine, QueryRefused, compile_metric, envelope
from health_data_registry.registry import Registry, load_registry
from health_data_registry.staging import PublisherDataRefused
from health_data_registry.store import Manifest, build_snapshot, latest_snapshot, sha256_file, write_ndjson
from tests.conftest import CATALOG, raw_fetch, write_registry

EST_CATALOG = CATALOG.replace("shape: surveillance_series", "shape: estimate_table").replace(
    "location_column: reporting_area", "location_column: place"
)

EST_CONTRACT = """\
apiVersion: v3.2.0
kind: DataContract
id: toy_cases
version: 0.2.0
status: draft
customProperties:
  - {property: unreliableRule, value: {kind: none, description: "None stated (toy)."}}
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
  - {name: where, column: place, type: categorical, non_additive: false}
  - {name: week_ending, column: day, type: time, non_additive: false}
  - {name: season, column: season, type: time, non_additive: false}
measures:
  - {name: cases, column: n, kind: count, description: cases reported}
  - {name: estimate, column: est, kind: estimate}
  - {name: low, column: lo, kind: lower_bound}
  - {name: high, column: hi, kind: upper_bound}
metrics:
  - {name: total, description: Cases, measure: cases, aggregation: sum}
  - {name: total_all_rows, description: Cases with totals, measure: cases, aggregation: sum, exclude_total_rows: false, filters: ["place = 'A'"]}
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
        'SELECT "place" AS "where", CASE WHEN count(*) FILTER (WHERE "n" IS NULL) = 0 THEN sum("n") END AS "total", '
        'count(*) FILTER (WHERE "n" IS NULL) AS suppressed_rows, string_agg(DISTINCT "n_flag", \', \' ORDER BY '
        '"n_flag") FILTER (WHERE "n" IS NULL) AS flag FROM "toy_cases" WHERE NOT coalesce((place = \'ALL\'), false) '
        "AND "
        '"place" IN (?, ?) AND "day" >= CAST(? AS DATE) AND "day" <= CAST(? AS DATE) GROUP BY ALL ORDER BY ALL'
    )
    assert cm.params == ["A", "B", "2025-01-01", "2026-12-31"]
    assert cm.columns == ["where", "total", "suppressed_rows", "flag"]
    assert cm.method == (
        "Sum of cases (cases reported) by where, publisher total rows excluded. A group with any suppressed or "
        "missing value has no total (never a partial sum); suppressed_rows gives how many of its values were empty."
    )
    out = eng.query_metric("toy_cases", "total", group_by=["where"], period={"start": 2025, "end": 2025})
    # Regression: A has 3 cases one week and a suppressed week. Its total is unknown, never 3; the suppressed
    # week is counted and its flag named.
    assert out["rows"] == [["A", None, 1, "S"]]
    whole = eng.query_metric("toy_cases", "total", filters={"where": "B"})
    assert whole["rows"] == [[4, 0, None]]
    assert whole["sql"].endswith("WHERE NOT coalesce((place = 'ALL'), false) AND \"place\" = ?")


def test_sum_with_total_rows_kept_and_metric_filter(est: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = est
    cm = compile_metric(reg.get("toy_cases"), "total_all_rows")
    assert cm.sql == (
        'SELECT CASE WHEN count(*) FILTER (WHERE "n" IS NULL) = 0 THEN sum("n") END AS "total_all_rows", '
        'count(*) FILTER (WHERE "n" IS NULL) AS suppressed_rows, string_agg(DISTINCT "n_flag", \', \' ORDER BY '
        '"n_flag") FILTER (WHERE "n" IS NULL) AS flag FROM "toy_cases" WHERE (place = \'A\')'
    )
    assert cm.method == (
        "Sum of cases (cases reported). A group with any suppressed or missing value has no total (never a partial "
        "sum); suppressed_rows gives how many of its values were empty."
    )
    assert eng.query_metric("toy_cases", "total_all_rows")["rows"] == [[None, 1, "S"]]


def test_estimates_are_read_with_their_bounds_and_flag(est: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = est
    cm = compile_metric(reg.get("toy_cases"), "est_value", group_by=["where", "week_ending"],
                        period={"dimension": "week_ending", "start": "2025-01-05"})  # fmt: skip
    assert cm.sql == (
        'SELECT "place" AS "where", "day" AS "week_ending", "est" AS "est_value", "lo" AS "lower", "hi" AS "upper", '
        '"est_flag" AS flag, "est" IS NULL AS not_published FROM "toy_cases" WHERE "day" >= CAST(? AS DATE) '
        "ORDER BY ALL"
    )
    assert cm.params == ["2025-01-05"]
    assert cm.columns == ["where", "week_ending", "est_value", "lower", "upper", "flag", "not_published"]
    assert cm.method == (
        "estimate as published (estimate); not summed. A row whose published cell is empty says so in "
        "not_published (suppressed or not published), never a blank read as a value. Lower and upper are the "
        "publisher's own uncertainty bounds."
    )
    out = eng.query_metric("toy_cases", "est_value", group_by=["where", "week_ending"],
                           period={"dimension": "week_ending", "start": "2025-01-05"})  # fmt: skip
    assert [r[:2] + r[2:] for r in out["rows"]] == [
        ["A", out["rows"][0][1], 2.5, 2.0, 3.0, "*", False],
        ["B", out["rows"][1][1], 0.5, 0.1, 0.9, None, False],
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
    assert out["rows"] == [["2024-25", None, 1, "S"]]  # a suppressed week: no season total
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


def test_rates_refusals_are_plain_words(est: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 12: the refusal names no metric, measure kind or dimension; the next step says what to do."""
    _, eng, _ = est
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("toy_cases", "est_value")
    assert (e.value.code, str(e.value)) == (
        "would_combine_rates",
        "This figure is published separately for each group and is never added up, so one figure can't be given "
        "for all of them together.",
    )
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("toy_cases", "est_value", group_by=["where"])
    assert str(e.value) == (
        "More than one published figure matches some of the groups asked for. Each published figure is read on "
        "its own and never added to another, so nothing is shown rather than one picked at random."
    )
    assert e.value.next_step.startswith("Use describe_dataset")
    one = eng.query_metric("toy_cases", "est_value", filters={"where": "B"})
    assert one["rows"] == [[0.5, 0.1, 0.9, None, False]]


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
            "raw_sha256": list(m.raw_sha256),
            "snapshot_id": m.snapshot_id,
            "contract_version": "0.2.0",
            "review_status": "draft",
            "partial_snapshot": True,
            "licence": "public-domain-us-federal",
            "terms_url": "https://www.cdc.gov/other/agencymaterials.html",
            "citation": ds.entry.citation,
            "data_from": "lake",
            "release_id": None,
            "release_ref": None,
            "release_contract_version": None,
            "release_files": {},
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
    out = Engine(registry=registry, lake=lake).query_metric(
        "toy_cases", "case_count", filters={"condition": "Legionellosis"}
    )
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
    # The latest fetch time, then a hash of the raw bytes, the contract and semantic versions, partial and how the
    # records were read (the adapter type, its config and extensions: ADR 0016).
    method = method_digest(reg.get("toy_cases").entry)
    digest = hashlib.sha256(f"{early.sha256}{late.sha256}|0.2.0|0.2.0|True|{method}".encode()).hexdigest()[
        :12
    ]
    assert m.snapshot_id == f"20261124T120000Z-{digest}"
    found = latest_snapshot(lake, "toy_cases")
    assert found is not None
    path, read = found
    assert read == m
    assert read.files == {name: sha256_file(path / name) for name in ("data.duckdb", "data.parquet")}
    written = json.loads((path / "manifest.json").read_text())
    quality = written.pop("quality")
    # The volume check, then a fill-rate check per optional column that is not a flag column (a flag is empty
    # wherever its count is published); this fixture fills them all, and its contract has no rules.
    assert quality["tool"] == "contract-rules" and quality["rule_errors"] == []
    assert {r["test"]: r["status"] for r in quality["results"]} == {
        "has_rows": "pass",
        **{
            f"fill_rate_{c.name}": "pass"
            for c in reg.get("toy_cases").contract.columns
            if not c.required and c.flag_for is None
        },
    }
    assert written == {
        "files": read.files,
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
        "origin": "lake",
        "release_id": None,
        "release_ref": None,
        "run_date": None,
        "release_contract_version": None,
        "release_files": {},
        "notes": [],
        "silver": {"layout_version": "1", "row_count": 4, "unlisted_codes": {}},
        "layouts": {},
        "method": {"type": "socrata", "digest": method},
    }
    assert (lake / "datasets" / "toy_cases" / "LATEST").read_text() == m.snapshot_id + "\n"
    assert sorted(p.name for p in lake.iterdir()) == [
        "datasets",
        "raw",
    ]  # partial: no raw_records; no temp folder
    assert (path / "data.parquet").stat().st_size > 0
    # The same bytes loaded in full are different inputs, so a new snapshot, never the partial one relabelled.
    again = build_snapshot(lake, reg.get("toy_cases"), [early, late], [f])
    assert again.snapshot_id != m.snapshot_id and again.partial is False
    assert sorted(p.name for p in (lake / "datasets" / "toy_cases").iterdir()) == [
        "LATEST",
        "run_date=2026-11-24",
    ]
    assert sorted(
        p.name for p in (lake / "datasets" / "toy_cases" / "run_date=2026-11-24").iterdir()
    ) == sorted([m.snapshot_id, again.snapshot_id])
    same = build_snapshot(lake, reg.get("toy_cases"), [early, late], [f], partial=True)
    assert same == m  # the same inputs: the existing snapshot, never overwritten


def test_build_snapshot_refusals(est: tuple[Registry, Engine, Path], tmp_path: Path) -> None:
    reg, _, _ = est
    with pytest.raises(ValueError, match="^a snapshot needs at least one fetch$"):
        build_snapshot(tmp_path, reg.get("toy_cases"), [], [])
    f = tmp_path / "bad.ndjson"
    write_ndjson(f, [{**RECS[0], "place": None}, {**RECS[1], "place": None}])
    with pytest.raises(
        PublisherDataRefused,
        match="^toy_cases: the publisher's data has 2 empty values in place, which the contract requires$",
    ):
        build_snapshot(tmp_path, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/x.json", [])], [f])
    assert not list(tmp_path.glob(".toy_cases-*"))
    assert latest_snapshot(tmp_path, "toy_cases") is None


def test_flags_and_whole_counts_load_as_published(est: tuple[Registry, Engine, Path]) -> None:
    _, eng, _ = est
    rows = eng.connect().execute("SELECT place, n, n_flag FROM toy_cases ORDER BY day, place").fetchall()
    assert rows == [("A", 3, None), ("ALL", 70, None), ("A", None, "S"), ("B", 4, None)]
