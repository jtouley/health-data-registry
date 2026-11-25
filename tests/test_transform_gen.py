"""The dbt project's staging layer comes only from the contracts: generated, reproducible, and typed by them."""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest
import yaml

from health_data_registry import transform_gen as tg
from health_data_registry.registry import DUCKDB_TYPES, RegistryError, load_registry
from health_data_registry.staging import (
    SILVER_METADATA_KEY,
    StagingReport,
    silver_schema,
    stage,
    write_parquet,
)
from health_data_registry.store import build_snapshot, keep_raw_records, write_ndjson
from tests.conftest import CONTRACT, RECORDS, raw_fetch, write_registry

REPO = Path(__file__).resolve().parents[1]
MODELS = REPO / "transform" / "models"

FLAGGED = CONTRACT.replace(
    "      - name: rate_per_100k\n",
    "      - name: cases_flag\n"
    "        physicalType: VARCHAR\n"
    "        logicalType: string\n"
    "        description: Why the count is empty.\n"
    "        customProperties:\n"
    "          - {property: role, value: attribute}\n"
    "          - {property: sourceField, value: m1_flag}\n"
    "          - {property: flagFor, value: cases}\n"
    "          - {property: flagCodes, value: {'-': no reported cases, U: unavailable}}\n"
    "      - name: rate_per_100k\n",
)


def test_committed_transform_files_are_exactly_what_the_contracts_generate() -> None:
    files = tg.generate(load_registry(REPO))
    assert tg.stale(REPO, files) == []
    assert tg.main(["--check", "--root", str(REPO)]) == 0


def test_no_column_type_or_code_is_declared_outside_the_contract() -> None:
    """Every staging column's type and code list is the contract's; nothing else in transform/ declares one."""
    reg = load_registry(REPO)
    generated = {REPO / p for p in tg.generate(reg)}
    for ds in tg.stored(reg):
        doc = yaml.safe_load((REPO / tg.STAGING_DIR / f"stg_{ds.id}.yml").read_text())
        cols = {c["name"]: c for c in doc["models"][0]["columns"]}
        assert list(cols) == [c.name for c in ds.contract.columns]
        for c in ds.contract.columns:
            assert cols[c.name]["data_type"] == tg.DBT_TYPES[c.physical_type]
            assert cols[c.name]["meta"]["codes"] == [k for k, _ in c.flag_codes]
            assert cols[c.name]["meta"]["missing_codes"] == list(c.missing_codes)
        assert [c.physical_type for c in silver_schema(ds.contract)] == [
            c.physical_type for c in ds.contract.columns
        ]
    for path in MODELS.rglob("*"):
        if path.is_file() and path not in generated:
            assert path.suffix == ".sql", f"{path}: only the generator writes model schemas"
            assert path.parent != REPO / tg.STAGING_DIR, f"{path}: staging is generated only"
            for marker in ("data_type", "accepted_values", "missing_codes", "constraints"):
                assert marker not in path.read_text().lower(), (
                    f"{path} declares {marker!r}; only the contract may"
                )


DATA_SUFFIXES = {
    ".csv",
    ".tsv",
    ".json",
    ".ndjson",
    ".jsonl",
    ".parquet",
    ".duckdb",
    ".xlsx",
    ".xls",
    ".zip",
    ".gz",
}


