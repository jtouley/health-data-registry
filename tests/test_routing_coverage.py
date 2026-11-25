"""Routing by declared coverage (#111, #112 items 2 and 3): a source fits a question only when what is declared
about it holds the question's place, condition and years; anything that can't be read is "unknown", never a fit.

The questions are synthetic, written here in the shape of the issue's examples (never eval items). They run against
the real catalog, contracts and semantic models; nothing is fetched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry import coverage, geo, tools
from health_data_registry.live import load_live
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from tests.conftest import CATALOG, write_registry

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ("detroit_dhd_epi_annual", "detroit_dhd_covid_archive_daily", "detroit_dhd_resp_ed_weekly",
         "mdhhs_measles_county")  # fmt: skip
ABSTRACTS = ("idweek_abstracts", "shea_abstracts", "apic_abstracts", "icid_abstracts")


@pytest.fixture(scope="module")
def reg() -> Registry:
    return load_registry(ROOT)


def _json(out: Any) -> dict[str, Any]:
    assert isinstance(out, dict)
    return out


def verdicts(registry: Registry, question: str) -> dict[str, tuple[str, str]]:
    out = _json(tools.suggest_datasets(registry, question, fmt="json"))
    return {r[0]: (r[1], r[2]) for r in out["rows"]}


# --------------------------------------------------------------------------------------------- suggest_datasets


@pytest.mark.parametrize(
    "question",
    [
        "Mpox cases in Nigeria since 2022",
        "Pediatric influenza deaths in the US, 2022 to 2024",
        "Brazil tuberculosis incidence 2015 to 2022",
        "Rubeola in NYC in 2025",
        "Chlamydia in California 2023",
        "Hepatitis C in Colorado, 2021-2023",
        "Tuberculosis in Ohio since 2020",
        "Syphilis rates in Illinois 2022",
        "COVID-19 hospitalisations in Ohio 2024",
        "Measles cases in Illinois 2026",
    ],
)
def test_a_local_source_never_fits_a_place_it_does_not_cover(reg: Registry, question: str) -> None:
    got = verdicts(reg, question)
    for ds_id in LOCAL:
        assert got[ds_id][0] != "yes", (ds_id, got[ds_id])


@pytest.mark.parametrize(
    ("question", "place"),
    [
        ("Mpox cases in Nigeria since 2022", "Nigeria"),
        ("Brazil tuberculosis incidence 2015 to 2022", "Brazil"),
    ],
)
def test_a_place_that_cant_be_read_is_unknown_never_a_fit(reg: Registry, question: str, place: str) -> None:
    out = _json(tools.suggest_datasets(reg, question, fmt="json"))
    assert not [r for r in out["rows"] if r[1] == "yes"]
    assert any(f"Couldn't tell whether '{place}' is a place" in n for n in out["notes"])
    nndss = verdicts(reg, question)["cdc_nndss_weekly"]
    assert nndss[0] != "yes" and f"'{place}'" in nndss[1]


def test_a_gbd_measure_is_handed_to_the_gbd_connector(reg: Registry) -> None:
    out = _json(tools.suggest_datasets(reg, "Years of life lost to tuberculosis in Pakistan", fmt="json"))
    rows = {r[0]: r for r in out["rows"]}
    assert {r[1] for r in rows.values()} == {"no"}
    for ds_id in ABSTRACTS:
        assert "years of life lost is a modelled GBD estimate" in rows[ds_id][2]
    assert any(n.startswith("For years of life lost, ask the IHME GBD connector") for n in out["notes"])


# --------------------------------------------------------------------------------------------- literature (#106)


def _data_sources(registry: Registry) -> list[str]:
    return [ds.id for ds in registry if ds.id not in ABSTRACTS]


@pytest.mark.parametrize(
    "question",
    [
        "Has anyone presented abstracts on doxycycline post-exposure prophylaxis for STI prevention?",
        "Prior work on C. auris in Michigan",
        "Published papers on antimicrobial stewardship in nursing homes",
        "Case reports of melioidosis after a hurricane",
        "Is there literature on syphilis screening in emergency departments?",
    ],
)
def test_a_literature_question_goes_to_the_abstracts_and_the_hand_off(reg: Registry, question: str) -> None:
    out = _json(tools.suggest_datasets(reg, question, fmt="json"))
    got = {r[0]: (r[1], r[2]) for r in out["rows"]}
    assert {ds_id for ds_id, v in got.items() if v[0] == "yes"} == set(ABSTRACTS)
    for ds_id in _data_sources(reg):
        assert got[ds_id] == ("no", "holds numbers (counts, rates or populations), not papers or abstracts")
    assert out["hand_off_to"] == ["pubmed", "openalex"]
    assert any(n.startswith("For published papers, search your PubMed connector") for n in out["notes"])
    assert out["notes"][0].startswith("Read as: a literature question")
    for row in out["rows"]:
        if row[0] in ABSTRACTS:
            assert "the title, DOI and link of each" in row[3] and "query_sql" in row[3]


def test_the_hand_off_carries_the_topic_not_the_question_words(reg: Registry) -> None:
    question = "Has anyone presented abstracts on doxycycline post-exposure prophylaxis at IDWeek 2024?"
    out = _json(tools.suggest_datasets(reg, question, fmt="json"))
    assert out["notes"][0] == (
        "Read as: a literature question (papers, abstracts or prior work) about 'doxycycline post-exposure "
        "prophylaxis', meetings ['IDWeek'], years [2024]."
    )
    assert out["notes"][1] == (
        "For published papers, search your PubMed connector, or OpenAlex if you have it, for 'doxycycline "
        "post-exposure prophylaxis'. This server lists conference abstracts (title, DOI and link), not papers."
    )


def test_a_named_meeting_narrows_to_its_own_abstracts(reg: Registry) -> None:
    got = verdicts(reg, "SHEA Spring abstracts on CLABSI in 2022")
    assert got["shea_abstracts"] == ("yes", "fits")
    assert got["idweek_abstracts"] == ("no", "holds IDWeek abstracts only, not SHEA Spring")
    assert got["apic_abstracts"][0] == "no" and got["icid_abstracts"][0] == "no"
    # Naming the meeting alone makes it a literature question.
    at_idweek = verdicts(reg, "C. auris at IDWeek")
    assert at_idweek["idweek_abstracts"] == ("yes", "fits") and at_idweek["cdc_nndss_weekly"][0] == "no"


def test_abstract_years_outside_the_table_are_a_no(reg: Registry) -> None:
    got = verdicts(reg, "SHEA abstracts on CLABSI in 2019")
    assert got["shea_abstracts"] == ("no", "holds SHEA Spring Conference abstracts for 2020-present only")


def test_a_meeting_with_no_table_here_goes_to_the_hand_off(reg: Registry) -> None:
    out = _json(tools.suggest_datasets(reg, "CROI abstracts on long-acting cabotegravir", fmt="json"))
    assert "yes" not in {r[1] for r in out["rows"]}
    assert (
        "No abstract table here holds CROI: ask PubMed or OpenAlex for its abstracts and papers."
        in out["notes"]
    )
    assert out["hand_off_to"] == ["pubmed", "openalex"]


def test_how_often_abstracts_became_papers_points_to_the_outlook(reg: Registry) -> None:
    out = _json(tools.suggest_datasets(reg, "How often do IDWeek abstracts become full papers?", fmt="json"))
    assert out["notes"][1] == (
        "For how often a meeting's presented abstracts became full papers, use get_publication_outlook with "
        "conference 'IDWeek': it answers from reviewed published base rates."
    )


def test_the_hand_off_shows_in_csv_too(reg: Registry) -> None:
    out = tools.suggest_datasets(reg, "Prior work on C. auris", fmt="csv")
    assert isinstance(out, str) and "hand_off_to: pubmed" in out and "hand_off_to: openalex" in out


@pytest.mark.parametrize(
    "question",
    [
        "Patients presented to the ED with influenza in Detroit",  # "presented to", not "presented at"
        "AIDS cases in Ohio 2022",  # a condition, never the International AIDS Conference
        "Chlamydia in California 2023",
    ],
)
def test_a_data_question_is_not_read_as_literature(reg: Registry, question: str) -> None:
    out = _json(tools.suggest_datasets(reg, question, fmt="json"))
    assert "hand_off_to" not in out
    assert not out["notes"][0].startswith("Read as: a literature question")


def test_generic_words_alone_never_fit(reg: Registry) -> None:
    got = verdicts(reg, "Cases and deaths per year in Ohio")
    assert "yes" not in {v[0] for v in got.values()}
    assert got["cdc_nndss_weekly"] == (
        "unknown",
        "the question names no specific condition (a disease or pathogen)",
    )


def test_a_covered_question_still_fits(reg: Registry) -> None:
    got = verdicts(reg, "Chlamydia in California 2023")
    assert got["cdc_nndss_weekly"] == ("yes", "fits")
    # Detroit's dashboard declares no places yet, so it is unknown, not a fit and not a no.
    assert got["detroit_dhd_epi_annual"] == (
        "unknown",
        "this source doesn't declare which places it covers yet",
    )
    assert verdicts(reg, "C. auris in Michigan in 2024")["cdc_nndss_weekly"][0] == "yes"


@pytest.mark.parametrize(
    ("question", "source"),
    [
        ("Chlamydia in California 2023", "cdc_nndss_weekly"),
        ("C. difficile infections in the United States 2020", "cdc_haicviz_cdi"),
        ("Carbapenem-resistant Enterobacterales in Michigan 2021", "cdc_nhsn_ar_hai"),
        ("RSV hospitalizations in Michigan 2024", "cdc_respnet"),  # "RSV" sits in the publisher's own name
        ("Tuberculosis incidence by country 2020", "who_gho_tb_incidence"),
        ("Population of Michigan in 2022", "census_county_pop"),
        ("Population of Ohio counties 2023", "census_county_pop"),
    ],
)
def test_a_question_reaches_its_source(reg: Registry, question: str, source: str) -> None:
    out = _json(tools.suggest_datasets(reg, question, fmt="json"))
    assert out["rows"][0][:3] == [source, "yes", "fits"], out["rows"][:3]


@pytest.mark.parametrize(
    ("question", "source"),
    [
        ("Tuberculosis in Detroit 2023", "detroit_dhd_epi_annual"),
        ("COVID-19 cases in Detroit 2021", "detroit_dhd_covid_archive_daily"),
        ("Influenza emergency department visits in Detroit 2026", "detroit_dhd_resp_ed_weekly"),
        ("Measles in Michigan 2026", "mdhhs_measles_county"),
    ],
)
def test_a_local_question_reaches_its_source_as_unknown_until_places_are_declared(
    reg: Registry, question: str, source: str
) -> None:
    """The city and state dashboards declare no places yet, so they are offered as unknown: never left out, and
    never ranked under a source that can't answer."""
    rows = _json(tools.suggest_datasets(reg, question, fmt="json"))["rows"]
    order = [r[0] for r in rows]
    assert tuple(rows[order.index(source)][1:3]) == (
        "unknown",
        "this source doesn't declare which places it covers yet",
    )
    assert all(r[1] != "no" for r in rows[: order.index(source)])


