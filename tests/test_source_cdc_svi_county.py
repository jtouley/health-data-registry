"""cdc_svi_county (CDC/ATSDR Social Vulnerability Index 2022, US counties) through the real catalog.

A covariate (hard rule 11, ADR 0019): set beside an infectious-disease rate, never an answer of its own and never a
denominator. Synthetic rows only (hard rule 5): the values are made up; the column names, the -999 missing code and
the byte-order mark are the publisher's.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import quality, tools
from health_data_registry.adapters import with_document
from health_data_registry.adapters.https_file import read_records
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, RegistryError, _check_covariate, load_registry
from health_data_registry.source_config import HttpsFileConfig
from health_data_registry.store import RawFetch, build_snapshot, write_ndjson

ROOT = Path(__file__).resolve().parents[1]
DS = "cdc_svi_county"
URL = "https://svi.cdc.gov/Documents/Data/2022/csv/states_counties/SVI_2022_US_county.csv"
HEADER = [
    "ST", "STATE", "ST_ABBR", "STCNTY", "COUNTY", "FIPS", "LOCATION", "AREA_SQMI", "E_TOTPOP", "M_TOTPOP",
    "RPL_THEME1", "RPL_THEME2", "RPL_THEME3", "RPL_THEME4", "RPL_THEMES", "F_TOTAL", "E_DAYPOP",
]  # fmt: skip
ROWS = [
    ["01", "Alabama", "AL", "01001", "Synthetic County", "01001", "Synthetic County, Alabama", "594.45", "50000",
     "0", "0.25", "0.5", "0.75", "0.1", "0.4", "1", "41000"],
    ["26", "Michigan", "MI", "26999", "Example County", "26999", "Example County, Michigan", "12.0", "-999",
     "-999", "-999", "0", "1", "0.4", "-999", "-999", "900"],
    ["09", "Connecticut", "CT", "09110", "Sample Planning Region", "09110", "Sample Planning Region, Connecticut",
     "100.5", "800000", "0", "0.0", "1.0", "0.333", "0.5", "0.5", "0", "750000"],
]  # fmt: skip


def _csv_bytes() -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(HEADER)
    w.writerows(ROWS)
    return buf.getvalue().encode("utf-8-sig")  # the publisher's file starts with a byte-order mark


@pytest.fixture
def real(tmp_path: Path) -> tuple[Registry, Engine, Path, list[dict[str, Any]]]:
    reg = load_registry(ROOT)
    ds = reg.get(DS)
    cfg = ds.entry.source.config()
    assert isinstance(cfg, HttpsFileConfig)
    body = _csv_bytes()
    fetch = RawFetch(URL, body, hashlib.sha256(body).hexdigest(), datetime(2026, 11, 27, 12, 0, tzinfo=UTC))
    _header, recs, provenance = read_records(ds.entry, cfg, body)
    records = with_document(recs, fetch)
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, records)
    build_snapshot(lake, ds, [fetch], [f], provenance=provenance)
    return reg, Engine(registry=reg, lake=lake), lake, records


def _rows(out: Any) -> list[dict[str, str]]:
    text = out if isinstance(out, str) else out.render("csv")
    return list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))


def test_the_catalog_entry_is_a_covariate_with_no_series_and_no_conditions() -> None:
    ds = load_registry(ROOT).get(DS)
    assert ds.entry.research_areas == ["covariate"]
    assert not ds.entry.id_areas and not ds.entry.conditions.labels and not ds.entry.gbd.causes
    assert not ds.semantic.series and not ds.semantic.generic_series
    # Ranks are estimates read as published: no metric sums them, and none is summable.
    assert {m.kind for m in ds.semantic.measures} == {"estimate"}
    assert {m.aggregation for m in ds.semantic.metrics} == {"none"}


@pytest.mark.parametrize(
    "change",
    [
        {"research_areas": ["covariate", "infectious_disease"]},
        {"id_areas": ["vector_borne"]},
    ],
)
def test_a_covariate_that_reaches_for_a_disease_area_is_refused(change: dict[str, Any]) -> None:
    ds = load_registry(ROOT).get(DS)
    with pytest.raises(RegistryError, match="hard rule 11"):
        _check_covariate(ds.entry.model_copy(update=change), ds.semantic)


def test_a_covariate_with_a_series_or_conditions_is_refused() -> None:
    reg = load_registry(ROOT)
    ds = reg.get(DS)
    census = reg.get("census_county_pop").semantic
    with pytest.raises(RegistryError, match="no series"):
        _check_covariate(ds.entry, ds.semantic.model_copy(update={"series": census.series}))
    labelled = ds.entry.conditions.model_copy(update={"labels": ["SVI"], "source": "made up for the test"})
    with pytest.raises(RegistryError, match="no conditions"):
        _check_covariate(ds.entry.model_copy(update={"conditions": labelled}), ds.semantic)


def test_a_covariate_question_on_its_own_is_told_it_needs_a_disease() -> None:
    reg = load_registry(ROOT)
    row = next(
        r
        for r in _rows(tools.suggest_datasets(reg, "What is the social vulnerability of Wayne County?"))
        if r["source_id"] == DS
    )
    assert row["can_answer"] == "no"
    assert "a covariate" in row["why"] and "never answers on its own" in row["why"]


def test_beside_a_disease_that_fits_the_covariate_fits_too() -> None:
    reg = load_registry(ROOT)
    out = tools.suggest_datasets(reg, "measles by county and social vulnerability")
    rows = {r["source_id"]: r for r in _rows(out)}
    assert rows["mdhhs_measles_county"]["can_answer"] == "yes"
    assert rows[DS]["can_answer"] == "yes"
    assert rows[DS]["why"].startswith("fits beside mdhhs_measles_county")


def test_describe_dataset_says_it_is_a_covariate_and_never_a_denominator() -> None:
    text = tools.describe_dataset(load_registry(ROOT), DS)
    assert isinstance(text, str)
    assert "this is a covariate (hard rule 11)" in text
    assert "use its population as a denominator" in text


def test_get_series_never_answers_from_the_covariate(real: tuple[Registry, Engine, Path, Any]) -> None:
    reg, eng, _, _ = real
    out = tools.get_series(reg, eng, condition="social vulnerability", source_id=DS, fmt="json")
    assert isinstance(out, dict)
    assert out["refused"] == "unknown_condition" and "rows" not in out


def test_raw_layer_keeps_every_column_as_sent(
    real: tuple[Registry, Engine, Path, list[dict[str, Any]]],
) -> None:
    """Hard rule 15: the raw records hold every column the file has, as text, -999 included, plus the adapter's
    document provenance; the byte-order mark never reaches the first label."""
    _, _, lake, _ = real
    files = list((lake / "raw_records" / DS).glob("*.ndjson"))
    assert len(files) == 1
    kept = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    for sent, row in zip(ROWS, kept, strict=True):
        assert {k: row[k] for k in HEADER} == dict(zip(HEADER, sent, strict=True))
        assert row["document_url"] == URL
    assert "﻿ST" not in kept[0]


def test_ranks_read_as_published_and_minus_999_is_no_value(real: tuple[Registry, Engine, Path, Any]) -> None:
    _, eng, _, _ = real
    out = tools.query_sql(
        eng,
        "SELECT county_fips, rank_overall, rank_household_characteristics, acs_population, flags_total "
        "FROM cdc_svi_county ORDER BY county_fips",
        fmt="json",
    )
    assert isinstance(out, dict), out
    got = [tuple(r[:5]) for r in out["rows"]]
    assert got == [
        ("01001", 0.4, 0.5, 50000, 1),
        ("09110", 0.5, 1.0, 800000, 0),
        ("26999", None, 0.0, None, None),  # -999 is "not available", never zero or -999
    ]


GOOD = [
    ("01001", "01001", "01", 0.4, 0.25, 0.5, 0.75, 0.1, 1),
    ("26163", "26163", "26", 0.95, 0.9, 0.8, 0.7, 0.6, 4),
]
COLS = (
    "county_fips VARCHAR, fips VARCHAR, state_fips VARCHAR, rank_overall DOUBLE, rank_socioeconomic DOUBLE, "
    "rank_household_characteristics DOUBLE, rank_racial_ethnic_minority DOUBLE, rank_housing_transportation DOUBLE, "
    "flags_total BIGINT, acs_population BIGINT, acs_population_moe BIGINT"
)


def _with(row: tuple[Any, ...], **kw: Any) -> tuple[Any, ...]:
    names = ["county_fips", "fips", "state_fips", "rank_overall", "rank_socioeconomic",
             "rank_household_characteristics", "rank_racial_ethnic_minority", "rank_housing_transportation",
             "flags_total"]  # fmt: skip
    d = dict(zip(names, row, strict=True)) | kw
    return tuple(d[n] for n in names)


@pytest.mark.parametrize(
    ("bad", "rule", "status"),
    [
        (_with(GOOD[0], county_fips="1001", fips="1001"), "county_fips_is_five_digits", "fail"),
        (_with(GOOD[0], county_fips="26001", fips="26001"), "county_fips_starts_with_its_state", "fail"),
        (_with(GOOD[0], fips="01003"), "fips_matches_county_fips", "warn"),
        (_with(GOOD[0], rank_overall=1.2), "rank_overall_between_0_and_1", "fail"),
        (_with(GOOD[0], rank_socioeconomic=-0.1), "rank_socioeconomic_between_0_and_1", "fail"),
        (
            _with(GOOD[0], rank_household_characteristics=-999.0),
            "rank_household_characteristics_between_0_and_1",
            "fail",
        ),  # a -999 the contract didn't catch is out of range, never read as a rank
        (
            _with(GOOD[0], rank_racial_ethnic_minority=1.01),
            "rank_racial_ethnic_minority_between_0_and_1",
            "fail",
        ),
        (
            _with(GOOD[0], rank_housing_transportation=2.0),
            "rank_housing_transportation_between_0_and_1",
            "fail",
        ),
        (_with(GOOD[0], flags_total=17), "flags_total_between_0_and_16", "fail"),
    ],
)
def test_each_contract_rule_catches_the_case_it_names(bad: tuple[Any, ...], rule: str, status: str) -> None:
    """Each rule is shown failing on its own bad case, not only ever passing. fifty_states_and_dc warns on these
    two-state tables, so it is checked on its own below."""
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({COLS})")
    rules = [
        r for r in quality.rules_for(load_registry(ROOT).get(DS).contract) if r.id != "fifty_states_and_dc"
    ]
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1000, 0)", GOOD)
    good = quality.run_rules(con, "t", rules)
    assert not good.rule_errors and {r["status"] for r in good.results} == {"pass"}, good.results
    con.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1000, 0)", bad)
    report = quality.run_rules(con, "t", rules)
    assert not report.rule_errors
    assert {r["test"]: r["status"] for r in report.results}[rule] == status


def test_an_all_empty_overall_rank_blocks_and_fewer_states_warn() -> None:
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({COLS})")
    con.executemany(
        "INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1000, 0)",
        [_with(g, rank_overall=None) for g in GOOD],
    )
    report = quality.run_rules(con, "t", quality.rules_for(load_registry(ROOT).get(DS).contract))
    status = {r["test"]: r["status"] for r in report.results}
    assert status["some_overall_ranks_present"] == "fail"
    assert status["fifty_states_and_dc"] == "warn"
