"""cdc_rsv_adult_coverage (NIS-FRVM RSV vaccination coverage among adults 75+ and 50-74 at high risk, data.cdc.gov
qeq7-f3ir) through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names, labels, the estimate's
published scale (a proportion) and the way Socrata leaves a suppressed field out of a row are the publisher's.
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
from health_data_registry.tools import series_for
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_rsv_adult_coverage"
URL = "https://data.cdc.gov/resource/qeq7-f3ir.json"
OLD, RISK = "75+ years", "50-74 years (high risk)"


def rec(age: str, place: str, week: str, *, group: tuple[str, str] | None = None,
        indicator: tuple[str, str] = ("Up-to-date", "Yes"), est: str | None = None, hw: str | None = None,
        n: str = "900", flag: str = "0", level: str | None = None) -> dict[str, str]:  # fmt: skip
    r = {
        "vaccine": "RSV",
        "age_group": age,
        "geography_level": level or ("National" if place == "National" else "Region"),
        "geography_name": place,
        "demographic_level": (group or ("Overall", age))[0],
        "demographic_name": (group or ("Overall", age))[1],
        "indicator_label": indicator[0],
        "indicator_category_label": indicator[1],
        "month_week": "November Week 1",
        "week_ending": f"{week}T00:00:00.000",
        "unweighted_sample_size": n,
        "suppression_flag": flag,
    }
    if est is not None:
        r |= {"estimate": est, "ci_half_width_95pct": hw or "2.0"}
    return r


RECORDS = [
    rec(OLD, "National", "2025-11-01", est="0.408344", hw="1.9044", n="18154"),
    rec(OLD, "National", "2025-11-08", est="0.41", hw="1.8", n="19000"),
    rec(OLD, "Region 5", "2025-11-01", est="0.39903", hw="3.46", n="1064"),
    rec(RISK, "National", "2025-11-01", est="0.243274", hw="2.98", n="5025"),
    rec(
        RISK,
        "National",
        "2025-11-01",
        indicator=("4-level vaccination and intent", "Definitely will get a vaccine"),
        est="0.057686",
        hw="2.88",
        n="5025",
    ),  # fmt: skip
    rec(RISK, "Region 8", "2025-11-01", group=("Sex", "Female"), n="23", flag="1"),
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


def test_a_plain_rsv_question_never_gets_vaccination_coverage() -> None:
    reg = load_registry(ROOT)
    for question in ("RSV", "RSV in adults 75 and older"):
        assert [s.label for d, s in series_for(reg, question) if d.id == DS] == [], question


def test_a_vaccination_question_gets_both_age_groups() -> None:
    reg = load_registry(ROOT)
    labels = sorted(s.label for d, s in series_for(reg, "RSV vaccination") if d.id == DS)
    assert len(labels) == 2
    assert all("a proportion from 0 to 1" in x for x in labels)


def test_a_state_question_is_told_there_are_no_state_figures() -> None:
    reg = load_registry(ROOT)
    row = next(
        r for r in _rows(tools.suggest_datasets(reg, "RSV vaccination in Michigan")) if r["source_id"] == DS
    )
    assert row["can_answer"] == "no"
    assert "not state" in row["why"]


def test_get_series_reads_the_estimate_as_published_never_rescaled(
    real: tuple[Registry, Engine, Path, Any],
) -> None:
    reg, eng, _, _ = real
    out = tools.get_series(
        reg,
        eng,
        condition="RSV vaccination",
        locations=["United States", "Region 5"],
        source_id=DS,
        fmt="json",
    )
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    old = sorted(
        (r[at["location"]], str(r[at["year"]]), r[at["value"]])
        for r in out["rows"]
        if "75 and older" in r[at["condition"]]
    )
    assert old == [
        ("Region 5", "2025-11-01", 0.39903),
        ("United States", "2025-11-01", 0.408344),
        ("United States", "2025-11-08", 0.41),
    ]
    risk = [r[at["value"]] for r in out["rows"] if "high-risk" in r[at["condition"]]]
    assert risk == [0.243274]  # the intent row (0.057686) is a different measure and never shows here
    # A proportion is never labelled percent: 0.41 beside "percent" would read as 0.41%.
    assert {r[at["measure"]] for r in out["rows"]} == {"estimate"}


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path, Any]) -> None:
    _, _, lake, _ = real
    (f,) = (lake / "raw_records" / DS).glob("*.ndjson")
    kept = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent


def test_every_rule_passes_on_publisher_shaped_rows(real: tuple[Registry, Engine, Path, Any]) -> None:
    _, _, _, m = real
    assert {r["status"] for r in m.quality["results"]} == {"pass"}
    assert m.quality["rule_errors"] == []


# One bad row per rule, so each rule is shown firing on its own case and no other rule fires with it.
FIRES_ALONE = {
    "age_groups": [*RECORDS, rec("60+ years", "Region 1", "2025-11-08", est="0.3")],
    "geography_levels": [*RECORDS, rec(OLD, "Region 1", "2025-11-08", est="0.3", level="County")],
    "half_width_not_negative": [*RECORDS, rec(OLD, "Region 1", "2025-11-08", est="0.3", hw="-1.0")],
    "indicator_labels": [*RECORDS, rec(OLD, "Region 2", "2025-11-08", indicator=("Received", "Yes"), est="0.3")],
    "flagged_rows_carry_no_estimate": [*RECORDS, rec(OLD, "Region 2", "2025-11-08", est="0.3", flag="1")],
    "interval_with_every_estimate": [*RECORDS, {**rec(OLD, "Region 3", "2025-11-08", est="0.3"), "ci_half_width_95pct": ""}],
    # CDC moving the estimate to percent (40.8 for 0.408) passes the 0-to-100 range but not the declared scale.
    "estimate_on_published_proportion_scale": [*RECORDS, rec(OLD, "Region 4", "2025-11-08", est="40.8")],
    # Every Up-to-date 'Yes' row suppressed: the column-wide fill check still passes on the intent row.
    "coverage_rows_have_estimates": [
        r if r["indicator_label"] != "Up-to-date" else {k: v for k, v in r.items() if k not in ("estimate", "ci_half_width_95pct")}
        for r in RECORDS
    ],
}  # fmt: skip


@pytest.mark.parametrize("rule", sorted(FIRES_ALONE))
def test_each_rule_fires_on_the_case_it_is_written_for(tmp_path: Path, rule: str) -> None:
    _, _, _, m = _build(tmp_path, FIRES_ALONE[rule])
    status = {r["test"]: r["status"] for r in m.quality["results"]}
    assert status[rule] == "warn", status
    assert {t: v for t, v in status.items() if t != rule and v != "pass"} == {}
    assert m.quality["rule_errors"] == []


def test_an_estimate_over_100_blocks_the_copy(tmp_path: Path) -> None:
    with pytest.raises(QualityBlocked, match="estimate_within_0_to_100"):
        _build(tmp_path, [*RECORDS, rec(OLD, "Region 4", "2025-11-01", est="101")])
