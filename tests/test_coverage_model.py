"""The coverage model (#223, #247): one list of place keys (dim_place), checked declarations, and the same rule in
the server (coverage.Coverage.holds) and in dbt (fct_dataset_place_year_coverage). Synthetic rows only."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from health_data_registry import coverage, geo, tools
from health_data_registry import transform_gen as tg
from health_data_registry.registry import (
    Registry,
    RegistryError,
    _check_coverage_years,
    load_registry,
    parse_contract,
)

REPO = Path(__file__).resolve().parents[1]
MODELS = REPO / "transform" / "models"


@pytest.fixture(scope="module")
def reg() -> Registry:
    return load_registry(REPO)


def _doc(name: str) -> dict[str, Any]:
    return dict(yaml.safe_load((REPO / "contracts" / f"{name}.odcs.yaml").read_text(encoding="utf-8")))


def _top(doc: dict[str, Any], prop: str) -> dict[str, Any]:
    return next(cp for cp in doc["customProperties"] if cp["property"] == prop)


# --------------------------------------------------------------------------------------------- place keys (#247)


def test_geo_reads_its_places_from_dim_place_and_every_key_it_emits_is_there() -> None:
    places = _top(_doc("models/dim_place"), "places")["value"]
    assert [(p.name, p.postal, p.fips) for p in geo.PLACES[1:]] == [tuple(s) for s in places["states"]]
    assert {c.key for c in geo.CITIES} == {c["key"] for c in places["cities"]}
    keys = geo.fixed_keys()
    emitted = {geo.key_of(p) for p in geo.PLACES} | {c.key for c in geo.CITIES}
    for p in geo.PLACES:
        emitted |= {k for _n, k in geo.places_named(f"cases in {p.name}")[0]}
    for c in geo.CITIES:
        emitted |= {k for _n, k in geo.places_named(f"cases in {c.name}")[0]}
    assert emitted <= keys
    assert all(geo.KEY_GRAMMAR.fullmatch(k) for k in keys)
    assert {"IND", "WHO-AFR", "US-66"} <= keys and "PRI" in keys  # a country, a WHO region, a territory
    # No place list is written in Python any more: the state names live in the contract only.
    assert "Alabama" not in (REPO / "src" / "health_data_registry" / "geo.py").read_text(encoding="utf-8")


def test_a_territory_is_its_own_place_related_to_the_united_states() -> None:
    """Maintainer decision, 27 Nov 2026: USA is the nation as each publisher reports it, never redefined here; a
    territory is related to it but never silently summed into it or taken out of it."""
    assert geo.parent_key("US-26") == "USA" and geo.parent_key("US-11") == "USA"
    for t in ("US-72", "US-66", "US-78", "US-60", "US-69"):
        assert geo.parent_key(t) is None and geo.territory_of(t) == "USA" and not geo.within("USA", t)
    assert geo.territory_of("US-26") is None
    assert geo.key_for("United States") == "USA" and geo.places_named("cases in the U.S.")[0] == [
        ("United States", "USA")
    ]
    assert geo.level_of("US-36/nyc") == "city" and geo.level_of("US-36005") == "county"
    assert geo.level_of("NGA") == "country" and geo.level_of("WHO-EUR") == "who_region"
    assert geo.is_place_key("US-36005") and not geo.is_place_key("US-99005") and not geo.is_place_key("usa")


def test_each_national_declaration_says_what_its_national_row_includes(reg: Registry) -> None:
    nndss = reg.get("cdc_nndss_weekly").contract.coverage
    census = reg.get("census_county_pop").contract.coverage
    assert nndss is not None and nndss.national_scope is not None and census is not None
    assert "territor" in nndss.national_scope.includes and nndss.national_scope.basis_quote
    assert census.national_scope is not None and "no territory" in census.national_scope.includes
    out = tools.suggest_datasets(reg, "Syphilis in the United States 2023", fmt="json")
    assert isinstance(out, dict)
    got = {r[0]: r[2] for r in out["rows"]}
    assert got["cdc_nndss_weekly"].startswith("fits; its United States figure is the US RESIDENTS row")


def test_dim_place_sql_lists_every_fixed_key_once(reg: Registry) -> None:
    contract = dict(tg.model_contracts(REPO))
    sql = tg.dim_place_sql(next(c for p, c in contract.items() if c.id == "dim_place"))
    for key in geo.fixed_keys():
        assert sql.count(f"('{key.replace(chr(39), chr(39) * 2)}', ") == 1, key


# --------------------------------------------------------------------------------------------- validation (#247)


def _with_conformed(value: Any) -> dict[str, Any]:
    doc = copy.deepcopy(_doc("cdc_nndss_weekly"))
    cps = [cp for cp in doc["schema"][0]["customProperties"] if cp["property"] != "conformedDimensions"]
    doc["schema"][0]["customProperties"] = [*cps, {"property": "conformedDimensions", "value": value}]
    return doc


@pytest.mark.parametrize(
    ("place", "why"),
    [
        (
            {"match": "state_name", "column": "states", "places": [{"key": "US-26"}]},
            "must map each publisher label",
        ),
        (
            {"match": "state_name", "column": "states", "places": {"Ohio": {"key": "US-6"}}},
            "not a conformed place",
        ),
        (
            {"match": "state_name", "column": "states", "places": {"US": {"key": "usa"}}},
            "not a conformed place",
        ),
        (
            {"match": "state_name", "column": "states", "places": {"X": {"key": "US-99"}}},
            "not a conformed place",
        ),
        ({"match": "state_name", "column": "states", "places": {"X": {"kee": "US-26"}}}, "unknown key 'kee'"),
        (
            {"match": "state_name", "column": "states", "places": {"X": {"note": "a"}}},
            "names its key, or the parent",
        ),
        (
            {"match": "state_name", "column": "states", "places": {"X": {"key": "US-26", "level": "city"}}},
            "level",
        ),
        ({"match": "state_name", "column": "nowhere"}, "'nowhere' is not a contract column"),
        ({"match": "state_name"}, "match state_name needs column"),
        ({"match": "by_name", "column": "states"}, "match must be one of"),
        ({"match": "fixed", "fixd": {"key": "USA"}}, "unknown key 'fixd'"),
        ("USA", "must be a mapping"),
    ],
)
def test_a_malformed_conformed_place_is_refused_when_the_registry_loads(place: Any, why: str) -> None:
    with pytest.raises(RegistryError, match=re.escape(why)):
        parse_contract(_with_conformed({"place": place}), expected_id="cdc_nndss_weekly")


def test_conformed_dimensions_are_parsed_into_typed_places() -> None:
    c = parse_contract(_doc("cdc_nndss_weekly"), expected_id="cdc_nndss_weekly")
    assert c.conformed is not None and c.conformed.match == "state_name" and c.conformed.column == "states"
    assert c.conformed.year_column == "year"
    assert {(p.label, p.key) for p in c.conformed_places} == {
        ("US RESIDENTS", "USA"),
        ("Commonwealth of Northern Mariana Islands", "US-69"),
    }
    pooled = parse_contract(
        _with_conformed(
            {
                "place": {
                    "match": "fixed",
                    "fixed": {"name": "EIP sites", "parent": "USA", "level": "catchment"},
                }
            }
        ),
        expected_id="cdc_nndss_weekly",
    )
    assert pooled.conformed_places[0].key is None  # a part of a place with no key of its own: never coverage


def test_a_misspelled_conformed_dimensions_property_is_refused() -> None:
    doc = _with_conformed({"place": {"match": "fixed", "fixed": {"key": "USA"}}})
    doc["schema"][0]["customProperties"][-1]["property"] = "conformedDimension"
    with pytest.raises(RegistryError, match="did you mean conformedDimensions"):
        parse_contract(doc, expected_id="cdc_nndss_weekly")


PLACE = {
    "placeKey": "US-26",
    "part": "whole",
    "validFrom": 2022,
    "validTo": None,
    "basisUrl": "https://data.cdc.gov/resource/x9gk-5huc.json",
    "basisQuote": "MICHIGAN is a reporting area.",
    "declaredInVersion": "0.1.0",
    "declaredAt": "2026-11-27",
}


def _with_coverage(value: Any) -> dict[str, Any]:
    doc = copy.deepcopy(_doc("cdc_nndss_weekly"))
    doc["customProperties"] = [
        *(cp for cp in doc["customProperties"] if cp["property"] != "coverage"),
        {"property": "coverage", "value": value},
    ]
    return doc


def _full(*places: dict[str, Any], **over: Any) -> dict[str, Any]:
    return {"kind": "full", "denominator": "not_stated", "places": list(places), **over}


@pytest.mark.parametrize(
    ("value", "why"),
    [
        (_full(PLACE, kind="national"), "coverage.kind must be one of"),
        (_full(PLACE, denominator="census"), "coverage.denominator must be one of"),
        (_full(PLACE, timeUnit="week"), "coverage.timeUnit must be one of"),
        (_full(PLACE, sites=[]), "unknown key 'sites'"),
        ({"kind": "full", "denominator": "not_stated", "places": {"US-26": PLACE}}, "must be a list"),
        (_full({**PLACE, "placeKey": "Michigan"}), "not a conformed place key"),
        (_full({**PLACE, "part": "some"}), "part must be one of"),
        (_full({**PLACE, "counties": ["US-26163"]}), "a whole place lists no counties"),
        (_full({**PLACE, "part": "counties", "counties": ["US-39049"]}), "not a county inside US-26"),
        (_full({**PLACE, "part": "counties", "counties": []}), "non-empty list"),
        (_full({**PLACE, "part": "counties", "counties": ["US-26163", "US-26163"]}), "listed twice"),
        (_full({**PLACE, "validTo": 2020}), "validTo is null or a year not before it"),
        (_full({**PLACE, "validFrom": "2022"}), "validFrom is a whole year"),
        (_full({**PLACE, "basisUrl": "http://data.cdc.gov/x"}), "cites its basis"),
        (_full({**PLACE, "basisQuote": " "}), "cites its basis"),
        (_full({**PLACE, "declaredInVersion": "0.2.0"}), "not later than this contract's 0.1.0"),
        (_full({**PLACE, "declaredInVersion": "v1"}), "declaredInVersion must be"),
        (_full({**PLACE, "declaredAt": "27 Nov 2026"}), "declaredAt must be"),
        (_full(PLACE, {**PLACE, "validFrom": 2024}), "overlapping intervals"),
        (_full(PLACE, kind="study_reported"), "declares no places"),
        (_full(PLACE, kind="unknown"), "declares no places"),
        (_full(PLACE, kind="catchment"), "never the whole place"),
        (
            _full(kind="catchment", denominator="census_place"),
            "only full coverage can use the Census population",
        ),
    ],
)
def test_a_malformed_coverage_block_is_refused_when_the_registry_loads(value: Any, why: str) -> None:
    with pytest.raises(RegistryError, match=re.escape(why)):
        parse_contract(_with_coverage(value), expected_id="cdc_nndss_weekly")


def test_intervals_that_follow_each_other_are_one_place_over_time() -> None:
    later = {**PLACE, "validFrom": 2024, "declaredInVersion": "0.1.0"}
    c = parse_contract(
        _with_coverage(_full({**PLACE, "validTo": 2023}, later)), expected_id="cdc_nndss_weekly"
    )
    assert c.coverage is not None and [p.valid_from for p in c.coverage.places] == [2022, 2024]


def test_coverage_never_claims_a_year_the_catalog_doesnt_have(reg: Registry) -> None:
    census = reg.get("census_county_pop")
    doc = _doc("census_county_pop")
    place = _top(doc, "coverage")["value"]["places"][0]
    for bad in ({"validFrom": 2019}, {"validTo": None}, {"validTo": 2026}):
        _top(doc, "coverage")["value"]["places"][0] = {**place, **bad}
        with pytest.raises(RegistryError, match="outside the catalog's years"):
            _check_coverage_years(census.entry, parse_contract(doc, expected_id="census_county_pop"))


# --------------------------------------------------------------------------------------------- declarations


def test_only_structural_facts_are_declared_places(reg: Registry) -> None:
    """Hard rule 1: places come only from an exact structural fact with its basis; catchments stay unknown."""
    declared = {ds.id: ds.contract.coverage for ds in reg if ds.contract.coverage is not None}
    with_places = {k for k, v in declared.items() if v.places}
    assert with_places == {"cdc_nndss_weekly", "census_county_pop", "mdhhs_measles_county"}
    for k in ("cdc_haicviz_cdi", "cdc_haicviz_isa", "cdc_haicviz_mugsi", "cdc_respnet"):
        assert declared[k].kind == "catchment" and not declared[k].places
    assert declared["cdc_respnet"].denominator == "catchment_population"
    assert {k for k, v in declared.items() if v.kind == "study_reported"} == {
        "apic_abstracts",
        "icid_abstracts",
        "idweek_abstracts",
        "shea_abstracts",
    }
    for v in declared.values():
        for p in v.places:
            host = p.basis_url.split("/")[2]
            assert host in reg.hosts or host.endswith((".census.gov", ".cdc.gov", ".michigan.gov")), host


def test_declared_coverage_reads_the_coverage_block(reg: Registry) -> None:
    nndss = coverage.declared_coverage(reg.get("cdc_nndss_weekly"))
    assert nndss.holds("US-26") and nndss.holds("US-72") and nndss.holds("US-36/nyc")
    assert nndss.holds("US-36") and nndss.partial_note("US-36") == (
        "its New York rows cover only part of New York (57 of its counties), not all of it"
    )
    assert nndss.holds("US-36005") is None  # a county: NNDSS rows stop at reporting areas
    census = coverage.declared_coverage(reg.get("census_county_pop"))
    assert (
        census.holds("US-36005") and census.holds("US-72") is False
    )  # a territory: not declared, not in the file
    measles = coverage.declared_coverage(reg.get("mdhhs_measles_county"))
    assert measles.holds("US-26") and measles.holds("US-26163") and measles.holds("US-39") is False
    assert measles.holds("US-26163/detroit") is None  # a city counts only when declared itself
    assert coverage.declared_coverage(reg.get("cdc_respnet")).holds("US-26") is None


def test_a_place_covered_in_part_fits_with_its_caveat(reg: Registry) -> None:
    out = tools.suggest_datasets(reg, "Syphilis in New York 2023", fmt="json")
    rows = (out if isinstance(out, dict) else json.loads(str(out)))["rows"]
    got = {r[0]: (r[1], r[2]) for r in rows}
    assert got["cdc_nndss_weekly"] == (
        "yes",
        "fits; its New York rows cover only part of New York (57 of its counties), not all of it",
    )
    assert rows[0][0] == "cdc_nndss_weekly"


def test_coverage_declarations_hold_every_contract_block(reg: Registry) -> None:
    sql = tg.coverage_declarations_sql(reg)
    assert sql.count("('cdc_nndss_weekly', ") == 8 and sql.count("('census_county_pop', 'USA', ") == 1
    assert "('cdc_respnet', cast(null as varchar)" in sql  # no place, but its kind and denominator are kept
    assert "who_gho_tb_incidence" not in sql  # no coverage block: unknown


# --------------------------------------------------------------------------------------------- server = dbt


def _render(sql: str) -> str:
    sql = re.sub(r"\{\{\s*ref\('([a-z_]+)'\)\s*\}\}", r"\1", sql)
    return re.sub(r"\{\{\s*var\('coverage_through_year'\)\s*\}\}", "2026", sql)


def _fct(reg: Registry) -> dict[tuple[str, str, int], str]:
    """The dbt coverage models run in DuckDB on the real contracts and a few synthetic Census counties."""
    con = duckdb.connect()
    con.execute(
        "create table int_census_county_pop as select * from (values "
        "('050', '36', '001', 'Albany County', 2023), ('050', '36', '005', 'Bronx County', 2023), "
        "('050', '26', '163', 'Wayne County', 2023), ('050', '06', '037', 'Los Angeles County', 2023)"
        ") as t(sumlev, state_fips, county_fips, county_name, year)"
    )
    dim_place = next(c for _p, c in tg.model_contracts(REPO) if c.id == "dim_place")
    for name, sql in (
        ("dim_place", tg.dim_place_sql(dim_place)),
        ("coverage_declarations", tg.coverage_declarations_sql(reg)),
        ("dim_dataset_coverage", (MODELS / "marts" / "dim_dataset_coverage.sql").read_text()),
        ("fct", (MODELS / "marts" / "fct_dataset_place_year_coverage.sql").read_text()),
    ):
        con.execute(f"create table {name} as {_render(sql)}")
    con.execute("create table fct_dataset_place_year_coverage as select * from fct")
    rows = con.execute("select dataset_id, place_key, year, coverage_status from fct").fetchall()
    return {(d, p, int(y)): s for d, p, y, s in rows}


KEYS = ["USA", "US-26", "US-36", "US-36001", "US-36005", "US-36/nyc", "US-26163", "US-26163/detroit",
        "US-72", "US-66", "US-06037", "US-39"]  # fmt: skip


@pytest.mark.parametrize("source", ["cdc_nndss_weekly", "census_county_pop", "mdhhs_measles_county"])
def test_the_server_and_the_fact_apply_one_rule(reg: Registry, source: str) -> None:
    fact = _fct(reg)
    cov = coverage.declared_coverage(reg.get(source))
    year = max(cov.year_start, 2023)
    for key in KEYS:
        status = fact.get((source, key, year))
        held = cov.holds(key)
        expected = {
            True: {"covered", "partial_catchment"},
            None: {"unknown", None},
            False: {"not_covered", None},
        }[held]
        assert status in expected, (source, key, held, status)
        assert (status == "partial_catchment") == (cov.partial_note(key) is not None), (source, key)
