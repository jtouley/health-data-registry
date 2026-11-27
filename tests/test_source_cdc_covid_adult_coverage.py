"""cdc_covid_adult_coverage (NIS-FRVM COVID-19 vaccination coverage among adults, data.cdc.gov ksfb-ug5d) through the
real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names, labels and the way Socrata
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
from health_data_registry.tools import series_for
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_covid_adult_coverage"
URL = "https://data.cdc.gov/resource/ksfb-ug5d.json"


def rec(place: str, level: str, week: str, *, group: tuple[str, str] = ("Overall", "18+ years"),
        indicator: tuple[str, str] = ("Up-to-date", "Yes"), est: str | None = None, hw: str | None = None,
        n: str = "500", flag: str = "0") -> dict[str, str]:  # fmt: skip
    """One Socrata row; estimate and half width only when CDC published them (a suppressed row leaves both out)."""
    r = {
        "vaccine": "COVID",
        "geographic_level": level,
        "geographic_name": place,
        "demographic_level": group[0],
        "demographic_name": group[1],
        "indicator_label": indicator[0],
        "indicator_category_label": indicator[1],
        "month_week": "November Week 1",
        "week_ending": f"{week}T00:00:00.000",
        "unweighted_sample_size": n,
        "current_season_week_ending": f"{week}T00:00:00.000",
        "covid_season": "2025-2026",
        "suppression_flag": flag,
    }
    if est is not None:
        r |= {"estimate": est, "ci_half_width_95pct": hw or "1.0"}
    return r


RECORDS = [
    rec("National", "National", "2025-11-01", est="12.5", hw="0.8", n="12000"),
    rec("National", "National", "2025-11-08", est="14.0", hw="0.7", n="13000"),
    rec("Michigan", "State", "2025-11-01", est="10.1", hw="3.2", n="400"),
    rec(
        "Michigan",
        "State",
        "2025-11-01",
        indicator=("4-level vaccination and intent", "Definitely will get a vaccine"),
        est="9.0",
        hw="2.5",
        n="400",
    ),  # fmt: skip
    rec("Ohio", "State", "2025-11-01", n="12", flag="1"),
    rec(
        "National",
        "National",
        "2025-11-01",
        group=("Age (3 Level)", "65+ years"),
        est="30.2",
        hw="1.5",
        n="4000",
    ),
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


def test_a_plain_disease_question_never_gets_vaccination_coverage() -> None:
    reg = load_registry(ROOT)
    for question in ("COVID-19", "COVID-19 in Michigan", "COVID"):
        assert [s.label for d, s in series_for(reg, question) if d.id == DS] == [], question


def test_a_vaccination_question_gets_the_coverage_series() -> None:
    reg = load_registry(ROOT)
    labels = [s.label for d, s in series_for(reg, "COVID-19 vaccination") if d.id == DS]
    assert len(labels) == 2 and all("vaccination coverage" in x for x in labels)


def test_a_county_question_is_declined() -> None:
    reg = load_registry(ROOT)
    q = "COVID-19 vaccination coverage by county in Michigan"
    row = next(r for r in _rows(tools.suggest_datasets(reg, q)) if r["source_id"] == DS)
    assert row["can_answer"] == "no"
    assert "not county" in row["why"]


def test_get_series_reads_each_estimate_as_published(real: tuple[Registry, Engine, Path, Any]) -> None:
    reg, eng, _, _ = real
    out = tools.get_series(
        reg, eng, condition="COVID-19 vaccination", locations=["United States", "Michigan", "Ohio"], source_id=DS,
        fmt="json",
    )  # fmt: skip
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    adults = sorted(
        (r[at["location"]], str(r[at["year"]]), r[at["value"]], r[at["flag"]])
        for r in out["rows"]
        if "18 and older" in r[at["condition"]]
    )
    # The intent row (9.0) is a different measure and never shows here; Ohio's suppressed estimate is empty, with
    # CDC's own flag beside it, never a zero.
    assert [a[:3] for a in adults] == [
        ("Michigan", "2025-11-01", 10.1),
        ("Ohio", "2025-11-01", None),
        ("United States", "2025-11-01", 12.5),
        ("United States", "2025-11-08", 14.0),
    ]
    assert "1" in str([a[3] for a in adults if a[0] == "Ohio"][0])
    assert {r[at["measure"]] for r in out["rows"]} == {"percent"}


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
    "geography_levels": [*RECORDS, rec("Ohio", "County", "2025-11-08", est="11.0")],
    "half_width_not_negative": [*RECORDS, rec("Ohio", "State", "2025-11-08", est="11.0", hw="-1.0")],
    "indicator_labels": [*RECORDS, rec("Iowa", "State", "2025-11-08", indicator=("Received", "Yes"), est="11.0")],
    "flagged_rows_carry_no_estimate": [*RECORDS, rec("Iowa", "State", "2025-11-08", est="11.0", flag="1")],
    "interval_with_every_estimate": [*RECORDS, {**rec("Utah", "State", "2025-11-08", est="11.0"), "ci_half_width_95pct": ""}],
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
        _build(tmp_path, [*RECORDS, rec("Iowa", "State", "2025-11-01", est="100.5")])