def test_a_nationwide_source_is_unknown_for_a_city(reg: Registry) -> None:
    """A country declaration says nothing about which cities a source reports apart from their states."""
    got = verdicts(reg, "Tuberculosis in Detroit 2023")
    assert got["cdc_nndss_weekly"] == (
        "unknown",
        "covers the United States, but doesn't declare which cities it reports apart from their states, so not "
        "whether it reports Detroit",
    )
    assert got["cdc_respnet"][0] == "no" and "city" in got["cdc_respnet"][1]  # its levels stop at state
    assert "cities" not in got["cdc_respnet"][1]
    assert "yes" not in {v[0] for v in got.values()}


def test_a_source_without_declared_places_is_unknown(tmp_path: Path) -> None:
    city = CATALOG.replace("level: federal", "level: city").replace(
        "[nation, state]", "[nation, state, city]"
    )
    registry = load_registry(write_registry(tmp_path / "reg", catalog=city))
    assert verdicts(registry, "syphilis in Ohio 2023")["toy_cases"] == (
        "unknown",
        "this source doesn't declare which places it covers yet",
    )
    # A question that names no place: the same, never a fit.
    assert verdicts(registry, "syphilis 2023")["toy_cases"][0] == "unknown"
    # A federal source names no place either: it covers the whole country, so it fits.
    federal = load_registry(write_registry(tmp_path / "fed"))
    assert verdicts(federal, "syphilis 2023")["toy_cases"] == ("yes", "fits")
    assert verdicts(federal, "syphilis in Lagos 2023")["toy_cases"][0] == "unknown"
    # An international source that names its country does not cover every country: it declares no places.
    intl = load_registry(
        write_registry(tmp_path / "intl", catalog=CATALOG.replace("level: federal", "level: international"))
    )
    assert verdicts(intl, "syphilis in Ohio 2023")["toy_cases"][0] == "unknown"


