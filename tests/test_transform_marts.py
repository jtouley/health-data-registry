"""Conformed keys and marts, run as generated on tiny synthetic tables (the dbt build runs them on fixtures).

The owner's tests for stage (c): every condition label maps to a concept or is explicitly unresolved; the
partial-area flags are present; no fact unions across countedUnits; dim_place covers every place key the facts
use. The contract and no-seeds guards stay in test_transform_gen.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from health_data_registry import transform_gen as tg
from health_data_registry import transform_marts as tm
from health_data_registry.registry import Dataset, Registry, load_registry

REPO = Path(__file__).resolve().parents[1]
MODELS = REPO / "transform" / "models"
REF = re.compile(r"\{\{\s*ref\('([a-z0-9_]+)'\)\s*\}\}")


@pytest.fixture(scope="module")
def reg() -> Registry:
    return load_registry(REPO)


def render(sql: str) -> str:
    """A generated model as plain SQL: each ref() becomes the table of the same name."""
    return REF.sub(lambda m: m.group(1), sql)


def calendar(con: duckdb.DuckDBPyConnection) -> None:
    text = (MODELS / "reference" / "mmwr_calendar.sql").read_text()
    text = text.replace("{{ var('mmwr_first_year') }}", "2022").replace("{{ var('mmwr_last_year') }}", "2026")
    con.execute(f"CREATE TABLE mmwr_calendar AS {text}")


def fips(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "CREATE TABLE stg_census_fips_state AS SELECT * FROM (VALUES ('MI', '26', 'x', 'Michigan'), "
        "('NY', '36', 'x', 'New York'), ('MP', '69', 'x', 'Commonwealth of the Northern Mariana Islands')) "
        "t(postal, state_fips, state_ns, state_name)"
    )
    con.execute(
        "CREATE TABLE stg_census_fips_county AS SELECT * FROM (VALUES ('MI', '26', '163', 'x', 'Wayne County', "
        "'H1', 'A'), ('MI', '26', '001', 'x', 'Alcona County', 'H1', 'A')) "
        "t(postal, state_fips, county_fips, county_ns, county_name, class_code, functional_status)"
    )


def stg(con: duckdb.DuckDBPyConnection, ds: Dataset, rows: list[dict[str, Any]]) -> None:
    """stg_<id> with the contract's columns and types, every column not given NULL."""
    cols = ", ".join(f'"{c.name}" {c.physical_type}' for c in ds.contract.columns)
    con.execute(f'CREATE TABLE "stg_{ds.id}" ({cols})')
    for r in rows:
        names = ", ".join(f'"{k}"' for k in r)
        con.execute(
            f'INSERT INTO "stg_{ds.id}" ({names}) VALUES ({", ".join("?" for _ in r)})', list(r.values())
        )


def build_int(con: duckdb.DuckDBPyConnection, ds: Dataset) -> list[dict[str, Any]]:
    con.execute(f'CREATE TABLE "int_{ds.id}" AS {render(tg.intermediate_sql(ds))}')
    cur = con.execute(f'SELECT * FROM "int_{ds.id}"')
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