def test_transform_has_no_seeds_no_data_and_every_input_has_an_odcs_contract() -> None:
    """Owner decision, 25 Nov: every dbt input and model is backed by an ODCS contract; no seeds, no data files."""
    root = REPO / "transform"
    project = yaml.safe_load((root / "dbt_project.yml").read_text())
    assert not (root / "seeds").exists() and "seed-paths" not in project and "seeds" not in project
    ignored = {"target", "logs", "dbt_packages"}
    for path in root.rglob("*"):
        if path.is_file() and not ignored & set(path.relative_to(root).parts):
            assert path.suffix.lower() not in DATA_SUFFIXES, f"{path}: no data files in transform/"
    sources: dict[str, str] = {}
    models: dict[str, str] = {}
    for path in MODELS.rglob("*.yml"):
        doc = yaml.safe_load(path.read_text())
        for src in doc.get("sources") or []:
            for t in src["tables"]:
                sources[f"{src['name']}.{t['name']}"] = t["meta"]["contract"]
        for m in doc.get("models") or []:
            models[m["name"]] = m["meta"]["contract"]
    for contract in [*sources.values(), *models.values()]:
        assert (REPO / contract).is_file() and contract.endswith(".odcs.yaml"), contract
    sql = {p.stem: p.read_text() for p in MODELS.rglob("*.sql")}
    assert set(sql) == set(models), "every model has a contract-backed schema, and every schema a model"
    for name, text in sql.items():
        for kind, ref in re.findall(r"\b(source|ref)\(([^)]*)\)", text):
            parts = [x.strip(" '\"") for x in ref.split(",")]
            key = ".".join(parts) if kind == "source" else parts[-1]
            assert key in (sources if kind == "source" else models), (
                f"{name}: {kind}({ref}) has no ODCS contract"
            )
    assert sources and models


def _mmwr(first: int, last: int) -> list[tuple[int, int, date, date]]:
    text = (MODELS / "reference" / "mmwr_calendar.sql").read_text()
    text = text.replace("{{ var('mmwr_first_year') }}", str(first)).replace(
        "{{ var('mmwr_last_year') }}", str(last)
    )
    return duckdb.connect().execute(text + " ORDER BY 1, 2").fetchall()


@pytest.mark.parametrize(
    ("year", "start", "weeks"),
    # CDC's rule (MMWR Weeks, business rules): week 1 holds 1 January when it falls Sunday to Wednesday, else it
    # is the first full week. Hand-written from CDC's published weeks: 2020 and 2025 have 53 weeks.
    [(2019, date(2018, 12, 30), 52), (2020, date(2019, 12, 29), 53), (2021, date(2021, 1, 3), 52),
     (2023, date(2023, 1, 1), 52), (2025, date(2024, 12, 29), 53), (2026, date(2026, 1, 4), 52)],
)  # fmt: skip
def test_mmwr_calendar_model_follows_cdc_rule(year: int, start: date, weeks: int) -> None:
    rows = _mmwr(year, year)
    assert len(rows) == weeks
    assert rows[0] == (year, 1, start, start + timedelta(days=6))
    assert rows[-1][1] == weeks
    assert all(
        b[2] == a[2] + timedelta(days=7) and b[3] == b[2] + timedelta(days=6)
        for a, b in zip(rows, rows[1:], strict=False)
    )
    assert all(r[2].isoweekday() == 7 for r in rows)  # every week starts on a Sunday


def test_mmwr_calendar_years_join_without_gaps_and_match_the_contract() -> None:
    rows = _mmwr(2015, 2030)
    assert all(b[2] == a[3] + timedelta(days=1) for a, b in zip(rows, rows[1:], strict=False))
    assert {r[0] for r in rows} == set(range(2015, 2031))
    project = yaml.safe_load((REPO / "transform" / "dbt_project.yml").read_text())
    assert (project["vars"]["mmwr_first_year"], project["vars"]["mmwr_last_year"]) == (2015, 2030)
    doc = yaml.safe_load((MODELS / "reference" / "mmwr_calendar.yml").read_text())
    (m,) = doc["models"]
    assert m["meta"]["contract"] == "contracts/models/mmwr_calendar.odcs.yaml"
    assert m["constraints"] == [{"type": "primary_key", "columns": ["mmwr_year", "mmwr_week"]}]
    assert [(c["name"], c["data_type"]) for c in m["columns"]] == [
        ("mmwr_year", "bigint"), ("mmwr_week", "bigint"), ("week_start", "date"), ("week_end", "date")]  # fmt: skip


