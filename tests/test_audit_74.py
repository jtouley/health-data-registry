"""Issue #74 (external audit): pad-function blow-up, a ./lake that shadows verified data, UNION across datasets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry
from health_data_registry.sql_guard import SqlRefused, guard_sql
from health_data_registry.store import build_snapshot, lake_root, latest_snapshot, write_ndjson
from tests.conftest import RECORDS, raw_fetch

TABLES: dict[str, frozenset[str]] = {
    "deaths": frozenset({"state", "year", "deaths"}),
    "pop": frozenset({"state", "year", "population"}),
    "vax": frozenset({"state", "year", "rate"}),
}
JOINS = frozenset({frozenset({"deaths", "pop"})})


def refused(sql: str) -> SqlRefused:
    """The guard's refusal; an AssertionError when the guard lets the query through."""
    try:
        guard_sql(sql, allowed_tables=TABLES, declared_joins=JOINS)
    except SqlRefused as e:
        return e
    raise AssertionError(f"the guard let this through: {sql}")


# ------------------------------------------------------------------ item 1: output-size blow-up


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="#74 item 1: pad length is not capped")
def test_audit74_padLengthHuge_refused() -> None:
    assert refused("SELECT lpad('x', 2000000000, 'y') AS big").message


# ------------------------------------------------------------------ item 2: a planted ./lake


def plant_lake(where: Path, registry: Registry, tmp_path: Path) -> Path:
    """A ./lake in the folder a clinician opens: made-up numbers, a manifest that claims a review."""
    lake = where / "lake"
    lake.mkdir(parents=True)
    fake = [{**r, "m1": "999999"} for r in RECORDS]
    f = tmp_path / "fake.ndjson"
    write_ndjson(f, fake)
    build_snapshot(
        lake,
        registry.get("toy_cases"),
        [raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json", fake)],
        [f],
    )
    found = latest_snapshot(lake, "toy_cases")
    assert found is not None
    manifest_path = found[0] / "manifest.json"
    doc = json.loads(manifest_path.read_text())
    manifest_path.write_text(json.dumps({**doc, "catalog_status": "reviewed"}))
    return lake


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="#74 item 2: lake_root defaults to ./lake")
def test_audit74_plantedCwdLake_notRead(
    tmp_path: Path, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "clinic-folder"
    plant_lake(folder, registry, tmp_path)
    monkeypatch.chdir(folder)
    monkeypatch.delenv("HDR_LAKE", raising=False)
    eng = Engine(registry=registry, lake=lake_root(), hub=None)
    try:
        out = eng.query_metric(
            "toy_cases", "case_count", filters={"condition": "Legionellosis", "year": 2023}
        )
    except QueryRefused:
        return  # nothing was read from the planted folder
    raise AssertionError(f"the planted ./lake answered: {out['rows']}")


# ------------------------------------------------------------------ item 3: set operations across datasets


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="#74 item 3: set operations skip the join rule")
def test_audit74_unionAllOuterGroupBy_undeclaredPair_refused() -> None:
    sql = (
        "SELECT state, sum(n) AS n FROM (SELECT state, deaths AS n FROM deaths "
        "UNION ALL SELECT state, rate AS n FROM vax) u GROUP BY state"
    )
    assert refused(sql).code == "undeclared_join"
