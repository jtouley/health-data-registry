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
from health_data_registry.dbt_raw import PROVENANCE
from health_data_registry.live import load_live
from health_data_registry.registry import DUCKDB_TYPES, RegistryError, load_registry
from health_data_registry.staging import (
    SILVER_METADATA_KEY,
    ContractLayoutRefused,
    PublisherDataRefused,
    StagingRefused,
    StagingReport,
    silver_schema,
    stage,
    write_parquet,
)
from health_data_registry.store import build_snapshot, keep_raw_records, latest_snapshot, write_ndjson
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
        assert list(cols) == [*(c.name for c in ds.contract.columns), *PROVENANCE]
        for c in ds.contract.columns:
            assert cols[c.name]["data_type"] == tg.DBT_TYPES[c.physical_type]
            assert cols[c.name]["meta"]["codes"] == [k for k, _ in c.flag_codes]
            assert cols[c.name]["meta"]["missing_codes"] == list(c.missing_codes)
        assert [c.physical_type for c in silver_schema(ds.contract)] == [
            c.physical_type for c in ds.contract.columns
        ]
    for path in MODELS.rglob("*"):
        if path.is_file() and path not in generated:
            assert not tg.is_generated(path), f"{path}: a generated file no contract makes any more"
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
    assert m["meta"]["primary_key"] == ["mmwr_year", "mmwr_week"]
    assert m["data_tests"] == [
        {"one_row_per_key": {"name": "mmwr_calendar_one_row_per_mmwr_year_mmwr_week",
                             "arguments": {"columns": ["mmwr_year", "mmwr_week"]}}}]  # fmt: skip
    assert (REPO / "transform" / "tests" / "mmwr_calendar_weeks_follow_cdc_rule.sql").is_file()
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
    (src,) = doc["sources"]
    assert (src["name"], src["schema"]) == ("raw", "raw")
    (t,) = src["tables"]
    assert t["name"] == "toy_cases"
    assert t["meta"] == {"raw_contract_version": "0.1.0", "contract": "contracts/toy_cases.odcs.yaml",
                         "load": "snapshot", "load_key": [], "updated_field": None,
                         "plugin": "hdr_raw", "source_fields": ["label", "m1", "m2", "states", "year"]}  # fmt: skip
    assert [c["name"] for c in t["columns"]] == ["label", "m1", "m2", "states", "year", *PROVENANCE]
    assert {c["data_type"] for c in t["columns"]} == {"varchar"}
    cols = {c["name"]: c["description"] for c in t["columns"]}
    assert cols["states"].startswith("Publisher field states, as text; read into reporting_area (")
    assert cols["_snapshot_id"] == tg.PROVENANCE_DOCS["_snapshot_id"]
    assert "LATEST" in t["description"] and "structural reshaping" in t["description"]
    # The provenance columns aren't publisher fields: adding them never counts as a raw field change.
    assert tg._raw_versions(tg.raw_sources(reg))["toy_cases"] == (
        "0.1.0",
        ["label", "m1", "m2", "states", "year"],
    )


KEYED = CONTRACT.replace("        required: true\n        description: The state", "        required: true\n        primaryKey: true\n        primaryKeyPosition: 2\n        description: The state", 1).replace(
    "      - name: year\n        physicalType: INTEGER\n",
    "      - name: year\n        physicalType: INTEGER\n        primaryKey: true\n        primaryKeyPosition: 1\n",
)  # fmt: skip


