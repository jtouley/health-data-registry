"""cdc_nssp_ed_trajectories (NSSP ED visit trajectories, data.cdc.gov rdmq-nq56) through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names and codes are the publisher's.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest

from health_data_registry import tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_nssp_ed_trajectories"
WEEK = "2024-01-06T00:00:00.000"


def _row(
    geography: str, county: str, fips: str, source: str, covid: str, flu: str, rsv: str
) -> dict[str, str]:
    hsa = "All" if county == "All" else "Kent (Grand Rapids), MI - Ionia, MI"
    return {
        "week_end": WEEK, "geography": geography, "county": county, "fips": fips, "trend_source": source,
        "hsa": hsa, "hsa_counties": "All" if county == "All" else "Ionia, Kent", "hsa_nci_id": "All" if county == "All" else "1",
        "percent_visits_covid": covid, "percent_visits_influenza": flu, "percent_visits_rsv": rsv,
        "percent_visits_combined": "6.0", "percent_visits_smoothed": "5.5", "percent_visits_smoothed_covid": "2.0",
        "percent_visits_smoothed_1": "3.0", "percent_visits_smoothed_rsv": "1.0",
        "ed_trends_covid": "No Change", "ed_trends_influenza": "Decreasing", "ed_trends_rsv": "Increasing",
        "ed_covid_level": "Low", "ed_flu_level": "High", "ed_rsv_level": "Moderate", "buildnumber": "2024-01-10",
    }  # fmt: skip


RECORDS = [
    _row("United States", "All", "0", "United States", "2.0", "3.0", "1.10"),
    _row("Michigan", "All", "26000", "State", "2.5", "3.5", "1.40"),
    # Two counties of one Health Service Area: the same shared figure, never added to the state's.
    _row("Michigan", "Kent", "26081", "HSA", "9.0", "9.0", "9.00"),
    _row("Michigan", "Ionia", "26067", "HSA", "9.0", "9.0", "9.00"),
]


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path]:
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake, reg.get(DS), [raw_fetch("https://data.cdc.gov/resource/rdmq-nq56.json", RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake), lake


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


def test_a_plain_question_about_ed_visits_routes_here() -> None:
    reg = load_registry(ROOT)
    rows = _rows(tools.suggest_datasets(reg, "RSV emergency department visits in Michigan"))
    assert rows[0]["source_id"] == DS
    assert rows[0]["can_answer"] == "yes"


def test_state_and_national_rows_are_read_as_published_never_from_counties(
    real: tuple[Registry, Engine, Path],
) -> None:
    reg, eng, _ = real
    out = tools.get_series(
        reg, eng, condition="RSV", locations=["Michigan", "United States"], source_id=DS, fmt="json"
    )
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    got = sorted((r[at["location"]], str(r[at["year"]]), r[at["value"]]) for r in out["rows"])
    assert got == [("Michigan", "2024-01-06", 1.4), ("United States", "2024-01-06", 1.1)]


def test_each_virus_reads_its_own_column(real: tuple[Registry, Engine, Path]) -> None:
    reg, eng, _ = real
    for condition, want in (("COVID-19", 2.5), ("Influenza", 3.5), ("RSV", 1.4)):
        out = tools.get_series(
            reg, eng, condition=condition, locations=["Michigan"], source_id=DS, fmt="json"
        )
        assert isinstance(out, dict), out
        at = {c: i for i, c in enumerate(out["columns"])}
        assert [r[at["value"]] for r in out["rows"]] == [want], condition


def test_raw_layer_keeps_the_publisher_values_as_sent(real: tuple[Registry, Engine, Path]) -> None:
    """Hard rule 15: FIPS without its leading zero and percents as text, exactly as CDC sends them."""
    _, _, lake = real
    files = list((lake / "raw_records" / DS).glob("*.ndjson"))
    assert len(files) == 1
    kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(RECORDS, kept, strict=True):
        assert {k: row[k] for k in sent} == sent
