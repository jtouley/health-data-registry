"""Named extensions (header_row_finder, wide_to_long), the extension registry, and staging extensions in the one
shared projection: the lake, the live read and the generated dbt staging model give the same rows.

Every extension test here runs with sockets disabled (``no_sockets``), so an impure extension fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest
from pydantic import ValidationError

from health_data_registry.adapters.extensions import (
    EXTENSIONS,
    Extension,
    ExtensionRefused,
    StagingStep,
    check_uses,
    register,
    resolved,
)
from health_data_registry.adapters.extensions import header_row_finder as hrf
from health_data_registry.adapters.extensions import wide_to_long as wtl
from health_data_registry.adapters.row_guard import RowLevelRefused
from health_data_registry.registry import Contract, Dataset, load_registry
from health_data_registry.source_config import ExtensionUse, Strict
from health_data_registry.staging import ContractLayoutRefused, raw_fields, stage_entry, staged_source
from tests.conftest import CATALOG, CONTRACT, SEMANTIC, write_registry

pytestmark = pytest.mark.usefixtures("no_sockets")


# ------------------------------------------------------------------------------------------- header_row_finder

GRID = [["Report on cases", ""], ["", "note"], ["County", "N"], ["Wayne", "4"]]


@pytest.mark.parametrize(
    ("args", "grid", "row", "match"),
    [
        ({"first_cell": "County"}, GRID, 2, None),
        ({"first_cell": " County "}, [["", "County"], ["x"]], 0, None),
        ({"first_cell": "County", "max_scan": 2}, GRID, None, "first 2"),
        ({"first_cell": "Year"}, GRID, None, "no row"),
        ({"first_cell": "County"}, [["County"], ["County"]], None, "rows 1, 2"),
    ],
)
def test_header_row_finder(
    args: dict[str, Any], grid: list[list[str]], row: int | None, match: str | None
) -> None:
    a = hrf.Args.model_validate(args)
    if row is None:
        with pytest.raises(ExtensionRefused, match=match):
            hrf.locate(a, grid)
    else:
        assert hrf.locate(a, grid).header_row == row


# ------------------------------------------------------------------------------------------- wide_to_long

ARGS = {"keep": ["STATE"], "stubs": ["POP"], "key_field": "YEAR", "expected_keys": ["2023", "2024"]}


def _run(sql: str, rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    con = duckdb.connect()
    cols = list(rows[0])
    con.execute("CREATE TABLE raw (" + ", ".join(f'"{c}" VARCHAR' for c in cols) + ")")
    con.executemany(
        f"INSERT INTO raw VALUES ({', '.join('?' for _ in cols)})", [[r[c] for c in cols] for r in rows]
    )
    return sorted(con.execute(f"SELECT * FROM {sql}").fetchall(), key=str)


@pytest.mark.parametrize(
    ("args", "fields", "match"),
    [
        (ARGS, ["STATE", "POP2023"], "no longer has the columns POP2024"),
        (ARGS, ["STATE", "POP2023", "POP2024", "POP2025"], "POP2025"),
        (ARGS, ["COUNTY", "POP2023", "POP2024"], "STATE"),
    ],
)
def test_wide_to_long_refuses_a_layout_it_would_misread(
    args: dict[str, Any], fields: list[str], match: str
) -> None:
    with pytest.raises(ExtensionRefused, match=match):
        wtl.staging(wtl.Args.model_validate(args), "raw", fields)


def test_wide_to_long_copies_every_value_and_keeps_nulls() -> None:
    step = wtl.staging(wtl.Args.model_validate(ARGS), "raw", ["STATE", "POP2023", "POP2024", "NOTE"])
    assert step.rows_per_row == 2 and step.fields == ("STATE", "NOTE", "YEAR", "POP")
    rows = [{"STATE": "26", "POP2023": "1,000", "POP2024": None, "NOTE": "x"}]
    assert _run(step.sql, rows) == sorted([("26", "x", "2023", "1,000"), ("26", "x", "2024", None)], key=str)


def test_wide_to_long_several_stubs() -> None:
    args = wtl.Args.model_validate({**ARGS, "stubs": ["POP", "BIRTHS"]})
    step = wtl.staging(args, "raw", ["STATE", "POP2023", "BIRTHS2023", "POP2024", "BIRTHS2024"])
    rows = [{"STATE": "26", "POP2023": "10", "BIRTHS2023": "1", "POP2024": "11", "BIRTHS2024": "2"}]
    assert _run(step.sql, rows) == [("26", "2023", "10", "1"), ("26", "2024", "11", "2")]


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({**ARGS, "expected_keys": []}, "at least 1"),
        ({**ARGS, "expected_keys": ["2023", "2023"]}, "once"),
        ({**ARGS, "expected_keys": ["23"]}, "key_pattern"),
        ({**ARGS, "key_field": "STATE"}, "overwrite"),
        ({**ARGS, "key_pattern": "(\\d)\\1"}, "linear time"),
        ({**ARGS, "extra": True}, "Extra inputs"),
    ],
)
def test_wide_to_long_arguments(args: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        wtl.Args.model_validate(args)


# ------------------------------------------------------------------------------------------- the registry


@pytest.mark.parametrize(
    ("kind", "uses", "match"),
    [
        ("https_file", [{"name": "no_such_thing", "hook": "locate"}], "no extension is named"),
        ("https_file", [{"name": "header_row_finder", "hook": "staging"}], "runs at hook locate"),
        (
            "socrata",
            [{"name": "header_row_finder", "hook": "locate", "args": {"first_cell": "x"}}],
            "does not run",
        ),
        ("https_file", [{"name": "header_row_finder", "hook": "locate", "args": {}}], "first_cell"),
        (
            "https_file",
            [{"name": "header_row_finder", "hook": "locate", "args": {"first_cell": "x"}}] * 2,
            "twice",
        ),
        ("tableau", [{"name": "wide_to_long", "hook": "staging", "args": ARGS}], "does not run"),
    ],
)
def test_a_catalog_entry_choosing_extensions_badly_is_refused(
    kind: str, uses: list[dict[str, Any]], match: str
) -> None:
    with pytest.raises((ExtensionRefused, ValidationError), match=match):
        check_uses(kind, [ExtensionUse.model_validate(u) for u in uses])


def test_register_refuses_a_second_name_and_an_undeclared_staging_step() -> None:
    with pytest.raises(ValueError, match="twice"):
        register(EXTENSIONS["wide_to_long"])
    with pytest.raises(ValueError, match="raw fields"):
        register(Extension("probe_x", "staging", Strict, lambda *a: None, "x"))


def test_resolved_keeps_catalog_order_and_hook() -> None:
    uses = [ExtensionUse(name="wide_to_long", hook="staging", args=ARGS),
            ExtensionUse(name="header_row_finder", hook="locate", args={"first_cell": "x"})]  # fmt: skip
    assert [e.name for e, _a in resolved(uses, "staging")] == ["wide_to_long"]
    assert [e.name for e, _a in resolved(uses, "locate")] == ["header_row_finder"]


# ------------------------------------------------------------------------------------------- shared projection

WIDE_CATALOG = CATALOG.replace(
    """source:
  portal: socrata
  host: data.cdc.gov
  dataset: toy0-toy0
  landing_url: https://data.cdc.gov/d/toy0-toy0
  api_url: https://data.cdc.gov/resource/toy0-toy0.json
