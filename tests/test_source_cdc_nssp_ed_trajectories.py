"""cdc_nssp_ed_trajectories (NSSP ED visit trajectories, data.cdc.gov rdmq-nq56) through the real catalog.

Synthetic records only (hard rule 5): the values below are made up; the field names and codes are the publisher's.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import quality, tools
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


def _hsa_rows(eng: Engine, metric: str) -> list[tuple[str, float]]:
    out = eng.query_metric(DS, metric, filters={"place": "Michigan"}, group_by=["county", "week_ending"])
    return sorted((r[0], r[2]) for r in out["rows"])


def test_hsa_metrics_give_each_county_its_hsa_figure_never_added_or_mixed_with_the_state(
    real: tuple[Registry, Engine, Path],
) -> None:
    """Kent and Ionia share one HSA figure (9.0): each county shows it once, never 18.0, and the state row stays out."""
    _, eng, _ = real
    for metric in (
        "covid_percent_of_ed_visits_hsa",
        "influenza_percent_of_ed_visits_hsa",
        "rsv_percent_of_ed_visits_hsa",
    ):
        assert _hsa_rows(eng, metric) == [("Ionia", 9.0), ("Kent", 9.0)], metric
    assert _hsa_rows(eng, "combined_percent_of_ed_visits_hsa") == [("Ionia", 6.0), ("Kent", 6.0)]


def test_combined_metric_reads_cdcs_combined_column_never_the_sum_of_the_three(
    real: tuple[Registry, Engine, Path],
) -> None:
    """The fixture's combined (6.0) differs from the sum of the three viruses (7.4 and 6.1) and the smoothed (5.5)."""
    _, eng, _ = real
    out = eng.query_metric(DS, "combined_percent_of_ed_visits", group_by=["place", "week_ending"])
    assert sorted((r[0], r[2]) for r in out["rows"]) == [("Michigan", 6.0), ("United States", 6.0)]


# The contract's quality rules on hand-made staged tables: each rule is shown failing on the case it names.
_GOOD = {
    "week_end": "2024-01-06", "geography": "Michigan", "county": "Kent", "fips": "26081",
    "trend_source": "HSA", "hsa": "Kent - Ionia", "hsa_counties": "Ionia, Kent", "hsa_nci_id": "1",
    "percent_visits_combined": 6.0, "percent_visits_covid": 2.0, "percent_visits_influenza": 3.0,
    "percent_visits_rsv": 1.0, "percent_visits_smoothed_combined": 5.5, "percent_visits_smoothed_covid": 2.0,
    "percent_visits_smoothed_influenza": 3.0, "percent_visits_smoothed_rsv": 1.0,
    "ed_trends_covid": "No Change", "ed_trends_influenza": "Decreasing", "ed_trends_rsv": "Increasing",
    "ed_covid_level": "Low", "ed_flu_level": "High", "ed_rsv_level": "Moderate", "buildnumber": "2024-01-10",
}  # fmt: skip


def _table(rows: list[dict[str, Any]]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    cols = list(_GOOD)

    def kind(c: str) -> str:
        return "DATE" if c == "week_end" else "DOUBLE" if c.startswith("percent_") else "VARCHAR"

    con.execute(f"CREATE TABLE t ({', '.join(f'{c} {kind(c)}' for c in cols)})")
    for r in rows:
        con.execute(f"INSERT INTO t VALUES ({', '.join('?' for _ in cols)})", [r[c] for c in cols])
    return con


def _status(rows: list[dict[str, Any]], rule_id: str) -> str:
    reg = load_registry(ROOT)
    rules = [r for r in quality.rules_for(reg.get(DS).contract) if r.id == rule_id]
    assert len(rules) == 1, rule_id
    return str(quality.run_rules(_table(rows), "t", rules).results[0]["status"])


def _state(**kw: Any) -> dict[str, Any]:
    return {**_GOOD, "county": "All", "fips": "26000", "trend_source": "State", "hsa": "All",
            "hsa_counties": "All", "hsa_nci_id": "All", **kw}  # fmt: skip


def test_hsa_rule_fails_when_one_county_has_a_figure_and_another_is_empty() -> None:
    kent, ionia = dict(_GOOD), {**_GOOD, "county": "Ionia", "fips": "26067"}
    assert _status([kent, ionia], "counties_of_one_hsa_share_its_values") == "pass"
    assert (
        _status([kent, {**ionia, "percent_visits_covid": None}], "counties_of_one_hsa_share_its_values")
        == "fail"
    )
    for col, other in (("percent_visits_smoothed_rsv", 4.0), ("ed_covid_level", "Very High")):
        assert _status([kent, {**ionia, col: other}], "counties_of_one_hsa_share_its_values") == "fail", col


def test_hsa_rule_leaves_rows_without_an_hsa_code_to_their_own_rule() -> None:
    """Two unrelated HSAs with no code must not be reported as one HSA whose counties disagree."""
    a = {**_GOOD, "hsa_nci_id": None}
    b = {**_GOOD, "county": "Wayne", "fips": "26163", "hsa_nci_id": None, "percent_visits_covid": 7.0}
    assert _status([a, b], "counties_of_one_hsa_share_its_values") == "pass"
    assert _status([a, b], "hsa_rows_carry_their_hsa_code") == "warn"


def test_a_renamed_trend_source_fails_rather_than_emptying_the_answers() -> None:
    assert _status([_GOOD, _state()], "trend_source_is_a_declared_value") == "pass"
    assert (
        _status([{**_GOOD, "trend_source": "Health Service Area"}], "trend_source_is_a_declared_value")
        == "fail"
    )
    assert _status([_state()], "some_hsa_rows_present") == "warn"


def test_total_rows_and_the_metrics_filter_pick_the_same_rows() -> None:
    rule = "total_rows_are_the_state_and_national_rows"
    assert _status([_GOOD, _state()], rule) == "pass"
    assert _status([{**_GOOD, "trend_source": "State"}], rule) == "fail"  # a named county marked State
    assert _status([_state(trend_source="HSA")], rule) == "fail"  # an All row marked HSA


def test_each_virus_range_rule_names_its_own_columns() -> None:
    assert (
        _status([{**_GOOD, "percent_visits_smoothed_rsv": 101.0}], "rsv_percents_between_0_and_100") == "fail"
    )
    assert (
        _status([{**_GOOD, "percent_visits_smoothed_rsv": 101.0}], "covid_percents_between_0_and_100")
        == "pass"
    )


def test_a_week_that_does_not_end_on_saturday_is_named() -> None:
    assert _status([_GOOD], "week_end_is_a_saturday") == "pass"
    assert _status([{**_GOOD, "week_end": "2024-01-07"}], "week_end_is_a_saturday") == "warn"