def test_live_only_sources_get_no_dbt_model() -> None:
    reg = load_registry(REPO)
    files = {str(p) for p in tg.generate(reg)}
    assert "transform/models/staging/stg_who_gho_tb_incidence.sql" not in files
    assert "who_gho_tb_incidence" not in tg.raw_sources(reg)
    assert len(tg.stored(reg)) == sum(1 for ds in reg if ds.entry.licence.redistribution)


def test_raw_fields_cannot_change_without_a_version_bump(tmp_path: Path) -> None:
    reg = load_registry(write_registry(tmp_path / "reg"))
    before = tg.raw_sources(reg)
    renamed = CONTRACT.replace("{property: sourceField, value: m2}", "{property: sourceField, value: m9}")
    after = tg.raw_sources(load_registry(write_registry(tmp_path / "r2", contract=renamed)))
    with pytest.raises(tg.RawContractChanged, match=r"toy_cases: the raw fields changed .* still 0\.1\.0"):
        tg.check_raw_versions(before, after)
    bumped = renamed.replace("version: 0.1.0\nstatus", "version: 0.2.0\nstatus")
    tg.check_raw_versions(
        before, tg.raw_sources(load_registry(write_registry(tmp_path / "r3", contract=bumped)))
    )
    tg.check_raw_versions(before, before)
    tg.check_raw_versions("", after)  # a new source has nothing to compare against
    # write() refuses too, and leaves the committed files alone
    root = write_registry(tmp_path / "w", contract=renamed)
    (root / tg.SOURCES_FILE).parent.mkdir(parents=True)
    (root / tg.SOURCES_FILE).write_text(before)
    with pytest.raises(tg.RawContractChanged):
        tg.write(root, tg.generate(load_registry(root)))
    assert (root / tg.SOURCES_FILE).read_text() == before


def test_raw_source_declares_fields_as_text_with_the_contract_version(tmp_path: Path) -> None:
    reg = load_registry(write_registry(tmp_path / "reg"))
    doc = yaml.safe_load(tg.raw_sources(reg))
    (t,) = doc["sources"][0]["tables"]
    assert t["name"] == "toy_cases"
    assert t["meta"]["raw_contract_version"] == "0.1.0"
    assert [c["name"] for c in t["columns"]] == ["label", "m1", "m2", "states", "year"]
    assert {c["data_type"] for c in t["columns"]} == {"varchar"}
    loc = t["meta"]["external_location"]
    assert "HDR_RAW_RECORDS" in loc and "/toy_cases/*.ndjson" in loc and "filename=true" in loc
    assert "{{ '{{' }}'label': 'VARCHAR'" in loc and loc.endswith("{{ '}}' }})")


def test_staging_model_types_latest_load_and_dedupes_only_on_a_declared_key(tmp_path: Path) -> None:
    reg = load_registry(write_registry(tmp_path / "reg"))
    ds = reg.get("toy_cases")
    sql = tg.staging_sql(ds)
    assert "source('raw', 'toy_cases')" in sql
    assert "where filename = (select max(filename) from raw)" in sql
    assert "qualify" not in sql and tg.primary_key(ds.contract) == []
    keyed = CONTRACT.replace("        required: true\n        description: The state", "        required: true\n        primaryKey: true\n        primaryKeyPosition: 2\n        description: The state", 1).replace(
        "      - name: year\n        physicalType: INTEGER\n",
        "      - name: year\n        physicalType: INTEGER\n        primaryKey: true\n        primaryKeyPosition: 1\n",
    )  # fmt: skip
    kds = load_registry(write_registry(tmp_path / "k", contract=keyed)).get("toy_cases")
    assert tg.primary_key(kds.contract) == ["year", "reporting_area"]
    assert 'qualify row_number() over (partition by "year", "reporting_area" order by filename desc) = 1' in (
        tg.staging_sql(kds)
    )
    # The SQL runs: only the newest file's rows survive, typed by the contract.
    con = duckdb.connect()
    con.execute("CREATE TABLE raw AS SELECT * FROM (VALUES ('a', 'MICHIGAN', '2023', 'Legionellosis', '5', '1.0'), "
                "('b', 'MICHIGAN', '2023', 'Legionellosis', '6.0', '1.5')) t(filename, states, year, label, m1, m2)")  # fmt: skip
    body = sql.split("),\n\nlatest", 1)[1]
    rows = con.execute("WITH latest" + body).fetchall()
    assert rows == [("MICHIGAN", 2023, "Legionellosis", 6, 1.5)]