""",
    """source:
  type: https_csv
  host: data.cdc.gov
  dataset: toy0-toy0
  landing_url: https://data.cdc.gov/d/toy0-toy0
  api_url: https://data.cdc.gov/resource/toy0-toy0.csv
  extensions:
    - name: wide_to_long
      hook: staging
      args: {keep: [states, label], stubs: [m1], key_field: year, expected_keys: ["2023", "2024"]}
""",
)
WIDE = [
    {"states": "MICHIGAN", "label": "Legionellosis", "m12023": "300", "m12024": "310", "m2": "3.0"},
    {"states": "OHIO", "label": "Legionellosis", "m12023": "400", "m12024": "N", "m2": "3.4"},
    {"states": "US RESIDENTS", "label": "Legionellosis", "m12023": "9000", "m12024": "9100", "m2": "2.7"},
]


# The unpivot makes one row per year, so the contract declares the grain it makes (registry checks it).
WIDE_CONTRACT = (
    CONTRACT.replace(
        "        description: MMWR year of report.\n",
        "        description: MMWR year of report.\n        primaryKey: true\n        primaryKeyPosition: 2\n",
    )
    .replace(
        "        description: The state where the case lived, or US RESIDENTS for the national total.\n",
        "        description: The state where the case lived, or US RESIDENTS for the national total.\n"
        "        primaryKey: true\n        primaryKeyPosition: 1\n",
    )
    .replace(
        "        description: Notifiable condition.\n",
        "        description: Notifiable condition.\n        primaryKey: true\n        primaryKeyPosition: 3\n",
    )
)


@pytest.fixture
def wide(tmp_path: Path) -> Dataset:
    root = write_registry(tmp_path / "wide", catalog=WIDE_CATALOG, contract=WIDE_CONTRACT)
    return load_registry(root).get("toy_cases")


def test_a_contract_key_without_the_unpivots_key_field_is_refused(tmp_path: Path) -> None:
    from health_data_registry.registry import RegistryError

    with pytest.raises(RegistryError, match="primary key includes the column reading year"):
        load_registry(write_registry(tmp_path / "nokey", catalog=WIDE_CATALOG))


def test_reshape_and_a_staging_extension_together_are_refused() -> None:
    from tests.adapters.helpers import entry

    with pytest.raises(ValidationError, match="reshapes its file once"):
        entry("https_csv", None, reshape={"wide_to_long": {"keep": ["STATE"], "stubs": ["POP"], "key_field": "YEAR"}},
              extensions=[{"name": "wide_to_long", "hook": "staging", "args": ARGS}])  # fmt: skip


def test_a_document_type_whose_contract_drops_its_metadata_is_refused(tmp_path: Path) -> None:
    from health_data_registry.registry import RegistryError

    doc = CATALOG.replace("  portal: socrata\n", "  type: https_file\n  https_file: {format: csv}\n")
    with pytest.raises(RegistryError, match="adds document_url, document_sha256, document_retrieved_at"):
        load_registry(write_registry(tmp_path / "doc", catalog=doc))


def test_an_extra_year_in_the_file_stops_the_load_end_to_end(wide: Dataset, tmp_path: Path) -> None:
    """The adapter hands on every column (hard rule 15), so a year the codebook doesn't list reaches the layout
    check of a real load and is refused, never dropped."""
    from health_data_registry.store import build_snapshot, write_ndjson
    from tests.conftest import raw_fetch

    lake = tmp_path / "lake"
    lake.mkdir()
    nd = tmp_path / "wide.ndjson"
    write_ndjson(nd, [{**r, "m12025": "1"} for r in WIDE])
    with pytest.raises(ContractLayoutRefused, match="m12025"):
        build_snapshot(lake, wide, [raw_fetch("https://data.cdc.gov/resource/toy0-toy0.csv", WIDE)], [nd])


def _typed(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple[Any, ...]]:
    return sorted(con.execute(f'SELECT * FROM "{table}"').fetchall(), key=str)


def test_raw_fields_are_what_the_extension_reads(wide: Dataset) -> None:
    contract_fields = sorted({c.source_field for c in wide.contract.columns})
    assert raw_fields(wide.entry, contract_fields) == ["label", "m12023", "m12024", "m2", "states"]


def test_lake_live_and_dbt_give_the_same_rows(wide: Dataset, tmp_path: Path) -> None:
    from health_data_registry.live import load_live
    from health_data_registry.store import build_snapshot, write_ndjson
    from health_data_registry.transform_gen import PROVENANCE, staging_sql
    from tests.conftest import raw_fetch

    # the lake
    lake = tmp_path / "lake"
    lake.mkdir()
    nd = tmp_path / "wide.ndjson"
    write_ndjson(nd, WIDE)
    m = build_snapshot(lake, wide, [raw_fetch("https://data.cdc.gov/resource/toy0-toy0.csv", WIDE)], [nd])
    assert m.row_count == 6
    con = duckdb.connect(str(next(lake.rglob("data.duckdb"))), read_only=True)
    lake_rows = _typed(con, "toy_cases")
    con.close()
    # the live read (in memory)
    mem = duckdb.connect()
    load_live(mem, wide, [dict(r) for r in WIDE])
    live_rows = _typed(mem, "toy_cases")
    # the generated dbt staging model, its source() replaced by the same raw records
    raw = duckdb.connect()
    cols = [*WIDE[0], *PROVENANCE]
    raw.execute("CREATE TABLE raw_toy (" + ", ".join(f'"{c}" VARCHAR' for c in cols) + ")")
    raw.executemany(f"INSERT INTO raw_toy VALUES ({', '.join('?' for _ in cols)})",
                    [[*r.values(), *("x" for _ in PROVENANCE)] for r in WIDE])  # fmt: skip
    sql = staging_sql(wide).replace("{{ source('raw', 'toy_cases') }}", "raw_toy")
    names = [c.name for c in wide.contract.columns]
    dbt_rows = sorted(
        raw.execute(f"SELECT {', '.join(chr(34) + n + chr(34) for n in names)} FROM ({sql})").fetchall(),
        key=str,
    )
    assert lake_rows == live_rows == dbt_rows
    assert ("OHIO", 2024, "Legionellosis", None, 3.4) in lake_rows  # "N" is the contract's missing code


def test_staging_extension_cardinality_is_checked(wide: Dataset, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = Extension(
        "wide_to_long", "staging", wtl.Args,
        lambda args, rel, fields: StagingStep(f"(SELECT * FROM {wtl.staging(args, rel, fields).sql} LIMIT 1)",
                                              wtl.staging(args, rel, fields).fields, 2),
        "drops rows", input_fields=wtl.input_fields,
    )  # fmt: skip
    monkeypatch.setitem(EXTENSIONS, "wide_to_long", bad)
    con = duckdb.connect()
    cols = list(WIDE[0])
    con.execute("CREATE TABLE raw (" + ", ".join(f'"{c}" VARCHAR' for c in cols) + ")")
    con.executemany(
        f"INSERT INTO raw VALUES ({', '.join('?' for _ in cols)})", [list(r.values()) for r in WIDE]
    )
    with pytest.raises(ContractLayoutRefused, match="made 1 rows from 3, not the 6"):
        stage_entry(con, wide.entry, wide.contract, "raw", table="t", present=set(cols))
    assert con.execute("SELECT count(*) FROM duckdb_tables() WHERE table_name = 't'").fetchone() == (0,)


def test_a_layout_the_extension_refuses_stops_the_load(wide: Dataset) -> None:
    con = duckdb.connect()
    con.execute('CREATE TABLE raw ("states" VARCHAR, "label" VARCHAR, "m12023" VARCHAR, "m2" VARCHAR)')
    with pytest.raises(ContractLayoutRefused, match="m12024"):
        stage_entry(
            con, wide.entry, wide.contract, "raw", table="t", present={"states", "label", "m12023", "m2"}
        )


def row_level_staging_is_refused(name: str) -> None:
    """A staging extension whose output fields are one row per person is refused by the core's guard."""
    from tests.adapters.helpers import entry

    args = {"wide_to_long": {"keep": ["Age", "Sex"], "stubs": ["Onset "], "key_field": "Year",
                             "expected_keys": ["2024"]},
            "period_header_parser": {"field": "Age", "year_field": "Sex",
                                     "rules": [{"pattern": "(\\d+)", "kind": "year", "year": 1}]}}[name]  # fmt: skip
    e = entry("https_file", {"format": "csv"}, extensions=[{"name": name, "hook": "staging", "args": args}])
    with pytest.raises(RowLevelRefused):
        stage_entry(
            duckdb.connect(), e, cast(Contract, None), "raw", table="t", present={"Age", "Sex", "Onset 2024"}
        )


