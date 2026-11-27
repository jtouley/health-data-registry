"""nchs_resp_deaths_pct (NCHS provisional percent of deaths for COVID-19, influenza and RSV, data.cdc.gov 53g5-jf7x)
through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names, labels, NCHS's notes and the
way Socrata leaves a suppressed field out of a row are the publisher's.
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
DS = "nchs_resp_deaths_pct"
URL = "https://data.cdc.gov/resource/53g5-jf7x.json"
NOTE = (
    "One or more data cells have counts between 1-9 and have been suppressed in accordance with NCHS "
    "confidentiality standards."
)


def rec(cause: str, kind: str, group: str, end: str, total: str, **cells: str) -> dict[str, str]:
    """One Socrata row; deaths, percent_deaths, provisional and suppressed only when NCHS published them."""
    start = {"2025-12-27": "2025-12-21", "2026-01-03": "2025-12-28"}[end]
    return {
        "data_as_of": "2026-01-07T00:00:00.000",
        "start_date": f"{start}T00:00:00.000",
        "end_date": f"{end}T00:00:00.000",
        "year": "2025",
        "month": "12",
        "mmwr_week": {"2025-12-27": "52", "2026-01-03": "53"}[end],
        "weekending_date": f"{end}T00:00:00.000",
        "state": "United States",
        "demographic_type": kind,
        "demographic_values": group,
        "pathogen": cause,
        "total_deaths": total,
        **cells,
    }


RECORDS = [
    rec("COVID-19", "Age Group", "65+ years", "2025-12-27", "40000.0", deaths="800.0", percent_deaths="2.0"),
    rec(
        "COVID-19",
        "Age Group",
        "65+ years",
        "2026-01-03",
        "41000.0",
        deaths="820.0",
        percent_deaths="2.0",
        provisional="(provisional)",
    ),  # fmt: skip
    rec(
        "COVID-19",
        "Age Group",
        "0-17 years",
        "2026-01-03",
        "500.0",
        suppressed=NOTE,
        provisional="(provisional)",
    ),
    rec(
        "Influenza", "Age Group", "65+ years", "2026-01-03", "41000.0", deaths="1230.0", percent_deaths="3.0"
    ),
    rec("RSV", "Sex", "Female", "2026-01-03", "20000.0", deaths="200.0", percent_deaths="1.0"),
    rec("Combined", "Race/Ethnicity", "AI/AN, NH", "2026-01-03", "0.0", deaths="0.0"),
]
AGES = {"0-17 years": "1000.0", "18-64 years": "20000.0", "65+ years": "40000.0"}
# Each cause in each age group the series read, with a percent, in the week before (every_age_series_has_a_percent).
SERIES_WEEK = [
    rec(
        cause,
        "Age Group",
        group,
        "2025-12-27",
        total,
        deaths=f"{float(total) / 50:.1f}",
        percent_deaths="2.0",
    )
    for cause in ("COVID-19", "Influenza", "RSV", "Combined")
    for group, total in AGES.items()
    if not (cause == "COVID-19" and group == "65+ years")  # already in RECORDS
]
RECORDS += SERIES_WEEK


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


def test_a_plain_disease_question_never_gets_a_percent_of_deaths() -> None:
    reg = load_registry(ROOT)
    for question in ("COVID-19", "Influenza", "RSV"):
        assert [s.label for d, s in series_for(reg, question) if d.id == DS] == [], question


@pytest.mark.parametrize(
    ("question", "cause"),
    [
        ("COVID-19 deaths", "COVID-19"),
        ("COVID-19 mortality", "COVID-19"),
        ("deaths from COVID-19", "COVID-19"),
        ("influenza death", "Influenza"),
        ("RSV deaths", "RSV"),
        ("deaths involving RSV", "RSV"),
        ("combined respiratory virus deaths", "Combined"),
    ],
)
def test_a_deaths_question_gets_one_series_per_age_group_of_its_own_cause(question: str, cause: str) -> None:
    """However the deaths are asked about, only the cause named answers: an RSV question never also gets the
    Combined series, whose label holds "RSV" too."""
    reg = load_registry(ROOT)
    found = [s for d, s in series_for(reg, question) if d.id == DS]
    assert len(found) == 3, question
    assert {s.filters["cause"] for s in found} == {cause}


def test_a_bare_deaths_question_names_no_cause() -> None:
    reg = load_registry(ROOT)
    for question in ("deaths", "mortality"):
        assert [s for d, s in series_for(reg, question) if d.id == DS] == [], question


def test_suggest_datasets_keeps_to_the_same_gate_as_get_series() -> None:
    """A plain disease question isn't offered a percent of deaths it then couldn't get; a deaths question is."""
    plain = _mine("COVID-19")
    assert plain["can_answer"] == "no"
    assert "asks about" in plain["why"]
    assert _mine("COVID-19 deaths")["can_answer"] == "yes"


def test_a_state_question_is_told_the_table_is_national() -> None:
    row = _mine("COVID-19 deaths in Ohio")
    assert row["can_answer"] == "no"
    assert "nation only, not state" in row["why"]


def test_get_series_reads_each_published_percent_and_never_adds(
    real: tuple[Registry, Engine, Path, Any],
) -> None:
    reg, eng, _, _ = real
    out = tools.get_series(reg, eng, condition="COVID-19 deaths", source_id=DS, fmt="json")
    assert isinstance(out, dict), out
    at = {c: i for i, c in enumerate(out["columns"])}
    got = sorted((r[at["condition"]], str(r[at["year"]]), r[at["value"]]) for r in out["rows"])
    old = [g for g in got if "65 and older" in g[0]]
    assert [(y, v) for _, y, v in old] == [("2025-12-27", 2.0), ("2026-01-03", 2.0)]
    young = [r for r in out["rows"] if "0 to 17" in r[at["condition"]] and str(r[at["year"]]) == "2026-01-03"]
    assert [r[at["value"]] for r in young] == [None]
    # The suppressed cell keeps NCHS's own note beside it, and says it is empty: never a zero.
    assert young[0][at["flag"]] and "suppressed" in young[0][at["flag"]]
    assert "suppressed or not published" in young[0][at["period_status"]]
    assert {r[at["measure"]] for r in out["rows"]} == {"percent"}


def test_a_place_is_declined_because_the_figure_is_national(real: tuple[Registry, Engine, Path, Any]) -> None:
    reg, eng, _, _ = real
    out = tools.get_series(
        reg, eng, condition="Influenza deaths", locations=["Ohio"], source_id=DS, fmt="json"
    )
    assert isinstance(out, dict)
    assert out["rows"] == []


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
    bad = [
        *RECORDS,
        rec("Flu", "Age Group", "18-64 years", "2026-01-03", "9000.0", deaths="5.0", percent_deaths="0.06"),
        rec("RSV", "Region", "HHS 5", "2026-01-03", "100.0", deaths="200.0", percent_deaths="50.0"),
        rec(
            "RSV",
            "Sex",
            "Male",
            "2026-01-03",
            "20000.0",
            deaths="30.0",
            percent_deaths="0.15",
            suppressed=NOTE,
        ),
        {
            **rec("Combined", "Sex", "Male", "2025-12-27", "20000.0", deaths="40.0", percent_deaths="0.2"),
            "state": "Ohio",
            "weekending_date": "2025-12-26T00:00:00.000",
        },  # fmt: skip
    ]
    _, _, _, m = _build(tmp_path, bad)
    status = {r["test"]: r["status"] for r in m.quality["results"]}
    for rule in (
        "pathogen_codes",
        "small_counts_suppressed",
        "demographic_types",
        "deaths_within_all_deaths",
        "suppressed_rows_carry_no_value",
        "national_rows_only",
        "week_ends_on_a_saturday",
        "demographic_groups",
    ):
        assert status[rule] == "warn", rule


def test_a_relabelled_age_group_is_reported(tmp_path: Path) -> None:
    """NCHS renames '65+ years': every column-level check still passes, but each 65+ series would now be empty,
    so the per-series rule warns and the new label is outside the published groups."""
    renamed = [
        {**r, "demographic_values": "65 years and over"} if r["demographic_values"] == "65+ years" else r
        for r in RECORDS
    ]
    _, _, _, m = _build(tmp_path, renamed)
    status = {r["test"]: r["status"] for r in m.quality["results"]}
    assert status["every_age_series_has_a_percent"] == "warn"
    assert status["demographic_groups"] == "warn"
    assert status["fill_rate_percent_deaths"] == "pass"


def test_a_percent_over_100_blocks_the_copy(tmp_path: Path) -> None:
    bad = [*RECORDS, rec("RSV", "Sex", "Male", "2026-01-03", "100.0", deaths="100.0", percent_deaths="100.5")]
    with pytest.raises(QualityBlocked, match="percent_between_0_and_100"):
        _build(tmp_path, bad)
