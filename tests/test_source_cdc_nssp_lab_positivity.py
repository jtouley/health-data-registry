"""cdc_nssp_lab_positivity (NSSP commercial laboratory tests for COVID-19, influenza and RSV, data.cdc.gov jqxy-cyu2)
through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names, codes and the way Socrata
leaves a suppressed field out of a row are the publisher's.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import tools
from health_data_registry.quality import QualityBlocked
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_nssp_lab_positivity"
URL = "https://data.cdc.gov/resource/jqxy-cyu2.json"


def rec(pathogen: str, end: str, place: str, **counts: str) -> dict[str, str]:
    """One Socrata row: the week's Sunday and Saturday, and only the counts CDC published (suppressed ones left out)."""
    start = {"2024-11-02": "2024-10-27", "2024-11-09": "2024-11-03"}[end]
    return {
        "pathogen": pathogen,
        "mmwr_week_start": f"{start}T00:00:00.000",
        "mmwr_week_end": f"{end}T00:00:00.000",
        "geography_type": "state",
        "geography_value": place,
        **counts,
    }


RECORDS = [
    rec("Influenza", "2024-11-02", "MI", test_count="250.0", positive_count="20.0", percent_pos="8.0"),
    rec(
        "Influenza", "2024-11-09", "MI", test_count="120.0"
    ),  # positives under 10: count and percent suppressed
    rec("Influenza", "2024-11-02", "OH", test_count="80.0", positive_count="12.0", percent_pos="15.0"),
    rec("RSV", "2024-11-02", "MI", test_count="40.0", positive_count="0.0", percent_pos="0.0"),  # a real zero
    rec("RSV", "2024-11-09", "MI", test_count="0.0", positive_count="0.0"),  # no test: nothing to divide by
    rec("COVID-19", "2024-11-02", "MI", positive_count="0.0"),  # tests under 10: suppressed
    rec("COVID-19", "2024-11-02", "OH", test_count="300.0", positive_count="30.0", percent_pos="10.0"),
]


def _build(tmp_path: Path, records: list[dict[str, str]]) -> tuple[Registry, Engine, Path, Any]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, records)
    m = build_snapshot(lake, reg.get(DS), [raw_fetch(URL, records)], [f])
    return reg, Engine(registry=reg, lake=lake), lake, m


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path, Any]:
    return _build(tmp_path, RECORDS)


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


def _mine(question: str) -> dict[str, str]:
    return next(
        r for r in _rows(tools.suggest_datasets(load_registry(ROOT), question)) if r["source_id"] == DS
    )


def test_a_state_question_about_rsv_test_positivity_routes_here() -> None:
    row = _mine("RSV test positivity in Michigan")
    assert row["can_answer"] == "yes"


def test_a_national_question_is_told_the_table_is_by_state_only() -> None:
    row = _mine("influenza test positivity nationally")
    assert row["can_answer"] == "no"
    assert "state only, not nation" in row["why"]


def test_a_county_question_is_told_the_table_is_by_state_only() -> None:
    row = _mine("COVID-19 test positivity by county in Michigan")
    assert row["can_answer"] == "no"
    assert "not county" in row["why"]


def test_get_series_reads_each_published_percent_by_state_and_never_adds(
    real: tuple[Registry, Engine, Path, Any],
) -> None:
    reg, eng, _, _ = real
    out = tools.get_series(
        reg, eng, condition="Influenza", locations=["Michigan", "Ohio"], source_id=DS, fmt="json"
    )
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    got = sorted((r[at["location"]], str(r[at["year"]]), r[at["value"]]) for r in out["rows"])
    # Michigan's 9 November percent was suppressed: it comes back empty and says so, never as 0 or as a sum.
    assert got == [
        ("Michigan", "2024-11-02", 8.0),
        ("Michigan", "2024-11-09", None),
        ("Ohio", "2024-11-02", 15.0),
    ]
    suppressed = next(r for r in out["rows"] if r[at["value"]] is None)
    assert "suppressed or not published" in suppressed[at["period_status"]]
    assert {r[at["source_id"]] for r in out["rows"]} == {DS}
    assert {r[at["measure"]] for r in out["rows"]} == {"percent"}