def test_row_level_staging_output_is_refused() -> None:
    row_level_staging_is_refused("wide_to_long")


def test_no_staging_extensions_means_the_projection_is_unchanged(tmp_path: Path) -> None:
    ds = load_registry(write_registry(tmp_path / "plain")).get("toy_cases")
    assert staged_source(ds.entry, "raw", {"a"}) == ("raw", {"a"}, 1)
    assert raw_fields(ds.entry, ["b", "a"]) == ["b", "a"]


def test_contract_semantic_unchanged_by_fixture() -> None:
    assert "sourceField, value: m1" in CONTRACT and "case_count" in SEMANTIC and json.dumps(WIDE)


def test_the_guard_on_what_a_step_makes_runs_on_a_partial_load_too() -> None:
    from tests.adapters.helpers import entry

    args = {"keep": ["Age", "Sex"], "stubs": ["Onset "], "key_field": "Year", "expected_keys": ["2024"]}
    e = entry(
        "https_file",
        {"format": "csv"},
        extensions=[{"name": "wide_to_long", "hook": "staging", "args": args}],
    )
    with pytest.raises(RowLevelRefused):  # present=None: a --max-rows sample, whose fields aren't checked
        stage_entry(duckdb.connect(), e, cast(Contract, None), "raw", table="t", present=None)