def test_staging_model_types_every_row_and_never_dedupes(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    kds = load_registry(write_registry(tmp_path / "k", contract=KEYED)).get("toy_cases")
    assert tg.primary_key(ds.contract) == [] and tg.primary_key(kds.contract) == ["year", "reporting_area"]
    for d in (ds, kds):
        sql = tg.staging_sql(d)
        assert sql.endswith("from {{ source('raw', 'toy_cases') }}\n")
        assert "qualify" not in sql and "row_number" not in sql and "filename" not in sql
    # The SQL runs: two rows with one key inside the load both survive, typed, with their snapshot link.
    con = duckdb.connect()
    con.execute("CREATE TABLE raw AS SELECT * FROM (VALUES ('MICHIGAN', '2023', 'Legionellosis', '5', '1.0', 's1', 'h1'), "
                "('MICHIGAN', '2023', 'Legionellosis', '6.0', '1.5', 's1', 'h1')) "
                "t(states, year, label, m1, m2, _snapshot_id, _raw_sha256)")  # fmt: skip
    body = tg.staging_sql(kds).replace("{{ source('raw', 'toy_cases') }}", "raw")
    rows = con.execute(body + " ORDER BY cases").fetchall()
    assert rows == [("MICHIGAN", 2023, "Legionellosis", 5, 1.0, "s1", "h1"),
                    ("MICHIGAN", 2023, "Legionellosis", 6, 1.5, "s1", "h1")]  # fmt: skip
    # ...and the keyed model's test names the key, so dbt build fails on them instead of choosing one.
    (m,) = yaml.safe_load(tg.staging_yml(kds))["models"]
    assert m["data_tests"] == [{"one_row_per_key": {"name": "stg_toy_cases_one_row_per_year_reporting_area",
                                                    "arguments": {"columns": ["year", "reporting_area"]}}}]  # fmt: skip
    assert "constraints" not in m
    macro = (REPO / "transform" / "macros" / "generic_tests.sql").read_text(encoding="utf-8")
    assert "{% test one_row_per_key(model, columns) %}" in macro and "having count(*) > 1" in macro


def test_staging_yml_is_an_enforced_contract_with_tests_from_codes(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg", contract=FLAGGED)).get("toy_cases")
    doc = yaml.safe_load(tg.staging_yml(ds))
    (m,) = doc["models"]
    assert m["name"] == "stg_toy_cases"
    assert m["config"] == {"materialized": "table", "contract": {"enforced": True}, "tags": ["staging"]}
    assert m["meta"] == {"contract": "contracts/toy_cases.odcs.yaml", "contract_id": "toy_cases",
                         "contract_version": "0.1.0", "contract_status": "draft", "primary_key": []}  # fmt: skip
    assert "constraints" not in m and "data_tests" not in m
    cols = {c["name"]: c for c in m["columns"]}
    assert cols["reporting_area"]["constraints"] == [{"type": "not_null"}]
    assert "data_tests" not in cols["reporting_area"]  # required: not_null, no fill-rate test
    assert "constraints" not in cols["cases"]
    assert cols["cases"]["data_tests"] == [
        {"not_all_empty": {"name": "stg_toy_cases_cases_filled", "config": {"severity": "warn"}}}]  # fmt: skip
    assert cols["cases_flag"]["description"] == (
        "Why the count is empty. Codes (publisher's codebook): - = no reported cases; U = unavailable."
    )
    assert cols["cases"]["description"] == ds.contract.column("cases").description
    for p in PROVENANCE:
        assert cols[p] == {"name": p, "data_type": "varchar", "description": tg.PROVENANCE_DOCS[p],
                           "constraints": [{"type": "not_null"}]}  # fmt: skip
    assert cols["cases"]["meta"]["missing_codes"] == ["-", "N"]
    assert cols["cases"]["meta"]["source_field"] == "m1"
    assert cols["year"]["data_type"] == "integer"
    assert cols["rate_per_100k"]["data_type"] == "double"
    filled, t = cols["cases_flag"]["data_tests"]
    assert filled == {
        "not_all_empty": {"name": "stg_toy_cases_cases_flag_filled", "config": {"severity": "warn"}}
    }
    assert t["accepted_values"]["arguments"] == {"values": ["-", "U"]}
    assert t["accepted_values"]["config"]["severity"] == "warn"
    assert t["accepted_values"]["name"] == "stg_toy_cases_cases_flag_codes"
    assert tg.staging_yml(ds).startswith(
        "# Generated by `make contracts` from contracts/toy_cases.odcs.yaml."
    )


def test_write_deletes_only_its_own_stale_staging_files_and_refuses_anything_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = write_registry(tmp_path / "reg")
    files = tg.generate(load_registry(root))
    assert set(tg.write(root, files)) == {str(p) for p in files}
    assert tg.stale(root, files) == []
    assert tg.main(["--check", "--root", str(root)]) == 0
    # Stage (b)'s hand-written models and schemas outside staging/ are never stale, deleted or refused.
    marts = root / tg.MODELS_DIR / "marts"
    marts.mkdir()
    (marts / "fct_hand.yml").write_text("version: 2\n", encoding="utf-8")
    (marts / "fct_hand.sql").write_text("select 1", encoding="utf-8")
    (root / tg.MODELS_DIR / "intermediate").mkdir(exist_ok=True)  # stage (b) generates int_* models here
    (root / tg.MODELS_DIR / "intermediate" / "flag_hand.yml").write_text("version: 2\n", encoding="utf-8")
    # A generated file no contract makes any more, in staging/: deleted.
    old = root / tg.STAGING_DIR / "stg_gone.sql"
    old.write_text(
        "-- " + tg.HEADER.format(src="contracts/gone.odcs.yaml") + "\nselect 1\n", encoding="utf-8"
    )
    (root / tg.SOURCES_FILE).write_text((root / tg.SOURCES_FILE).read_text() + "# edited\n")
    assert tg.stale(root, files) == [str(tg.SOURCES_FILE), str(tg.STAGING_DIR / "stg_gone.sql")]
    assert tg.main(["--check", "--root", str(root)]) == 1
    assert tg.main(["--root", str(root)]) == 0
    assert not old.exists() and tg.stale(root, files) == []
    assert (marts / "fct_hand.yml").is_file() and (marts / "fct_hand.sql").is_file()
    assert (root / tg.MODELS_DIR / "intermediate" / "flag_hand.yml").is_file()
    # A hand-written file in staging/, a generated file outside staging/ that no contract makes, and a hand edit
    # that dropped the header of a generated file: refused, named, and nothing is deleted or written.
    hand = root / tg.STAGING_DIR / "stg_hand.sql"
    hand.write_text("select 1", encoding="utf-8")
    orphan = marts / "orphan.yml"
    orphan.write_text(
        "# " + tg.HEADER.format(src="contracts/models/orphan.odcs.yaml") + "\n", encoding="utf-8"
    )
    edited = root / tg.STAGING_DIR / "stg_toy_cases.sql"
    edited.write_text("select 2", encoding="utf-8")
    with pytest.raises(tg.NotGenerated) as err:
        tg.write(root, files)
    for p in (hand, orphan, edited):
        assert str(p.relative_to(root)) in str(err.value)
    assert str(tg.SOURCES_FILE) not in str(err.value)
    assert hand.is_file() and orphan.is_file() and edited.read_text() == "select 2"
    assert tg.main(["--root", str(root)]) == 1
    assert "make contracts stopped: make contracts writes and deletes only its own generated files" in (
        capsys.readouterr().err
    )
    assert set(tg.stale(root, files)) == {str(p.relative_to(root)) for p in (hand, orphan, edited)}


def test_is_generated_reads_the_header_as_utf8(tmp_path: Path) -> None:
    p = tmp_path / "x.yml"
    p.write_text("# " + tg.HEADER.format(src="contracts/é.odcs.yaml") + "\nα: β\n", encoding="utf-8")
    assert tg.is_generated(p)
    p.write_text("-- " + tg.HEADER.format(src="x") + "\n", encoding="utf-8")
    assert tg.is_generated(p)
    p.write_text("version: 2  # " + tg.GENERATED_MARK + "\n", encoding="utf-8")
    assert not tg.is_generated(p)  # the mark only counts as the header comment
    p.write_text("# hand-written\n", encoding="utf-8")
    assert not tg.is_generated(p)
    p.write_bytes(b"\xff\xfe# " + tg.GENERATED_MARK.encode())
    assert not tg.is_generated(p)
    assert not tg.is_generated(tmp_path / "missing.yml")
    root = write_registry(tmp_path / "reg")
    files = tg.generate(load_registry(root))
    tg.write(root, files)
    assert not tg._differs(root / tg.SOURCES_FILE, files[tg.SOURCES_FILE])
    assert tg._differs(root / "nope.yml", "")


@pytest.mark.parametrize("layer", ["reference", "intermediate", "marts"])
def test_model_contracts_may_declare_each_layer(tmp_path: Path, layer: str) -> None:
    text = (REPO / "contracts" / "models" / "mmwr_calendar.odcs.yaml").read_text(encoding="utf-8")
    root = write_registry(tmp_path / "reg")
    (root / tg.MODEL_CONTRACTS).mkdir(parents=True)
    (root / tg.MODEL_CONTRACTS / "mmwr_calendar.odcs.yaml").write_text(
        text.replace("{property: layer, value: reference}", f"{{property: layer, value: {layer}}}"),
        encoding="utf-8",
    )
    files = tg.generate(load_registry(root))
    assert tg.MODELS_DIR / layer / "mmwr_calendar.yml" in files


@pytest.mark.parametrize(
    "bad",
    [
        "{property: layer, value: staging}",
        "{property: layer, value: marts}\n      - {property: layer, value: marts}",
        "",
    ],
)
def test_model_contracts_must_declare_one_known_layer(tmp_path: Path, bad: str) -> None:
    text = (REPO / "contracts" / "models" / "mmwr_calendar.odcs.yaml").read_text(encoding="utf-8")
    root = write_registry(tmp_path / "reg")
    (root / tg.MODEL_CONTRACTS).mkdir(parents=True)
    (root / tg.MODEL_CONTRACTS / "mmwr_calendar.odcs.yaml").write_text(
        text.replace("{property: layer, value: reference}", bad).replace("      - \n", ""), encoding="utf-8"
    )
    with pytest.raises(
        ValueError, match="declares its layer once, as one of reference, intermediate, flag, marts"
    ):
        tg.generate(load_registry(root))


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


def _tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("SELECT table_name FROM duckdb_tables() ORDER BY 1").fetchall()]


def test_stage_refuses_an_empty_required_value_and_keeps_no_table(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    con = duckdb.connect()
    con.execute("CREATE TABLE raw AS SELECT * FROM (VALUES (NULL::VARCHAR, '2023', 'L', '1', '1'), (NULL, '2024', 'L', '1', '1')) "
                "t(states, year, label, m1, m2)")  # fmt: skip
    with pytest.raises(
        PublisherDataRefused,
        match="^toy_cases: the publisher's data has 2 empty values in reporting_area, which the contract requires$",
    ):
        stage(con, ds.contract, "raw", table="toy_cases")
    assert _tables(con) == ["raw"]
    con.execute("UPDATE raw SET states = 'X' WHERE year = '2024'")
    with pytest.raises(PublisherDataRefused, match="has 1 empty value in reporting_area,"):
        stage(con, ds.contract, "raw", table="toy_cases", temp=True)
    assert _tables(con) == ["raw"]
    con.execute("UPDATE raw SET states = 'X', m1 = '2.5'")
    with pytest.raises(
        PublisherDataRefused,
        match="^toy_cases: the publisher's data has a value in cases that has a fraction",
    ):
        stage(con, ds.contract, "raw", table="toy_cases")
    assert _tables(con) == ["raw"]
    with pytest.raises(duckdb.Error) as err:  # any other failure is raised as it is
        stage(con, ds.contract, "no_such_table", table="toy_cases")
    assert not isinstance(err.value, StagingRefused)
    assert issubclass(PublisherDataRefused, StagingRefused) and issubclass(
        ContractLayoutRefused, StagingRefused
    )
    assert issubclass(StagingRefused, ValueError)


def test_stage_refuses_a_declared_field_no_record_carries(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE raw AS SELECT * FROM (VALUES ('A', '2023', 'L', '1', '1')) t(states, year, label, m1, m2)"
    )
    seen = {"states", "year", "label", "m1", "m2"}
    assert stage(con, ds.contract, "raw", table="t1", present=seen).row_count == 1
    assert stage(con, ds.contract, "raw", table="t2", present=None).row_count == 1  # not known: not checked
    with pytest.raises(
        ContractLayoutRefused,
        match="^toy_cases: the publisher's records no longer have the field m2, which the contract reads; the "
        "publisher may have renamed it, so check the contract$",
    ):
        stage(con, ds.contract, "raw", table="t3", present=seen - {"m2"} | {"m2_renamed"})
    with pytest.raises(ContractLayoutRefused, match="no longer have the fields m1, m2, .* renamed them,"):
        stage(con, ds.contract, "raw", table="t3", present={"states", "year", "label"})
    assert "t3" not in _tables(con)
    # A column the contract marks mayBeEmpty may be absent from a whole load.
    optional = CONTRACT.replace(
        "{property: sourceField, value: m2}",
        "{property: sourceField, value: m2}, {property: mayBeEmpty, value: true}",
    )
    ods = load_registry(write_registry(tmp_path / "o", contract=optional)).get("toy_cases")
    assert ods.contract.column("rate_per_100k").may_be_empty and not ods.contract.column("cases").may_be_empty
    assert stage(con, ods.contract, "raw", table="t4", present=seen - {"m2"}).row_count == 1
    (m,) = yaml.safe_load(tg.staging_yml(ods))["models"]
    assert "data_tests" not in {c["name"]: c for c in m["columns"]}["rate_per_100k"]  # no fill-rate floor


@pytest.mark.parametrize("field", ["m{1}", "m'1", 'm"1', "m\\1", "m\n1", "m`1"])
def test_registry_refuses_field_names_generated_sql_cant_carry(tmp_path: Path, field: str) -> None:
    bad = CONTRACT.replace(
        "{property: sourceField, value: m2}", "{property: sourceField, value: " + json.dumps(field) + "}"
    )
    with pytest.raises(
        RegistryError,
        match="rate_per_100k: sourceField .* has a brace, quote, backtick, backslash or control",
    ):
        load_registry(write_registry(tmp_path / "reg", contract=bad))


def test_registry_refuses_a_may_be_empty_that_isnt_a_boolean(tmp_path: Path) -> None:
    bad = CONTRACT.replace(
        "{property: sourceField, value: m2}",
        "{property: sourceField, value: m2}, {property: mayBeEmpty, value: yes please}",
    )
    with pytest.raises(RegistryError, match="rate_per_100k: mayBeEmpty must be true or false"):
        load_registry(write_registry(tmp_path / "reg", contract=bad))


def test_live_load_refusals_leave_nothing_in_memory(tmp_path: Path) -> None:
    """Live sources are checked like stored ones: a refused table never stays on the shared connection."""
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    con = duckdb.connect(":memory:")
    good = [dict(r) for r in RECORDS]
    report = load_live(con, ds, good)
    assert report.staging.row_count == len(RECORDS)
    assert con.execute("SELECT count(*) FROM toy_cases").fetchone() == (len(RECORDS),)
    con.execute("DROP TABLE toy_cases")
    with pytest.raises(PublisherDataRefused, match="empty value in reporting_area"):
        load_live(con, ds, [*good, {**good[0], "states": None}])
    assert _tables(con) == []
    with pytest.raises(ContractLayoutRefused, match="no longer have the field m2,"):
        load_live(con, ds, [{k: v for k, v in r.items() if k != "m2"} for r in good])
    assert _tables(con) == []
    assert load_live(con, ds, []).staging.row_count == 0  # no records: nothing to check fields against


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
        lake, ds, [raw_fetch("https://data.cdc.gov/resource/b.json", RECORDS)], [page], partial=True
    )
    assert not (lake / "raw_records" / "toy_cases" / f"{p.snapshot_id}.ndjson").exists()
    # Two pages become one file in page order, written once.
    other = tmp_path / "q.ndjson"
    write_ndjson(other, RECORDS[:1])
    out = keep_raw_records(lake, ds, "20260101T000000Z-000000000000", [page, other])
    assert out == lake / "raw_records" / "toy_cases" / "20260101T000000Z-000000000000.ndjson"
    assert out.read_bytes() == page.read_bytes() + other.read_bytes()
    assert keep_raw_records(lake, ds, "20260101T000000Z-000000000000", [other]) == out
    assert out.read_bytes() == page.read_bytes() + other.read_bytes()
    assert not list(out.parent.glob(".*.tmp"))


def test_a_snapshot_built_before_raw_records_gets_them_on_the_next_refresh(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    lake.mkdir()
    fetch = raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)
    m = build_snapshot(lake, ds, [fetch], [page])
    kept = lake / "raw_records" / "toy_cases" / f"{m.snapshot_id}.ndjson"
    kept.unlink()  # as in a lake from before this change
    again = build_snapshot(lake, ds, [fetch], [page])  # same inputs: the early return
    assert again == m and kept.read_bytes() == page.read_bytes()
    found = latest_snapshot(lake, "toy_cases")
    assert found is not None and found[1].snapshot_id == m.snapshot_id
    # A partial sample takes the early return without keeping records.
    pm = build_snapshot(lake, ds, [fetch], [page], partial=True)
    (lake / "raw_records" / "toy_cases" / f"{pm.snapshot_id}.ndjson").unlink(missing_ok=True)
    build_snapshot(lake, ds, [fetch], [page], partial=True)
    assert not (lake / "raw_records" / "toy_cases" / f"{pm.snapshot_id}.ndjson").exists()


def test_raw_records_are_never_kept_for_a_restricted_source(tmp_path: Path) -> None:
    from tests.conftest import CATALOG

    restricted = (
        CATALOG.replace("redistribution: true", "redistribution: false")
        .replace("public-domain-us-federal", "CC-BY-NC-SA-3.0-IGO")
        .replace("hosting: public\n", "")
    )
    ds = load_registry(write_registry(tmp_path / "reg", catalog=restricted)).get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    with pytest.raises(ValueError, match="^toy_cases: its licence forbids keeping a copy"):
        keep_raw_records(lake, ds, "20260101T000000Z-000000000000", [page])
    assert not (lake / "raw_records").exists()
    lake.mkdir()
    fetch = raw_fetch("https://data.cdc.gov/resource/a.json", RECORDS)
    build_snapshot(lake, ds, [fetch], [page])
    build_snapshot(lake, ds, [fetch], [page])  # and on the early return
    assert not (lake / "raw_records").exists()


def test_a_failed_raw_records_copy_leaves_no_temporary_file(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    lake = tmp_path / "lake"
    with pytest.raises(FileNotFoundError):
        keep_raw_records(lake, ds, "20260101T000000Z-000000000000", [page, tmp_path / "missing.ndjson"])
    folder = lake / "raw_records" / "toy_cases"
    assert list(folder.iterdir()) == []


def test_a_full_load_missing_a_declared_field_is_refused_and_a_partial_one_is_not(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    renamed = [{("m2_new" if k == "m2" else k): v for k, v in r.items()} for r in RECORDS]
    page = tmp_path / "p.ndjson"
    write_ndjson(page, renamed)
    lake = tmp_path / "lake"
    lake.mkdir()
    fetch = raw_fetch("https://data.cdc.gov/resource/a.json", renamed)
    with pytest.raises(
        ContractLayoutRefused, match="^toy_cases: the publisher's records no longer have the field m2,"
    ):
        build_snapshot(lake, ds, [fetch], [page])
    assert latest_snapshot(lake, "toy_cases") is None and not (lake / "raw_records").exists()
    m = build_snapshot(lake, ds, [fetch], [page], partial=True)
    assert m.partial and m.row_count == len(RECORDS)