ROWS: dict[str, list[dict[str, Any]]] = {
    "cdc_nndss_weekly": [
        {"states": "MICHIGAN", "year": 2024, "week": 3, "label": "Legionellosis"},
        {
            "states": "US RESIDENTS",
            "year": 2024,
            "week": 3,
            "label": "Legionellosis",
            "location2": "US RESIDENTS",
        },
        {"states": "New York", "year": 2024, "week": 3, "label": "Candida auris, clinical"},
        {"states": "NEW YORK CITY", "year": 2024, "week": 3, "label": "Candida auris, screening"},
        {
            "states": "Commonwealth of Northern Mariana Islands",
            "year": 2025,
            "week": 1,
            "label": "Legionellosis",
        },
        {
            "states": "NEW ENGLAND",
            "year": 2024,
            "week": 3,
            "label": "Legionellosis",
            "location2": "NEW ENGLAND",
        },
        {"states": None, "year": None, "week": None, "label": None},
    ],
    "cdc_respnet": [
        {
            "surveillance_network": "FluSurv-NET",
            "state": "Michigan",
            "date": "2024-01-06",
            "season": "2023-24",
        },
        {"surveillance_network": "FluSurv-NET", "state": "Overall", "date": "2023-24", "season": "2023-24"},
        {"surveillance_network": "RSV-NET", "state": "Atlantis", "date": "2024-01-07T00:00:00.000"},
    ],
    "cdc_nhsn_ar_hai": [
        {"source": "national", "year": 2022, "geography": "United States", "phenotype": "MRSA"},
        {"source": "state", "year": 2022, "geography": "Michigan", "phenotype": "MRSA"},
        {"source": "region", "year": 2022, "geography": "East North Central", "phenotype": "MRSA"},
    ],
    "cdc_haicviz_cdi": [{"yearname": 2021, "topic": "t", "viewby": "Total", "series": "Total"}],
    "census_county_pop": [
        {"sumlev": "040", "state_fips": "26", "county_fips": "000", "year": 2023, "population": 10},
        {"sumlev": "050", "state_fips": "26", "county_fips": "163", "year": 2023, "population": 4},
        {"sumlev": "050", "state_fips": "09", "county_fips": "110", "year": 2023, "population": 1},
    ],
}


@pytest.fixture(scope="module")
def built(reg: Registry) -> tuple[duckdb.DuckDBPyConnection, dict[str, list[dict[str, Any]]]]:
    con = duckdb.connect()
    calendar(con)
    fips(con)
    out = {}
    for ds in tm.facts(reg):
        stg(con, reg.get(ds.id), ROWS[ds.id])
        out[ds.id] = build_int(con, ds)
    return con, out


def by(rows: list[dict[str, Any]], **match: Any) -> dict[str, Any]:
    (row,) = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    return row


def test_every_stored_source_with_measures_has_one_fact_and_its_rows_carry_every_conformed_key(
    reg: Registry, built: Any
) -> None:
    _con, ints = built
    assert sorted(ints) == sorted(ROWS)
    for ds_id, rows in ints.items():
        assert len(rows) == len(ROWS[ds_id]), f"{ds_id}: the conformed joins never add or drop a row"
        for r in rows:
            for name, *_ in tm.CONFORMED:
                assert name in r, (ds_id, name)
            assert r["place_status"] in ("resolved", "partial_area", "unresolved")
            assert (r["place_key"] is None) == (r["place_status"] == "unresolved")
            assert r["date_basis"] == reg.get(ds_id).contract.conformed.time.date_basis


def test_places_resolve_by_name_code_declaration_or_stay_unresolved(built: Any) -> None:
    _con, ints = built
    n = ints["cdc_nndss_weekly"]
    assert by(n, states="MICHIGAN")["place_key"] == "US-26"
    assert by(n, states="US RESIDENTS")["place_key"] == "USA"
    nmi = by(n, states="Commonwealth of Northern Mariana Islands")
    assert (nmi["place_key"], nmi["place_label"]) == (
        "US-69",
        "Northern Mariana Islands",
    )  # renamed, declared
    region = by(n, states="NEW ENGLAND")
    assert (region["place_key"], region["place_status"]) == (None, "unresolved")
    assert region["place_note"].startswith("unresolved: the contract does not declare this place label")
    empty = by(n, states=None)
    assert (empty["place_key"], empty["condition_key"], empty["time_key"]) == (None, None, None)
    nhsn = ints["cdc_nhsn_ar_hai"]
    assert [r["place_key"] for r in nhsn] == ["USA", "US-26", None]
    pop = ints["census_county_pop"]
    assert [(r["place_key"], r["place_label"]) for r in pop] == [
        ("US-26", "Michigan"), ("US-26163", "Wayne County, Michigan"), (None, "FIPS 09110")]  # fmt: skip