# --------------------------------------------------------------------------------------------- coverage


CONFORMED = {
    "property": "conformedDimensions",
    "value": {
        "place": {
            "match": "state_name",
            "column": "location_label",
            "places": {
                "Detroit Case Rate": {"key": "US-26163/detroit", "level": "city"},
                "Michigan Case Rate": {"key": "US-26", "level": "state"},
                "Pooled sites": {"parent": "USA"},  # a part of a place, with no key of its own: not coverage
            },
        }
    },
}


def test_declared_coverage_reads_the_contracts_conformed_places(reg: Registry) -> None:
    from dataclasses import replace

    ds = reg.get("detroit_dhd_epi_annual")
    undeclared = coverage.declared_coverage(ds)  # main's contract declares no places yet
    assert undeclared.place_keys is None and undeclared.place_words == "no places declared yet"
    assert undeclared.holds("US-26") is None and not undeclared.nationwide
    schema = dict(ds.contract.raw["schema"][0])
    schema["customProperties"] = [*schema.get("customProperties", []), CONFORMED]
    declared = replace(ds, contract=replace(ds.contract, raw={**ds.contract.raw, "schema": [schema]}))
    cov = coverage.declared_coverage(declared)
    assert cov.place_keys == {"US-26163/detroit", "US-26"}
    assert cov.place_words == "Detroit and Michigan" and not cov.nationwide
    assert cov.holds("US-26") and cov.holds("US-26163/detroit") and cov.holds("US-26163")
    assert cov.holds("US-06") is False and cov.holds("USA") is False
    assert {"tuberculosis", "chlamydia", "syphilis"} <= cov.condition_words
    assert not {"detroit", "health", "cases", "rate"} & cov.condition_words