def test_staging_yml_is_an_enforced_contract_with_tests_from_codes(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg", contract=FLAGGED)).get("toy_cases")
    doc = yaml.safe_load(tg.staging_yml(ds))
    (m,) = doc["models"]
    assert m["name"] == "stg_toy_cases"
    assert m["config"] == {"materialized": "table", "contract": {"enforced": True}, "tags": ["staging"]}
    assert m["meta"] == {"contract": "contracts/toy_cases.odcs.yaml", "contract_id": "toy_cases",
                         "contract_version": "0.1.0", "contract_status": "draft", "primary_key": []}  # fmt: skip
    assert "constraints" not in m
    cols = {c["name"]: c for c in m["columns"]}
    assert cols["reporting_area"]["constraints"] == [{"type": "not_null"}]
    assert "constraints" not in cols["cases"]
    assert cols["cases"]["meta"]["missing_codes"] == ["-", "N"]
    assert cols["cases"]["meta"]["source_field"] == "m1"
    assert cols["year"]["data_type"] == "integer"
    assert cols["rate_per_100k"]["data_type"] == "double"
    (t,) = cols["cases_flag"]["data_tests"]
    assert t["accepted_values"]["arguments"] == {"values": ["-", "U"]}
    assert t["accepted_values"]["config"]["severity"] == "warn"
    assert t["accepted_values"]["name"] == "stg_toy_cases_cases_flag_codes"
    assert tg.staging_yml(ds).startswith(
        "# Generated by `make contracts` from contracts/toy_cases.odcs.yaml."
    )


def test_write_replaces_hand_written_staging_files_and_check_reports_them(tmp_path: Path) -> None:
    root = write_registry(tmp_path / "reg")
    files = tg.generate(load_registry(root))
    assert set(tg.write(root, files)) == {str(p) for p in files}
    assert tg.stale(root, files) == []
    assert tg.main(["--check", "--root", str(root)]) == 0
    stray = root / tg.STAGING_DIR / "stg_hand.sql"
    stray.write_text("select 1")
    schema = root / tg.MODELS_DIR / "marts" / "hand.yml"
    schema.parent.mkdir(exist_ok=True)
    schema.write_text("version: 2\n")
    model = root / tg.MODELS_DIR / "marts" / "hand.sql"
    model.write_text("select 1")
    (root / tg.SOURCES_FILE).write_text((root / tg.SOURCES_FILE).read_text() + "# edited\n")
    assert tg.stale(root, files) == [
        str(tg.SOURCES_FILE), str(tg.MODELS_DIR / "marts" / "hand.yml"), str(tg.STAGING_DIR / "stg_hand.sql")]  # fmt: skip
    assert tg.main(["--check", "--root", str(root)]) == 1
    assert tg.main(["--root", str(root)]) == 0
    assert not stray.exists() and not schema.exists() and model.exists() and tg.stale(root, files) == []


def test_logical_type_must_match_physical_type(tmp_path: Path) -> None:
    bad = CONTRACT.replace(
        "physicalType: DOUBLE\n        logicalType: number",
        "physicalType: DOUBLE\n        logicalType: integer",
    )
    with pytest.raises(
        RegistryError,
        match=r"rate_per_100k: logicalType 'integer' does not match physicalType DOUBLE \(expected number\)",
    ):
        load_registry(write_registry(tmp_path / "reg", contract=bad))
    assert set(tg.DBT_TYPES) == DUCKDB_TYPES  # every contract type has a dbt type


def test_stage_reports_unlisted_codes_and_keeps_them(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg", contract=FLAGGED)).get("toy_cases")
    con = duckdb.connect()
    con.execute("CREATE TABLE raw AS SELECT * FROM (VALUES ('MICHIGAN', '2023', 'Legionellosis', NULL, 'X', '1'), "
                "('OHIO', '2023', 'Legionellosis', NULL, 'Q', '1'), ('OHIO', '2022', 'Legionellosis', NULL, 'U', '1'), "
                "('OHIO', '2021', 'Legionellosis', '4', ' ', '1')) t(states, year, label, m1, m1_flag, m2)")  # fmt: skip
    report = stage(con, ds.contract, "raw", table="toy_cases")
    assert report == StagingReport(row_count=4, unlisted_codes={"cases_flag": ("Q", "X")})
    assert con.execute("SELECT cases_flag FROM toy_cases ORDER BY year").fetchall() == [
        (" ",),
        ("U",),
        ("X",),
        ("Q",),
    ]
    assert report.notes() == [
        "The publisher's cases_flag column holds the codes 'Q', 'X', which the reviewed contract doesn't list, so "
        "what they mean is not known here."
    ]
    one = StagingReport(row_count=1, unlisted_codes={"cases_flag": ("Q",)})
    assert one.notes() == [
        "The publisher's cases_flag column holds the code 'Q', which the reviewed contract doesn't list, so what "
        "it means is not known here."
    ]
    assert one.as_dict() == {"layout_version": "1", "row_count": 1, "unlisted_codes": {"cases_flag": ["Q"]}}
    comments = dict(
        con.execute(
            "SELECT column_name, comment FROM duckdb_columns() WHERE table_name = 'toy_cases'"
        ).fetchall()
    )
    assert comments["cases_flag"] == "Why the count is empty."
    path = tmp_path / "t.parquet"
    write_parquet(con, ds.contract, "toy_cases", str(path))
    meta = dict(con.execute("SELECT key, value FROM parquet_kv_metadata(?)", [str(path)]).fetchall())
    layout = json.loads(meta[SILVER_METADATA_KEY.encode()])
    assert layout["contract_id"] == "toy_cases" and layout["contract_version"] == "0.1.0"
    assert [c["name"] for c in layout["columns"]] == [c.name for c in ds.contract.columns]
    assert layout["columns"][-2]["codes"] == ["-", "U"]


def test_stage_refuses_an_empty_required_value(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    con = duckdb.connect()
    con.execute("CREATE TABLE raw AS SELECT * FROM (VALUES (NULL, '2023', 'L', '1', '1'), (NULL, '2024', 'L', '1', '1')) "
                "t(states, year, label, m1, m2)")  # fmt: skip
    with pytest.raises(ValueError, match="toy_cases: required column reporting_area has 2 empty values"):
        stage(con, ds.contract, "raw", table="toy_cases")


def test_full_loads_keep_their_raw_records_and_partial_loads_do_not(tmp_path: Path) -> None:
    reg = load_registry(write_registry(tmp_path / "reg"))
    ds = reg.get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    m = build_snapshot(lake, ds, [raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)], [page])
    kept = lake / "raw_records" / "toy_cases" / f"{m.snapshot_id}.ndjson"
    assert kept.read_bytes() == page.read_bytes()
    assert m.silver == {"layout_version": "1", "row_count": len(RECORDS), "unlisted_codes": {}}
    p = build_snapshot(
        lake, ds, [raw_fetch("https://data.cdc.gov/resource/b.json", RECORDS[:1])], [page], partial=True
    )
    assert not (lake / "raw_records" / "toy_cases" / f"{p.snapshot_id}.ndjson").exists()
    # Two pages become one file in page order, written once.
    other = tmp_path / "q.ndjson"
    write_ndjson(other, RECORDS[:1])
    out = keep_raw_records(lake, "x", "20260101T000000Z-000000000000", [page, other])
    assert out.read_bytes() == page.read_bytes() + other.read_bytes()
    assert keep_raw_records(lake, "x", "20260101T000000Z-000000000000", [other]) == out
    assert out.read_bytes() == page.read_bytes() + other.read_bytes()
    assert not list(out.parent.glob(".*.tmp"))