def test_partial_area_flags_are_present(built: Any) -> None:
    """NNDSS New York excludes New York City; RESP-NET sites are catchment areas; EIP areas are not the US."""
    _con, ints = built
    n = ints["cdc_nndss_weekly"]
    ny = by(n, states="New York")
    assert ny["place_is_partial"] and ny["place_status"] == "partial_area"
    assert (ny["place_key"], ny["place_parent_key"]) == ("US-36/cdc_nndss_weekly:new-york", "US-36")
    assert ny["place_note"].startswith("New York State excluding New York City")
    nyc = by(n, states="NEW YORK CITY")
    assert nyc["place_is_partial"] and nyc["place_key"] == "US-36/cdc_nndss_weekly:new-york-city"
    assert (
        not by(n, states="MICHIGAN")["place_is_partial"]
        and not by(n, states="US RESIDENTS")["place_is_partial"]
    )
    r = ints["cdc_respnet"]
    site = by(r, state="Michigan")
    assert site["place_is_partial"] and site["place_key"] == "US-26/cdc_respnet:michigan"
    assert "catchment" in site["place_note"]
    overall = by(r, state="Overall")
    assert overall["place_key"] == "USA/cdc_respnet:overall" and overall["place_parent_key"] == "USA"
    nowhere = by(r, state="Atlantis")  # a site whose state is not a Census name: partial, but no parent
    assert (nowhere["place_key"], nowhere["place_status"], nowhere["place_is_partial"]) == (
        None,
        "unresolved",
        True,
    )
    (eip,) = ints["cdc_haicviz_cdi"]
    assert eip["place_is_partial"] and eip["place_parent_key"] == "USA"
    assert eip["place_key"] == "USA/cdc_haicviz_cdi:emerging-infections-program-catchment-areas-combined"


def test_time_keys_follow_mmwr_weeks_dates_and_years(built: Any) -> None:
    _con, ints = built
    n = by(ints["cdc_nndss_weekly"], states="MICHIGAN")
    assert (n["mmwr_year"], n["mmwr_week"], n["time_key"], n["time_grain"]) == (
        2024,
        3,
        "2024-W03",
        "mmwr_week",
    )
    r = ints["cdc_respnet"]
    assert by(r, state="Michigan")["time_key"] == "2024-W01"  # 6 Jan 2024 ends MMWR week 1
    assert by(r, state="Atlantis")["time_key"] == "2024-W02"  # a Sunday starts the next week
    season = by(r, state="Overall")
    assert (season["time_key"], season["mmwr_week"], season["time_grain"]) == (None, None, "mmwr_week")
    pop = ints["census_county_pop"][0]
    assert (pop["time_key"], pop["mmwr_year"], pop["time_grain"]) == ("Y2023", None, "year")
    assert ints["cdc_haicviz_cdi"][0]["time_key"] == "Y2021"


def test_conditions_carry_status_and_subtype_apart(built: Any) -> None:
    _con, ints = built
    n = ints["cdc_nndss_weekly"]
    clinical = by(n, states="New York")
    assert clinical["condition_key"] == "cdc_nndss_weekly:Candida auris, clinical"
    assert clinical["condition_case_subtype"] == "clinical"
    assert by(n, states="NEW YORK CITY")["condition_case_subtype"] == "screening"
    assert by(n, states="MICHIGAN")["condition_case_subtype"] is None
    assert clinical["condition_case_status"].startswith("unresolved:")
    assert by(ints["cdc_respnet"], state="Michigan")["condition_case_status"].startswith(
        "laboratory-confirmed"
    )
    (cdi,) = ints["cdc_haicviz_cdi"]
    assert cdi["condition_key"] == "cdc_haicviz_cdi:Clostridioides difficile infection"
    assert all(r["condition_key"] is None for r in ints["census_county_pop"])


CROSSWALK_COLS = ("record_id", "dataset", "label", "state", "mesh_id", "ncbi_taxon_id", "gbd_cause", "synonym",
                  "proposed_by", "source", "reviewer", "reviewed_at", "note")  # fmt: skip


def crosswalk(con: duckdb.DuckDBPyConnection, rows: list[tuple[Any, ...]]) -> None:
    con.execute(f"DROP TABLE IF EXISTS {tm.CROSSWALK_MODEL}")
    cols = ", ".join(f"{c} {'DATE' if c == 'reviewed_at' else 'VARCHAR'}" for c in CROSSWALK_COLS)
    con.execute(f"CREATE TABLE {tm.CROSSWALK_MODEL} ({cols})")
    for r in rows:
        con.execute(f"INSERT INTO {tm.CROSSWALK_MODEL} VALUES ({', '.join('?' for _ in r)})", list(r))