def test_a_fixed_place_is_declared_coverage(reg: Registry) -> None:
    from dataclasses import replace

    ds = reg.get("detroit_dhd_covid_archive_daily")
    place = {"match": "fixed", "fixed": {"name": "City of Detroit", "key": "US-26163/detroit"}}
    schema = {**ds.contract.raw["schema"][0], "customProperties": [{"property": "conformedDimensions",
                                                                     "value": {"place": place}}]}  # fmt: skip
    declared = replace(ds, contract=replace(ds.contract, raw={**ds.contract.raw, "schema": [schema]}))
    cov = coverage.declared_coverage(declared)
    assert cov.place_keys == {"US-26163/detroit"} and cov.place_words == "Detroit"
    assert cov.holds("US-26") is False


def test_declared_coverage_from_the_catalog(reg: Registry) -> None:
    nndss = coverage.declared_coverage(reg.get("cdc_nndss_weekly"))
    assert nndss.place_keys == {"USA"} and nndss.nationwide and nndss.holds("US-36")
    assert nndss.holds("US-36/nyc") is None  # which cities it reports apart from their states isn't declared
    assert nndss.place_words == "the United States"
    assert {"legionella", "candida", "syphilis"} <= nndss.condition_words  # GBD crosswalk pathogens count too
    assert nndss.holds_years(2020, 2023) and nndss.holds_years(2030, None) and nndss.holds_years(2022, 2022)
    assert not nndss.holds_years(None, 2021) and not nndss.holds_years(2010, 2021)
    cdi = coverage.declared_coverage(reg.get("cdc_haicviz_cdi"))
    assert cdi.holds_years(2022, None) and not cdi.holds_years(2023, None)
    assert coverage.words("a TB e-x") == {"tb", "e-x"}  # single letters dropped
    who = coverage.declared_coverage(reg.get("who_gho_tb_incidence"))
    assert who.everywhere and who.holds("NGA") and who.place_words == "every country"
    assert coverage.declared_coverage(reg.get("census_county_pop")).condition_words == frozenset()
    assert coverage.declared_coverage(reg.get("idweek_abstracts")).condition_words == frozenset()
    assert {"q fever", "c auris", "influenza", "associated"} & coverage.words(
        "Q fever; C. auris; influenza-associated"
    ) == {"q fever", "c auris", "influenza", "associated"}


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Rubeola in NYC", [("New York City", "US-36/nyc")]),
        ("measles in New York City and New York", [("New York City", "US-36/nyc"), ("New York", "US-36")]),
        ("TB in West Virginia", [("West Virginia", "US-54")]),
        ("flu in Washington, DC", [("District of Columbia", "US-11")]),
        ("deaths in the U.S. and Detroit", [("Detroit", "US-26163/detroit"), ("United States", "USA")]),
        ("tell us about USA flu", [("United States", "USA")]),
        ("tell us about flu", []),
    ],
)
def test_places_named(text: str, found: list[tuple[str, str]]) -> None:
    assert geo.places_named(text)[0] == found