def test_a_published_zero_stays_zero_and_no_tests_stays_empty(
    real: tuple[Registry, Engine, Path, Any],
) -> None:
    reg, eng, _, _ = real
    out = tools.get_series(reg, eng, condition="RSV", locations=["MI"], source_id=DS, fmt="json")
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    assert sorted((str(r[at["year"]]), r[at["value"]]) for r in out["rows"]) == [
        ("2024-11-02", 0.0),
        ("2024-11-09", None),
    ]


def test_suppressed_counts_are_empty_not_zero(real: tuple[Registry, Engine, Path, Any]) -> None:
    _, eng, _, _ = real
    out = tools.query_sql(
        eng,
        f"SELECT geography_value, mmwr_week_end, test_count, positive_count, percent_pos FROM {DS} "
        "WHERE pathogen = 'COVID-19' AND geography_value = 'MI'",
        fmt="json",
    )
    assert isinstance(out, dict), out
    assert [r[2:] for r in out["rows"]] == [[None, 0, None]]


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path, Any]) -> None:
    """Hard rule 15: the raw records dbt reads hold every value exactly as the publisher sent it (text)."""
    _, _, lake, _ = real
    files = list((lake / "raw_records" / DS).glob("*.ndjson"))
    assert len(files) == 1
    kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent


def test_every_rule_passes_on_publisher_shaped_rows(real: tuple[Registry, Engine, Path, Any]) -> None:
    _, _, _, m = real
    assert {r["status"] for r in m.quality["results"]} == {"pass"}
    assert m.quality["rule_errors"] == []


def test_each_rule_fires_on_the_case_it_is_written_for(tmp_path: Path) -> None:
    """Every contract rule is shown catching its own bad case, not only passing on good data."""
    bad = [
        *RECORDS,
        rec("Influenza", "2024-11-09", "OH", test_count="20.0", positive_count="25.0"),
        rec("hMPV", "2024-11-09", "OH", test_count="5.0", positive_count="0.0", percent_pos="0.0"),
        {**rec("RSV", "2024-11-09", "OH", test_count="30.0"), "mmwr_week_end": "2024-11-08T00:00:00.000"},
        {
            **rec("RSV", "2024-11-02", "OH", test_count="30.0", percent_pos="10.0"),
            "geography_type": "hhs_region",
        },
        rec("COVID-19", "2024-11-09", "OH", test_count="250.0", positive_count="30.0"),  # counts, no percent
    ]
    _, _, _, m = _build(tmp_path, bad)
    status = {r["test"]: r["status"] for r in m.quality["results"]}
    for rule in (
        "positives_within_tests",
        "pathogen_codes",
        "small_counts_suppressed",
        "week_is_sunday_to_saturday",
        "every_row_is_a_state",
        "percent_suppressed_with_its_counts",
        "counts_published_with_their_percent",
    ):
        assert status[rule] == "warn", rule


def test_a_percent_over_100_blocks_the_copy(tmp_path: Path) -> None:
    """The one error-severity rule: a percent outside 0 to 100 stops this copy being served."""
    bad = [
        *RECORDS,
        rec("Influenza", "2024-11-09", "OH", test_count="20.0", positive_count="20.0", percent_pos="100.5"),
    ]
    with pytest.raises(QualityBlocked, match="percent_between_0_and_100"):
        _build(tmp_path, bad)


def test_a_virus_with_no_percent_is_reported(tmp_path: Path) -> None:
    """RSV's rows are still there but none has a percent: the column-level checks pass, and only the per-virus
    rule notices that RSV's questions would go unanswered."""
    rsv_empty = [
        {k: v for k, v in r.items() if k not in ("test_count", "positive_count", "percent_pos")}
        if r["pathogen"] == "RSV"
        else r
        for r in RECORDS
    ]
    assert any(r["pathogen"] == "RSV" for r in rsv_empty)
    _, _, _, m = _build(tmp_path, rsv_empty)
    status = {r["test"]: r["status"] for r in m.quality["results"]}
    assert status["every_virus_has_a_percent"] == "warn"
    assert status["percent_between_0_and_100"] == "pass"
    assert status["fill_rate_percent_pos"] == "pass"
