"""Issue #74 (external audit): pad-function blow-up, a ./lake that shadows verified data, UNION across datasets."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from health_data_registry import store
from health_data_registry.hub import Hub
from health_data_registry.query import Engine, QueryRefused
from health_data_registry.registry import Registry
from health_data_registry.sql_guard import (
    MAX_GROWTH,
    MAX_PAD_LENGTH,
    SqlRefused,
    guard_sql,
    harden,
    run_guarded,
)
from health_data_registry.store import (
    build_snapshot,
    default_lake,
    lake_root,
    latest_snapshot,
    sha256_file,
    write_ndjson,
)
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


def allowed(sql: str) -> str:
    return guard_sql(sql, allowed_tables=TABLES, declared_joins=JOINS).sql


# ------------------------------------------------------------------ item 1: output-size blow-up


def test_audit74_padLengthHuge_refused() -> None:
    error = refused("SELECT lpad('x', 2000000000, 'y') AS big")
    assert error.code == "too_large" and f"{MAX_PAD_LENGTH:,}" in error.message


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT rpad(state, {MAX_PAD_LENGTH + 1}) FROM deaths",
        "SELECT lpad(state, year) FROM deaths",  # a length the guard can't read is refused too
        "SELECT lpad('x', 2 * 1000000000)",
    ],
)
def test_audit74_padLength_notASmallLiteral_refused(sql: str) -> None:
    assert refused(sql).code == "too_large"


@pytest.mark.parametrize(
    "sql",
    [f"SELECT lpad(state, {MAX_PAD_LENGTH}, '0') FROM deaths", "SELECT rpad(state, 5) FROM deaths"],
)
def test_audit74_padLength_small_allowed(sql: str) -> None:
    assert "PAD(" in allowed(sql)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT string_agg(state, ',') FROM deaths", "too_large"),
        ("SELECT group_concat(state) FROM deaths", "too_large"),
        ("SELECT list(state) FROM deaths", "too_large"),
        ("SELECT array_agg(state) FROM deaths", "too_large"),
        ("SELECT levenshtein(state, state) FROM deaths", "too_slow"),
        (
            "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT n FROM r",
            "too_large",
        ),
        ("SELECT replace(state, 'S', state) FROM deaths", "too_large"),
        ("SELECT regexp_replace(state, 'S', state) FROM deaths", "too_large"),
        ("SELECT repeat('x', 1000000000)", "forbidden_function"),
        ("SELECT range(1000000000)", "forbidden_function"),
        ("SELECT generate_series(1, 10)", "forbidden_function"),
        ("SELECT printf('%2000000000d', 1)", "forbidden_function"),
        ("SELECT format('{:>2000000000}', 1)", "forbidden_function"),
        ("SELECT list_range(1, 10)", "forbidden_function"),
        ("SELECT array_fill(1, 5)", "forbidden_function"),
    ],
)
def test_audit74_unboundedFunctions_refused(sql: str, code: str) -> None:
    error = refused(sql)
    assert error.code == code and error.message


def doubling_chain(steps: int) -> str:
    """WITH c0 AS (...), c1 AS (SELECT s || s ...), ...: each step doubles the text."""
    ctes = ["c0 AS (SELECT state AS s FROM deaths)"]
    ctes += [f"c{i} AS (SELECT s || s AS s FROM c{i - 1})" for i in range(1, steps + 1)]
    return f"WITH {', '.join(ctes)} SELECT length(s) FROM c{steps}"


def test_audit74_growthBudget_boundary() -> None:
    steps = MAX_GROWTH.bit_length() - 1  # 2 ** steps == MAX_GROWTH
    assert 2**steps == MAX_GROWTH
    assert allowed(doubling_chain(steps))
    assert refused(doubling_chain(steps + 1)).code == "too_large"


@pytest.mark.parametrize(
    ("sql", "ok"),
    [
        ("SELECT state || ' ' || year || ' ' || state FROM deaths", True),  # 3 columns: x3
        ("SELECT concat(state, '-', year) FROM deaths", True),
        ("SELECT concat_ws(', ', state, year) FROM deaths", True),
        ("SELECT [1, 2, 3], {'a': 1} FROM deaths", True),
        ("SELECT replace(state, 'S', 'T'), regexp_replace(state, '\\s+', ' ') FROM deaths", True),
        ("SELECT replace(state, 'S', '" + "x" * MAX_GROWTH + "') FROM deaths", True),
        ("SELECT replace(state, 'S', '" + "x" * (MAX_GROWTH + 1) + "') FROM deaths", False),
        ("SELECT replace(state, 'SS', '" + "x" * (2 * MAX_GROWTH) + "') FROM deaths", True),
        ("SELECT regexp_replace(state, 'S', '" + "x" * MAX_GROWTH + "') FROM deaths", False),
        ("SELECT regexp_replace(state, 'S', '" + "x" * (MAX_GROWTH - 1) + "') FROM deaths", True),
        ("SELECT concat(" + ", ".join(["state"] * (MAX_GROWTH + 1)) + ") FROM deaths", False),
        ("SELECT [" + ", ".join(["state"] * (MAX_GROWTH + 1)) + "] FROM deaths", False),
        ("SELECT {" + ", ".join(f"'k{i}': state" for i in range(MAX_GROWTH + 1)) + "} FROM deaths", False),
        ("SELECT concat_ws(state, " + ", ".join(["'a'"] * (MAX_GROWTH + 2)) + ") FROM deaths", False),
        ("SELECT concat_ws(',', " + ", ".join(["state"] * MAX_GROWTH) + ") FROM deaths", True),
        ("SELECT concat_ws(',', " + ", ".join(["state"] * (MAX_GROWTH + 1)) + ") FROM deaths", False),
    ],
)
def test_audit74_growth_perFunction(sql: str, ok: bool) -> None:
    if ok:
        assert allowed(sql)
    else:
        assert refused(sql).code == "too_large"


def test_audit74_smallPad_runsOnHardenedConnection() -> None:
    con = duckdb.connect(":memory:")
    con.execute("CREATE VIEW deaths AS SELECT 'S1' AS state, 2001 AS year, 3 AS deaths")
    harden(con, memory_limit="256MB", threads=1)
    q = guard_sql("SELECT lpad(state, 5, '0') AS s FROM deaths", allowed_tables=TABLES)
    assert run_guarded(con, q) == (["s"], [("000S1",)])


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


def test_audit74_lakeRoot_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HDR_LAKE", raising=False)
    assert lake_root() == default_lake() == tmp_path / "data-home" / "health-data-registry" / "lake"
    monkeypatch.setenv("HDR_LAKE", "lake")  # relative: would read the working folder, so it is ignored
    assert lake_root() == default_lake()
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "mine"))
    assert lake_root() == (tmp_path / "mine").resolve()
    monkeypatch.setenv("XDG_DATA_HOME", "relative")
    assert default_lake() == Path.home() / ".local" / "share" / "health-data-registry" / "lake"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", ("Library", "Application Support", "health-data-registry", "lake")),
        ("win32", ("AppData", "Local", "health-data-registry", "lake")),
    ],
)
def test_audit74_defaultLake_perPlatform(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected: tuple[str, ...]
) -> None:
    monkeypatch.setattr(store.sys, "platform", platform)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_lake() == Path.home().joinpath(*expected)


def test_audit74_defaultLake_windowsLocalAppData(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert default_lake() == tmp_path / "local" / "health-data-registry" / "lake"


def snapshot_of(lake: Path) -> tuple[Path, dict[str, object]]:
    found = latest_snapshot(lake, "toy_cases")
    assert found is not None
    path = found[0]
    return path, json.loads((path / "manifest.json").read_text())


def lake_refusal(registry: Registry, lake: Path) -> QueryRefused:
    eng = Engine(registry=registry, lake=lake, hub=None)
    with pytest.raises(QueryRefused) as e:
        eng.query_metric("toy_cases", "case_count", filters={"condition": "Legionellosis", "year": 2023})
    assert e.value.code == "lake_unverified" and "No numbers were read" in str(e.value)
    assert "HDR_LAKE" in e.value.next_step
    return e.value


def test_audit74_verifiedLake_answers(lake: Path, registry: Registry) -> None:
    path, doc = snapshot_of(lake)
    assert doc["files"] == {n: sha256_file(path / n) for n in ("data.duckdb", "data.parquet")}
    out = Engine(registry=registry, lake=lake, hub=None).query_metric(
        "toy_cases", "case_count", filters={"condition": "Legionellosis", "year": 2023}
    )
    assert out["rows"] == [[710, 0]]


@pytest.mark.parametrize("name", ["data.duckdb", "data.parquet"])
def test_audit74_lakeFileChanged_refused(lake: Path, registry: Registry, name: str) -> None:
    path, _ = snapshot_of(lake)
    with (path / name).open("ab") as f:
        f.write(b"\0")
    assert "does not match" in str(lake_refusal(registry, lake))


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"dataset": "other"}, "different dataset"),
        ({"snapshot_id": "20200101T000000Z-000000000000"}, "different dataset or snapshot"),
        ({"contract_version": "9.9.9"}, "version 9.9.9"),
        ({"catalog_status": "reviewed"}, "maintainer review"),
        ({"files": {}}, "no fingerprint"),
    ],
)
def test_audit74_lakeManifest_mismatch_refused(
    lake: Path, registry: Registry, change: dict[str, object], why: str
) -> None:
    path, doc = snapshot_of(lake)
    (path / "manifest.json").write_text(json.dumps({**doc, **change}))
    assert why in str(lake_refusal(registry, lake))


def test_audit74_lakeFileMissing_refused(lake: Path, registry: Registry) -> None:
    path, _ = snapshot_of(lake)
    (path / "data.parquet").unlink()
    assert "does not match" in str(lake_refusal(registry, lake))


def test_audit74_lakeDamagedPointer_refused(lake: Path, registry: Registry) -> None:
    (lake / "datasets" / "toy_cases" / "LATEST").write_text("not-a-snapshot\n")
    assert "damaged" in str(lake_refusal(registry, lake))


def test_audit74_hubFile_rehashedOnReconnect(tmp_path: Path) -> None:
    from tests.test_hub import engine, export_public, load_registry, make_lake, write_hub_registry
    from tests.test_publish import RUN

    reg = load_registry(write_hub_registry(tmp_path / "reg"))
    out = tmp_path / "hub-release"
    export_public(reg, make_lake(tmp_path / "maint-lake", reg, tmp_path), str(out), run_date=RUN)
    eng = engine(reg, tmp_path, out)
    assert eng.manifest("toy_cases").origin == "hub"
    file = eng._hub_files["toy_cases"][0]
    eng._reconnect()
    assert "toy_cases" in eng._hub_files and file.is_file()  # intact: kept
    with file.open("ab") as f:
        f.write(b"\0")  # changed in the cache after it was checked
    eng._reconnect()
    assert "toy_cases" not in eng._hub_files and not file.is_file()
    assert eng.manifest("toy_cases").origin == "hub"  # downloaded and checked afresh
    assert sha256_file(eng._hub_files["toy_cases"][0]) == sha256_file(
        out / "toy_cases" / "data" / "toy_cases.parquet"
    )


def test_audit74_hubStillIntact_unknownFile(tmp_path: Path) -> None:
    h = Hub(url=str(tmp_path), cache=tmp_path / "cache")
    stray = tmp_path / "stray.parquet"
    stray.write_bytes(b"x")
    assert h.still_intact(stray) is False and not stray.exists()


# ------------------------------------------------------------------ item 3: set operations across datasets


def test_audit74_unionAllOuterGroupBy_undeclaredPair_refused() -> None:
    sql = (
        "SELECT state, sum(n) AS n FROM (SELECT state, deaths AS n FROM deaths "
        "UNION ALL SELECT state, rate AS n FROM vax) u GROUP BY state"
    )
    assert refused(sql).code == "undeclared_join"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT state FROM deaths UNION ALL SELECT state FROM vax",
        "SELECT state FROM deaths INTERSECT SELECT state FROM vax",
        "SELECT state FROM deaths EXCEPT SELECT state FROM vax",
        "SELECT state FROM pop UNION SELECT state FROM deaths UNION SELECT state FROM vax",
        "WITH a AS (SELECT state FROM deaths), b AS (SELECT state FROM vax) SELECT * FROM a UNION ALL SELECT * FROM b",
        "WITH u AS (SELECT state FROM deaths UNION ALL SELECT state FROM vax) SELECT count(*) FROM u",
        "SELECT (SELECT count(*) FROM deaths), (SELECT count(*) FROM vax)",
        "SELECT state FROM deaths WHERE year IN (SELECT year FROM pop UNION SELECT year FROM vax)",
        "WITH unused AS (SELECT state FROM vax) SELECT state FROM deaths",
    ],
)
def test_audit74_anyScope_undeclaredPair_refused(sql: str) -> None:
    error = refused(sql)
    assert error.code == "undeclared_join" and "vax" in error.message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT state FROM deaths UNION ALL SELECT state FROM pop",
        "SELECT state, sum(n) FROM (SELECT state, deaths AS n FROM deaths UNION ALL SELECT state, population FROM pop) u GROUP BY state",
        "SELECT state FROM vax UNION ALL SELECT state FROM vax",
        "SELECT state FROM deaths WHERE year IN (SELECT year FROM pop) UNION SELECT state FROM pop",
    ],
)
def test_audit74_declaredOrSameTable_setOperation_allowed(sql: str) -> None:
    assert allowed(sql)