def dim_condition(con: duckdb.DuckDBPyConnection, reg: Registry) -> dict[str, dict[str, Any]]:
    cur = con.execute(render(tm.dim_condition_sql(reg, "h")))
    names = [d[0] for d in cur.description]
    return {r[0]: dict(zip(names, r, strict=True)) for r in cur.fetchall()}


def rule_failures(con: duckdb.DuckDBPyConnection, model: str, relation: str) -> list[str]:
    """Each rule of the model's contract that its rows fail (the same SQL the dbt test runs)."""
    from health_data_registry.quality import condition_sql, measure_sql

    (contract,) = [c for p, c in tg.model_contracts(REPO) if c.id == model]
    failed = []
    for r in contract.quality:
        value = con.execute(measure_sql(r, relation)).fetchone()
        ok = con.execute(
            f"SELECT {condition_sql(r, '?')}".replace("?", str(value[0] if value else None))
        ).fetchone()
        if not (ok and ok[0]):
            failed.append(r.id)
    return failed


def test_every_condition_label_maps_to_a_concept_or_is_unresolved(reg: Registry, built: Any) -> None:
    con, ints = built
    crosswalk(con, [])  # no export yet: every label is unresolved
    dim = dim_condition(con, reg)
    used = {r["condition_key"] for rows in ints.values() for r in rows if r["condition_key"]}
    assert set(dim) == used, "dim_condition covers every condition key the facts use"
    assert {d["mapping_state"] for d in dim.values()} == {"unresolved"}
    assert all(d["mesh_id"] is None and d["gbd_cause"] is None for d in dim.values())
    legion = "cdc_nndss_weekly:Legionellosis"
    assert dim[legion]["is_listed"] and dim[legion]["row_count"] == 4
    con.execute(f"CREATE OR REPLACE TABLE dim_condition AS {render(tm.dim_condition_sql(reg, 'h'))}")
    assert rule_failures(con, "dim_condition", "dim_condition") == []

    crosswalk(
        con,
        [
            ("a", "cdc_nndss_weekly", "Legionellosis", "accepted", "D007877", "445", "Lower respiratory infections",
             None, "string_match", "MeSH", "m", "2026-11-20", None),
            ("b", "cdc_nndss_weekly", "Legionellosis", "accepted", "D000001", None, None, None, "maintainer", "old",
             "m", "2026-01-01", None),  # an earlier acceptance: the latest one wins
            ("c", "cdc_nndss_weekly", "Candida auris, clinical", "proposed", None, "498019", None, None,
             "string_match", None, None, None, None),
            ("d", "cdc_nndss_weekly", "Candida auris, screening", "rejected", "D0", None, None, None, "string_match",
             "x", "m", "2026-11-20", None),
            ("e", "cdc_respnet", "FluSurv-NET", "accepted", "D007251", None, None, None, "maintainer", None, "m",
             "2026-11-20", None),  # no source: not a reviewed record, so not used
            ("f", "cdc_nhsn_ar_hai", "MRSA", "accepted", None, None, None, "staph", "dev_eval_synonym", "dev", "m",
             "2026-11-20", None),  # a synonym is a way of asking, not a concept
        ],
    )  # fmt: skip
    dim = dim_condition(con, reg)
    accepted = dim[legion]
    assert (accepted["mapping_state"], accepted["mesh_id"], accepted["ncbi_taxon_id"], accepted["gbd_cause"]) == (
        "accepted", "D007877", "445", "Lower respiratory infections")  # fmt: skip
    assert (accepted["mapping_source"], accepted["mapping_reviewed_by"]) == ("MeSH", "m")
    clinical = dim["cdc_nndss_weekly:Candida auris, clinical"]
    assert (clinical["mapping_state"], clinical["ncbi_taxon_id"], clinical["open_proposals"]) == (
        "unresolved",
        None,
        1,
    )
    for key in (
        "cdc_nndss_weekly:Candida auris, screening",
        "cdc_respnet:FluSurv-NET",
        "cdc_nhsn_ar_hai:MRSA",
    ):
        assert dim[key]["mapping_state"] == "unresolved" and dim[key]["mesh_id"] is None, key
    con.execute(f"CREATE OR REPLACE TABLE dim_condition AS {render(tm.dim_condition_sql(reg, 'h'))}")
    assert rule_failures(con, "dim_condition", "dim_condition") == []
    con.execute("UPDATE dim_condition SET mapping_state = 'proposed' WHERE condition_key = ?", [legion])
    assert rule_failures(con, "dim_condition", "dim_condition") == ["mapped_or_unresolved"]


