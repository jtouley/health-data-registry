"""dbt reads each stored source's load from the dataset's LATEST pointer, with its snapshot link on every row."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from dbt.adapters.duckdb.utils import SourceConfig

from health_data_registry.dbt_raw import PROVENANCE, Plugin, empty_sql, load_sql
from health_data_registry.registry import load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import RECORDS, raw_fetch, write_registry

FIELDS = ["label", "m1", "m2", "states", "year"]


def _lake(tmp_path: Path) -> tuple[Path, list[str]]:
    """A lake with two full loads (older, newer) and LATEST on the newer one; returns the snapshot ids."""
    ds = load_registry(write_registry(tmp_path / "reg")).get("toy_cases")
    lake = tmp_path / "lake"
    lake.mkdir()
    sids = []
    for day, records in ((1, RECORDS[:1]), (8, RECORDS)):
        page = tmp_path / f"p{day}.ndjson"
        write_ndjson(page, records)
        when = datetime(2026, 1, day, tzinfo=UTC)
        m = build_snapshot(
            lake, ds, [raw_fetch(f"https://data.cdc.gov/resource/{day}.json", records, when)], [page]
        )
        sids.append(m.snapshot_id)
    return lake, sids


def _rows(sql: str) -> list[tuple[object, ...]]:
    return duckdb.connect().execute(sql + " ORDER BY ALL").fetchall()


def test_reads_only_the_load_latest_names_with_its_snapshot_link(tmp_path: Path) -> None:
    lake, (old, new) = _lake(tmp_path)
    sql, warning = load_sql(lake, "toy_cases", FIELDS)
    assert warning is None
    rows = _rows(sql)
    assert len(rows) == len(RECORDS)
    manifest = (lake / "datasets" / "toy_cases").rglob(f"{new}/manifest.json")
    sha = next(iter(manifest)).read_text()
    assert {r[-2] for r in rows} == {new}
    assert all(len(r[-1]) == 64 and r[-1] in sha for r in rows)  # type: ignore[arg-type,operator]
    assert ("Legionellosis", "300", "3.0", "MICHIGAN", "2022", new) == rows[0][:-1]
    # A maintainer rolls LATEST back: dbt follows it, as the server does.
    (lake / "datasets" / "toy_cases" / "LATEST").write_text(old + "\n")
    sql, warning = load_sql(lake, "toy_cases", FIELDS)
    assert warning is None and {r[-2] for r in _rows(sql)} == {old} and len(_rows(sql)) == 1


def test_no_load_to_read_gives_an_empty_source_and_a_plain_warning(tmp_path: Path) -> None:
    lake, (old, new) = _lake(tmp_path)
    empty = _rows(empty_sql(FIELDS))
    assert empty == []
    names = [d[0] for d in duckdb.connect().execute(empty_sql(FIELDS)).description]
    assert names == [*FIELDS, *PROVENANCE]

    sql, warning = load_sql(tmp_path / "nowhere", "toy_cases", FIELDS)
    assert sql == empty_sql(FIELDS)
    assert warning == (
        f"toy_cases has no load in the lake at {tmp_path / 'nowhere'} yet, so stg_toy_cases is empty. "
        "Run `health-data-registry refresh toy_cases` to fill it."
    )
    (lake / "raw_records" / "toy_cases" / f"{new}.ndjson").unlink()
    sql, warning = load_sql(lake, "toy_cases", FIELDS)
    assert sql == empty_sql(FIELDS)
    assert warning == (
        f"toy_cases: LATEST names {new}, whose records were not kept for dbt, so stg_toy_cases is empty. "
        "Run `health-data-registry refresh toy_cases` to keep them."
    )
    (lake / "datasets" / "toy_cases" / "LATEST").write_text("not a snapshot id\n")
    sql, warning = load_sql(lake, "toy_cases", FIELDS)
    assert sql == empty_sql(FIELDS)
    assert warning is not None and warning.startswith(
        "toy_cases: the lake's LATEST pointer or its snapshot can't"
    )
    assert warning.endswith(
        "so stg_toy_cases is empty. Run `health-data-registry refresh toy_cases` to rebuild it."
    )


def test_a_partial_latest_is_not_read(tmp_path: Path) -> None:
    lake, _ = _lake(tmp_path)
    ds = load_registry(tmp_path / "reg").get("toy_cases")
    page = tmp_path / "p.ndjson"
    write_ndjson(page, RECORDS)
    when = datetime(2026, 1, 9, tzinfo=UTC)
    p = build_snapshot(
        lake, ds, [raw_fetch("https://data.cdc.gov/x.json", RECORDS, when)], [page], partial=True
    )
    sql, warning = load_sql(lake, "toy_cases", FIELDS)
    assert sql == empty_sql(FIELDS)
    assert warning == (
        f"toy_cases: LATEST names {p.snapshot_id}, a partial sample, and dbt reads only full loads, so "
        "stg_toy_cases is empty. Run `health-data-registry refresh toy_cases` without --max-rows to fill it."
    )


def test_field_names_and_paths_are_quoted(tmp_path: Path) -> None:
    lake, (_, new) = _lake(tmp_path)
    sql, _ = load_sql(lake, "toy_cases", ['we"ird', "it's"])
    assert '"we""ird"' in sql and "'it''s': 'VARCHAR'" in sql
    assert _rows(sql)[0][:2] == (None, None)
    assert '"we""ird"' in empty_sql(['we"ird']) and _rows(empty_sql(['we"ird', "it's"])) == []


def test_plugin_hands_dbt_the_latest_load_and_warns_when_there_is_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, (_, new) = _lake(tmp_path)
    plugin = Plugin(name="hdr_raw", plugin_config={"lake": str(lake)})
    config = SourceConfig(
        name="raw",
        identifier="toy_cases",
        schema="raw",
        database=None,
        meta={"source_fields": FIELDS},
        tags=[],
    )
    with pytest.raises(RuntimeError, match="no DuckDB connection yet"):
        plugin.load(config)
    con = duckdb.connect()
    plugin.configure_connection(con)
    assert con.execute("SELECT count(*) FROM duckdb_schemas() WHERE schema_name = 'raw'").fetchone() == (1,)
    rel = plugin.load(config)
    cur = con.cursor()
    cur.register("df", rel)
    cur.execute("CREATE TABLE raw.toy_cases AS SELECT * FROM df")
    assert con.execute("SELECT count(*), min(_snapshot_id) FROM raw.toy_cases").fetchone() == (
        len(RECORDS),
        new,
    )
    assert capsys.readouterr().err == ""
    other = SourceConfig(name="raw", identifier="toy_other", schema="raw", database=None, meta={}, tags=[])
    assert plugin.load(other).fetchall() == []
    assert capsys.readouterr().err.startswith("WARNING: toy_other has no load in the lake at ")
    assert Plugin(name="x", plugin_config={}).lake == Path("../lake")