@pytest.mark.parametrize(
    ("outer", "inner", "held"),
    [
        ("USA", "US-26163/detroit", True), ("US-26", "US-26163", True), ("US-26163", "US-26", False),
        ("US-26163/detroit", "US-26163", False), ("USA", "NGA", False), ("US-36", "US-36/nyc", True),
    ],
)  # fmt: skip
def test_within(outer: str, inner: str, held: bool) -> None:
    assert geo.within(outer, inner) is held


def test_key_for() -> None:
    assert [geo.key_for(x) for x in ("Ohio", "OH", "United States", "nyc", "Detroit", "Travis County, Texas")] == [
        "US-39", "US-39", "USA", "US-36/nyc", "US-26163/detroit", "US-48"
    ]  # fmt: skip
    assert geo.key_for("Lagos") is None


# --------------------------------------------------------------------------------------------- live sources


class Getter:
    """Counts publisher reads; every read fails, as an unreachable host would."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str, *a: Any, **kw: Any) -> Any:
        self.calls.append(url)
        raise ConnectionResetError("the host closed the connection")


def live_engine(reg: Registry, tmp_path: Path) -> tuple[Engine, Getter]:
    get = Getter()
    eng = Engine(registry=reg, lake=tmp_path / "lake", hub=None, fetch_at_run_time=True, getter=get)
    return eng, get


def test_a_live_source_is_not_read_for_a_place_it_does_not_cover(reg: Registry, tmp_path: Path) -> None:
    eng, get = live_engine(reg, tmp_path)
    out = _json(tools.get_series(reg, eng, condition="Tuberculosis", locations=["California"],
                                 source_id="detroit_dhd_epi_annual", fmt="json"))  # fmt: skip
    assert get.calls == []  # never read: its declared coverage doesn't hold California
    assert out["refused"] == "place_not_covered"
    assert out["message"] == (
        "No source that reports 'Tuberculosis' covers California, so no numbers are shown. detroit_dhd_epi_annual "
        "was not read for California: it is read live from City of Detroit Health Department (DHD), and which "
        "places it covers isn't declared yet. Ask again without places to read it."
    )
    assert out["next_step"].startswith("Ask suggest_datasets which sources cover that place")


def test_a_live_source_that_fails_gives_one_refusal(reg: Registry, tmp_path: Path) -> None:
    eng, get = live_engine(reg, tmp_path)
    out = _json(tools.get_series(reg, eng, condition="Tuberculosis", source_id="detroit_dhd_epi_annual",
                                 fmt="json"))  # fmt: skip
    mentions = [n for n in out["notes"] if n.startswith("detroit_dhd_epi_annual: ")]
    assert len(mentions) == 1 and "could not be reached" in mentions[0]  # one read, one refusal
    assert len(get.calls) == 1 and out["rows"] == []


def test_a_detroit_read_with_no_suppressed_count_loads(reg: Registry) -> None:
    """The dashboard writes 'SUM(Cases) flag' only on a suppressed mark, so a clean read carries none of it."""
    ds = reg.get("detroit_dhd_epi_annual")
    rows = [
        {"view": "TB", "worksheet": "TByear", "Location (alias)": place, "YEAR(Year)": "2024",
         "SUM(Cases) (alias)": cases, "SUM(Rate)": rate}
        for place, cases, rate in (("Detroit Case Rate", "44", "6.8"), ("Michigan Case Rate", "150", "1.5"))
    ]  # fmt: skip
    report = load_live(duckdb.connect(":memory:"), ds, rows).staging
    assert report.row_count == 2


# --------------------------------------------------------------------------------------------- source_id


def test_get_series_honours_source_id(reg: Registry, tmp_path: Path) -> None:
    eng = Engine(registry=reg, lake=tmp_path / "lake", hub=None, fetch_at_run_time=False)
    abstracts = _json(
        tools.get_series(reg, eng, condition="Candida auris", source_id="idweek_abstracts", fmt="json")
    )
    assert abstracts["refused"] == "source_has_no_series"
    assert abstracts["message"].startswith("IDWeek abstracts (Open Forum Infectious Diseases supplement) "
                                           "(idweek_abstracts) is a list of conference abstracts")  # fmt: skip
    assert abstracts["next_step"].startswith("A tool for searching the abstracts is planned. For now, browse")
    missing = _json(tools.get_series(reg, eng, condition="Candida auris", source_id="nope", fmt="json"))
    assert missing["refused"] == "unknown_dataset" and "There's no source called 'nope'" in missing["message"]
    census = _json(
        tools.get_series(reg, eng, condition="Candida auris", source_id="census_county_pop", fmt="json")
    )
    assert census["refused"] == "unknown_condition"
    assert census["message"] == "census_county_pop doesn't report 'Candida auris'."
    assert not census.get("nearest_served")  # never another source's labels


def test_query_sql_on_a_private_source_offers_no_other_tool(reg: Registry, tmp_path: Path) -> None:
    eng = Engine(registry=reg, lake=tmp_path / "lake", hub=None, fetch_at_run_time=False)
    out = _json(tools.query_sql(eng, "SELECT doi FROM idweek_abstracts", fmt="json"))
    assert "No other tool can read idweek_abstracts yet" in out["message"]
    assert "other tools (get_series" not in out["message"]
    assert out["next_step"].startswith("For now, browse the abstracts on https://")


def test_detroit_contract_reads_the_flag_only_when_shown(reg: Registry) -> None:
    col = reg.get("detroit_dhd_epi_annual").contract.column("cases_flag")
    assert col.may_be_empty


def test_an_mdhhs_read_with_no_suppressed_county_loads(reg: Registry) -> None:
    """The Case Map writes its count flags only on a suppressed county, so a clean read carries neither."""
    ds = reg.get("mdhhs_measles_county")
    rows = [
        {"view": "CaseMap", "worksheet": "Case By Cty Map", "County (alias)": county,
         "c_Case Indicator (alias)": "Case", "SUM(c_Total Cases Reported) (alias)": total,
         "SUM(c_Outbreak Cases) (alias)": outbreak}
        for county, total, outbreak in (("Wayne", "3", "2"), ("Kent", "1", "0"))
    ]  # fmt: skip
    assert load_live(duckdb.connect(":memory:"), ds, rows).staging.row_count == 2


def test_a_live_state_source_is_not_read_for_a_city_it_does_not_declare(reg: Registry) -> None:
    from dataclasses import replace

    ds = reg.get("detroit_dhd_epi_annual")
    place = {"match": "fixed", "fixed": {"name": "Michigan", "key": "US-26"}}
    schema = {**ds.contract.raw["schema"][0], "customProperties": [{"property": "conformedDimensions",
                                                                     "value": {"place": place}}]}  # fmt: skip
    state_only = replace(ds, contract=replace(ds.contract, raw={**ds.contract.raw, "schema": [schema]}))
    assert tools.not_read_live(state_only, ["Michigan"]) is None
    assert tools.not_read_live(state_only, ["Detroit"]) == (
        "detroit_dhd_epi_annual was not read for Detroit: it covers Michigan, and which cities it reports apart "
        "from their states isn't declared. Ask again for the state, or without places."
    )
    assert tools.not_read_live(state_only, ["Ohio"]) == (
        "detroit_dhd_epi_annual was not read for Ohio: it covers Michigan only."
    )