def test_dim_place_covers_every_place_key_the_facts_use(reg: Registry, built: Any) -> None:
    con, ints = built
    cur = con.execute(render(tm.dim_place_sql(reg, "h")))
    names = [d[0] for d in cur.description]
    assert names == list(tm.PLACE_COLUMNS)
    places = {r[0]: dict(zip(names, r, strict=True)) for r in cur.fetchall()}
    used = {r["place_key"] for rows in ints.values() for r in rows if r["place_key"]}
    assert used <= set(places), f"not in dim_place: {sorted(used - set(places))}"
    parents = {p["parent_key"] for p in places.values() if p["parent_key"]}
    assert parents <= set(places)
    assert (
        places["USA"]["iso3"] == "USA"
        and places["USA"]["m49"] == "840"
        and places["USA"]["parent_key"] is None
    )
    assert places["US-26"]["gbd_location_name"] == "Michigan" and places["US-26"]["parent_key"] == "USA"
    assert places["US-69"]["gbd_location_name"] is None  # geo.py has no GBD name for it
    assert (
        places["US-26163"]["parent_key"] == "US-26"
        and places["US-26163"]["key_source"] == "census_fips_county"
    )
    ny = places["US-36/cdc_nndss_weekly:new-york"]
    assert ny["is_partial_area"] and ny["parent_key"] == "US-36" and ny["key_source"] == "cdc_nndss_weekly"
    con.execute(f"CREATE OR REPLACE TABLE dim_place AS {render(tm.dim_place_sql(reg, 'h'))}")
    assert rule_failures(con, "dim_place", "dim_place") == []


def test_detroit_out_wayne_and_wayne_county_are_never_added(reg: Registry, built: Any) -> None:
    con, _ints = built
    cur = con.execute(render(tm.dim_place_sql(reg, "h")))
    places = {r[0]: r for r in cur.fetchall()}
    never = tm.PLACE_COLUMNS.index("never_add_with")
    partial = tm.PLACE_COLUMNS.index("is_partial_area")
    trio = {"US-26163", "US-26163/detroit", "US-26163/out-wayne"}
    for key in trio:
        assert set(places[key][never].split(", ")) == trio - {key}, key
    assert places["US-26163/detroit"][partial] and places["US-26163/out-wayne"][partial]
    assert not places["US-26163"][partial]
    assert sum(1 for k in places if k == "US-26163") == 1  # the declared county row only adds never_add_with


def test_no_fact_unions_across_counted_units(reg: Registry) -> None:
    files = tg.generate(reg)
    facts = {
        p.stem: text for p, text in files.items() if p.parent == tm.MARTS_DIR and p.name.startswith("fct_")
    }
    assert set(facts) == {f"fct_{ds.id}" for ds in tm.facts(reg)}
    for ds in tm.facts(reg):
        sql = files[tm.MARTS_DIR / f"fct_{ds.id}.sql"]
        body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
        assert "union" not in body.lower(), ds.id
        assert REF.findall(body) == [f"int_{ds.id}"], ds.id
        assert "as counted_unit" in body and tm.lit(ds.contract.conformed.counted_unit) in body
        doc = yaml.safe_load(files[tm.MARTS_DIR / f"fct_{ds.id}.yml"])
        (model,) = doc["models"]
        cols = {c["name"]: c for c in model["columns"]}
        for key, dim in (
            ("condition_key", "dim_condition"),
            ("place_key", "dim_place"),
            ("time_key", "dim_time"),
        ):
            (test,) = cols[key]["data_tests"]
            assert test["relationships"]["arguments"] == {"to": f"ref('{dim}')", "field": key}
        assert model["meta"]["context"]["counted_unit"] == ds.contract.conformed.counted_unit
        assert model["meta"]["contract"] == ds.entry.contract
    units = [ds.contract.conformed.counted_unit for ds in tm.facts(reg)]
    assert len(set(units)) == len(units), "each fact is its own series with its own counted unit"
    # Only the coverage summary reads several facts, and it adds no counts across them: one row per fact.
    readers = [
        p
        for p, text in files.items()
        if p.suffix == ".sql" and len(set(re.findall(r"ref\('(fct_[a-z0-9_]+)'\)", text))) > 1
    ]
    assert readers == [tm.MARTS_DIR / "dim_dataset_coverage.sql"]