def test_a_second_staging_extension_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        EXTENSIONS, "probe_stage",
        Extension("probe_stage", "staging", Strict, lambda a, r, f: StagingStep(r, tuple(f), 1), "x",
                  input_fields=lambda a: []),
    )  # fmt: skip
    uses = [
        ExtensionUse(name="wide_to_long", hook="staging", args=ARGS),
        ExtensionUse(name="probe_stage", hook="staging"),
    ]
    with pytest.raises(ExtensionRefused, match="one staging extension at most"):
        check_uses("https_csv", uses)


def test_a_layout_refusal_from_an_extension_is_a_layout_error() -> None:
    from health_data_registry.adapters import LayoutError

    with pytest.raises(LayoutError):
        hrf.locate(hrf.Args.model_validate({"first_cell": "Year"}), GRID)


def test_the_generated_docs_say_what_staging_extensions_do(wide: Dataset) -> None:
    """The raw source describes each wide column by the value it holds and the column it lands in; the staging
    model names its extensions, the rows they make and the grain they add; the SQL header says so too."""
    import yaml

    from health_data_registry.registry import Registry
    from health_data_registry.transform_gen import ADDED_DOCS, raw_sources, staging_sql, staging_yml

    table = yaml.safe_load(raw_sources(cast(Registry, [wide])))["sources"][0]["tables"][0]
    docs = {c["name"]: c["description"] for c in table["columns"]}
    assert "the value of m1 for year = 2023" in docs["m12023"] and "into cases" in docs["m12023"]
    assert "staging reshapes it (wide_to_long)" in table["description"]
    model = yaml.safe_load(staging_yml(wide))["models"][0]
    assert model["meta"]["staging_extensions"] == [
        {"name": "wide_to_long", "hook": "staging", "rows_per_row": 2, "adds_to_grain": ["year"]}
    ]
    assert "one row per raw row and year" in model["description"]
    assert {c["name"]: c.get("meta", {}).get("made_by") for c in model["columns"]}["year"] == "wide_to_long"
    assert "-- Staging steps: wide_to_long (one raw row makes 2 rows" in staging_sql(wide)
    assert all(not d.startswith("Publisher field") for d in ADDED_DOCS.values())