def test_dataset_coverage_has_one_row_per_fact(reg: Registry, built: Any) -> None:
    con, _ints = built
    crosswalk(con, [])
    con.execute(f"CREATE OR REPLACE TABLE dim_condition AS {render(tm.dim_condition_sql(reg, 'h'))}")
    for ds in tm.facts(reg):
        con.execute(f'CREATE OR REPLACE TABLE "fct_{ds.id}" AS {render(tm.fact_sql(ds, "h"))}')
    rows = con.execute(render(tm.dim_dataset_coverage_sql(reg, "h")) + " ORDER BY 1").fetchall()
    assert [r[0] for r in rows] == sorted(ds.id for ds in tm.facts(reg))
    nndss = next(r for r in rows if r[0] == "cdc_nndss_weekly")
    blocking = len(reg.get("cdc_nndss_weekly").contract.blocking_questions)
    assert blocking and nndss[6] == f"draft: {blocking} open question(s) could change a number"
    assert nndss[8:] == (7, "2024-W03", "2025-W01", 3, 5, 2, 2, 6)


def test_marts_are_generated_or_declared_and_hand_written_models_survive(
    tmp_path: Path, reg: Registry
) -> None:
    files = tg.generate(reg)
    for name in ("dim_condition", "dim_place", "dim_dataset_coverage"):
        assert tm.MARTS_DIR / f"{name}.sql" in files and tm.MARTS_DIR / f"{name}.yml" in files
    assert tm.MARTS_DIR / "dim_time.yml" in files and tm.MARTS_DIR / "dim_time.sql" not in files
    hand = [p for p in (REPO / "transform" / "models").rglob("*.sql") if p.relative_to(REPO) not in files]
    assert sorted(p.name for p in hand) == ["dim_time.sql", "flag_all_dash_areas.sql",
                                            "flag_cauris_clinical_screening_swap.sql",
                                            "flag_near_zero_running_totals.sql", "mmwr_calendar.sql"]  # fmt: skip
    assert tg._hand_written(REPO, files) == []  # stage (b)'s hand-written models are outside the delete path


def test_marts_refuse_contracts_that_would_clash(tmp_path: Path, reg: Registry) -> None:
    ds = reg.get("cdc_nndss_weekly")
    with pytest.raises(ValueError, match="already has a column named place_key"):
        tm.check_clash(ds.contract, ["place_key"])
    assert tm.declared_places(tmp_path) == []
    (tmp_path / "contracts" / "models").mkdir(parents=True)
    path = tmp_path / tm.DIM_PLACE_CONTRACT
    path.write_text("schema: [{customProperties: []}]\n")
    with pytest.raises(ValueError, match="declare the places once"):
        tm.declared_places(tmp_path)
    path.write_text(
        "schema: [{customProperties: [{property: declaredPlaces, value: [{key: USA, name: US}]}]}]\n"
    )
    with pytest.raises(ValueError, match="needs key, name and source"):
        tm.declared_places(tmp_path)
    two = "[{key: USA, name: US, source: s}, {key: USA, name: U, source: s}]"
    path.write_text(f"schema: [{{customProperties: [{{property: declaredPlaces, value: {two}}}]}}]\n")
    with pytest.raises(ValueError, match="share a key"):
        tm.declared_places(tmp_path)
