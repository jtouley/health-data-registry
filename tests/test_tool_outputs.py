"""Exact outputs of the catalog, rate and provenance tools on the synthetic registry (they kill mutants).

Clinicians read these strings; a change to one should be a reviewed change here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from health_data_registry import tools
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import CONTRACT, RECORDS, SEMANTIC, raw_fetch, write_registry

POP_CONTRACT = CONTRACT.replace(
    "      - name: rate_per_100k\n",
    "      - name: population\n"
    "        physicalType: BIGINT\n"
    "        logicalType: integer\n"
    "        description: Census resident population.\n"
    "        customProperties: [{property: role, value: measure}, {property: sourceField, value: pop}]\n"
    "      - name: rate_per_100k\n",
)
POP_SEMANTIC = SEMANTIC.replace(
    "  - {name: rate, column: rate_per_100k,",
    "  - {name: residents, column: population, kind: count, unit: '1', description: residents}\n"
    "  - {name: rate, column: rate_per_100k,",
).replace(
    "  - {name: case_rate,",
    "  - {name: pop_total, description: Residents, measure: residents, aggregation: sum}\n  - {name: case_rate,",
) + (
    "  - {condition: population, label: Population, metric: pop_total, measure: population,"
    " filters: {condition: Legionellosis}, location_dimension: state, time_dimension: year}\n"
)
POP_RECORDS = [{**r, "pop": {"MICHIGAN": "10000000", "OHIO": "0"}.get(r["states"])} for r in RECORDS]


@pytest.fixture
def pop(tmp_path: Path) -> tuple[Registry, Engine]:
    reg = load_registry(write_registry(tmp_path / "reg", contract=POP_CONTRACT, semantic=POP_SEMANTIC))
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "p.ndjson"
    write_ndjson(f, POP_RECORDS)
    build_snapshot(
        lake, reg.get("toy_cases"), [raw_fetch("https://data.cdc.gov/resource/p.json", POP_RECORDS)], [f]
    )
    return reg, Engine(registry=reg, lake=lake)


def _json(out: Any) -> dict[str, Any]:
    assert isinstance(out, dict)
    return out


def test_get_rate_is_crude_and_says_so(pop: tuple[Registry, Engine]) -> None:
    reg, eng = pop
    out = _json(tools.get_rate(reg, eng, condition="legionellosis", locations=["Michigan", "Ohio"], year=2023,
                               fmt="json"))  # fmt: skip
    assert out["columns"] == ["location", "year", "condition", "cases", "population", "rate_per_100k", "lower_95",
                              "upper_95", "period_status", "flags", "case_source", "population_source",
                              "computed"]  # fmt: skip
    label = ("computed here: crude rate per 100,000 with an exact Poisson (Garwood) 95% interval; cases and "
             "population as published")  # fmt: skip
    assert out["rows"] == [
        ["Michigan", 2023, "Legionellosis", 310, 10000000, 3.1, 2.764, 3.465, "", "", "toy_cases", "toy_cases",
         label],
        # A zero population is no denominator: no rate, and it says why.
        ["Ohio", 2023, "Legionellosis", 400, 0, None, None, None, "",
         "the Census population for Ohio in 2023 is 0, so no rate", "toy_cases", "toy_cases", label],
    ]  # fmt: skip
    assert out["notes"][0] == (
        "Method: crude rate = cases / Census July 1 population x 100,000, with the exact Poisson (Garwood) 95% "
        "interval for the count divided by the population."
    )
    assert "not age-standardised" in out["notes"][1]
    empty = _json(
        tools.get_rate(reg, eng, condition="legionellosis", locations=["Michigan"], year=2024, fmt="json")
    )
    assert [r[:6] + r[9:12] for r in empty["rows"]] == [
        ["Michigan", 2024, "Legionellosis", None, 10000000, None,
         "1 value in this count suppressed or missing in the source, so no total and no rate (it is not zero)",
         "toy_cases", "toy_cases"]
    ]  # fmt: skip
    csv = tools.get_rate(reg, eng, condition="legionellosis", locations=["Michigan"], year=2023)
    assert isinstance(csv, str) and csv.startswith("location,year,condition,cases,population,")
    assert tools.get_rate(reg, eng, condition="mumps", year=2023, fmt="json") == {
        "message": "No loaded source reports 'mumps' as case counts.",
        "refused": "unknown_condition",
        "next_step": "Open describe_dataset for a source search_datasets lists, pick one of its condition labels, "
        "and ask again with that label.",
    }


def test_get_rate_without_population(registry: Registry, lake: Path) -> None:
    out = tools.get_rate(
        registry, Engine(registry=registry, lake=lake), condition="syphilis", year=2023, fmt="json"
    )
    assert out == {
        "message": "Population counts (Census) aren't loaded yet, so a rate can't be worked out here. Use "
        "get_series for the counts, or a published rate if the source has one (describe_dataset says).",
        "refused": "no_denominator",
        "next_step": "Use get_series for the counts, or a rate the source publishes (describe_dataset says).",
    }


def test_get_rate_refusal_is_plain(pop: tuple[Registry, Engine], tmp_path: Path) -> None:
    reg, _ = pop
    out = _json(tools.get_rate(reg, Engine(registry=reg, lake=tmp_path / "none"), condition="legionellosis",
                               year=2023, fmt="json"))  # fmt: skip
    assert out["refused"] == "no_snapshot" and "has reading data turned off" in out["message"]


def test_list_sources_exact(registry: Registry, lake: Path) -> None:
    eng = Engine(registry=registry, lake=lake)
    out = _json(tools.list_sources(registry, eng, fmt="json"))
    assert out == {
        "columns": ["source_id", "title", "publisher", "years", "geography", "updated", "last_fetched",
                    "review", "contract_version", "data_from", "source_url", "retrieved_at", "raw_sha256", "version", "licence", "citation", "contract_status"],
        "rows": [["toy_cases", "Toy notifiable condition cases by state and year",
                  "Centers for Disease Control and Prevention", "2022-2024", "nation state", "weekly", "2026-11-24",
                  "draft", "0.1.0", "the local lake (a maintainer's copy)", "https://data.cdc.gov/d/toy0-toy0",
                  "2026-11-24T12:00:00", "227232863363b7f6d297ffab7de67fbf91f8890d5c439fd8f6baa52464a41c9d",
                  "snapshot 20261124T120000Z-c1d38a70c53f; contract 0.1.0",
                  "public-domain-us-federal (https://www.cdc.gov/other/agencymaterials.html)",
                  "CDC. Toy table. https://data.cdc.gov/d/toy0-toy0", "draft"]],
        "notes": ["review = draft means no maintainer has checked this source's contract yet."],
    }  # fmt: skip
    assert _json(tools.list_sources(registry, None, fmt="json"))["rows"][0][6] == ""
    assert _json(tools.list_sources(registry, eng, condition="syphilis", fmt="json"))["rows"]
    none = _json(tools.list_sources(registry, eng, condition="mumps", fmt="json"))
    assert none["rows"] == [] and none["notes"] == [
        "No source matches; try without a condition or geography.",
        "Next step: Open describe_dataset for a source search_datasets lists, pick one of its condition labels, "
        "and ask again with that label.",
    ]
    assert _json(tools.list_sources(registry, eng, geography_level="county", fmt="json"))["rows"] == []
    assert _json(tools.list_sources(registry, eng, geography_level="state", fmt="json"))["rows"]


def test_search_datasets_exact(registry: Registry) -> None:
    out = _json(tools.search_datasets(registry, topic="legionella legionellosis in states", fmt="json"))
    assert out == {
        "columns": ["source_id", "title", "agency", "years", "geography", "why_it_fits", "source_url", "retrieved_at", "raw_sha256", "version", "licence", "citation", "contract_status"],
        "rows": [["toy_cases", "Toy notifiable condition cases by state and year", "CDC", "2022-2024",
                  "nation state", "mentions legionella, legionellosis; One row per reporting area, condition and year.",
                  "https://data.cdc.gov/d/toy0-toy0", "not fetched yet", "not fetched yet", "contract 0.1.0",
                  "public-domain-us-federal (https://www.cdc.gov/other/agencymaterials.html)",
                  "CDC. Toy table. https://data.cdc.gov/d/toy0-toy0", "draft"]],
        "notes": ["Next: describe_dataset with a source_id to see what it can and can't answer."],
    }  # fmt: skip
    plain = _json(tools.search_datasets(registry, fmt="json"))
    assert plain["rows"][0][5] == "One row per reporting area, condition and year."
    for kw in ({"id_area": "oncology"}, {"geography": "county"}, {"year_start": 2025}, {"year_end": 2021},
               {"topic": "mumps"}):  # fmt: skip
        miss = _json(tools.search_datasets(registry, fmt="json", **kw))
        assert miss["refused"] == "no_dataset_matched" and miss["next_step"] == (
            "Try a broader topic, or suggest_datasets with your whole question."
        )
    for kw in (
        {"id_area": "infectious_disease"},
        {"geography": "state"},
        {"year_start": 2024},
        {"year_end": 2022},
    ):
        assert _json(tools.search_datasets(registry, fmt="json", **kw))["rows"]


def test_suggest_datasets_exact(registry: Registry) -> None:
    out = _json(
        tools.suggest_datasets(registry, "Legionellosis in Michigan counties 2019 to 2021?", fmt="json")
    )
    assert out == {
        "columns": ["source_id", "can_answer", "why", "what_it_supports", "source_url", "retrieved_at", "raw_sha256", "version", "licence", "citation", "contract_status"],
        "rows": [["toy_cases", "no", "goes down to nation, state only, not county; covers 2022-2024 only",
                  "Legionellosis (cases); Legionellosis (rate); Syphilis (cases)", "https://data.cdc.gov/d/toy0-toy0", "not fetched yet", "not fetched yet", "contract 0.1.0",
                  "public-domain-us-federal (https://www.cdc.gov/other/agencymaterials.html)",
                  "CDC. Toy table. https://data.cdc.gov/d/toy0-toy0", "draft"]],
        "notes": [
            "Read as: condition words ['legionellosis'], places ['Michigan'], years [2019, 2021].",
            "Public data shows what and where; your hospital's own data, kept in approved systems, answers why.",
            "Never put patient information in the chat.",
            "Next step: Open describe_dataset for a source search_datasets lists, pick one of its condition labels, "
            "and ask again with that label.",
        ],
        "presentation_rules": tools.PRESENTATION_RULES,
    }  # fmt: skip
    ok = _json(tools.suggest_datasets(registry, "syphilis in Ohio 2023", fmt="json"))
    assert ok["rows"][0][:3] == ["toy_cases", "yes", "fits"]
    off = _json(tools.suggest_datasets(registry, "measles", fmt="json"))
    assert off["rows"][0][1:3] == ["no", "doesn't cover this condition"]


def test_describe_dataset_text(registry: Registry, lake: Path) -> None:
    text = str(tools.describe_dataset(registry, "toy_cases", Engine(registry=registry, lake=lake)))
    for line in [
        "Toy notifiable condition cases by state and year (toy_cases)",
        "Publisher: Centers for Disease Control and Prevention",
        "One row is: One row per reporting area, condition and year.",
        "Years: 2022-2024. Places: nation, state. Last fetched: 2026-11-24.",
        "- Legionellosis: cases; case status: confirmed and probable; case definition: CSTE 2005 (09-ID-04)",
        "- Syphilis: cases",
        "- cases: Confirmed and probable cases reported that year.",
        "Total rows: reporting_area = 'US RESIDENTS'",
        "Counts and rates: cases: count (added up); rate: rate (never added up)",
        "- Counts for the latest year are provisional.",
        "- Is 'N' a true zero or not notifiable?",
        "How to cite: CDC. Toy table. https://data.cdc.gov/d/toy0-toy0",
        "Licence: public-domain-us-federal (https://www.cdc.gov/other/agencymaterials.html)",
        "Review status: draft",
    ]:
        assert line in text.splitlines(), line
    info = _json(tools.describe_dataset(registry, "toy_cases", fmt="json"))
    assert info["columns"][3] == {"name": "cases", "means": "Confirmed and probable cases reported that year.",
                                  "unit": None, "missing_codes": ["-", "N"], "flag_codes": {},
                                  "partial_places": {}, "renamed_labels": {}}  # fmt: skip
    assert info["last_fetched"] == "" and info["running_totals"] == []
    assert info["total_rows"] == "reporting_area = 'US RESIDENTS'"


def test_provenance_and_caveats_exact(registry: Registry, lake: Path) -> None:
    eng = Engine(registry=registry, lake=lake)
    out = _json(tools.get_provenance(registry, eng, source_id="toy_cases", year=2023, fmt="json"))
    m = eng.manifest("toy_cases")
    assert out["columns"] == [
        "source_id",
        "url",
        "retrieved_at",
        "content_sha256",
        "snapshot_id",
        "contract_version",
    ]
    assert out["rows"] == [["toy_cases", "https://data.cdc.gov/resource/toy0-toy0.json", m.retrieved_at,
                            m.raw_sha256[0], m.snapshot_id, "0.1.0"]]  # fmt: skip
    assert out["notes"][:-2] == [
        "Every row for 2023 in this source comes from the fetches listed.",
        "Case definitions: Legionellosis: CSTE 2005 (09-ID-04)",
        "Cite as: CDC. Toy table. https://data.cdc.gov/d/toy0-toy0",
        "Each content hash identifies the exact bytes fetched; the same hash means the same data.",
        "Data from: the local lake (a maintainer's copy). Contract toy_cases version 0.1.0.",
    ]
    assert out["notes"][-2].startswith("Add it to a Claude project: ")
    # Like every paged answer, it ends with the source's provenance line (tools.provenance_note).
    assert out["notes"][-1] == tools.provenance_note(registry.get("toy_cases"), m)
    assert len(_json(tools.get_provenance(registry, eng, source_id="toy_cases", fmt="json"))["notes"]) == 6
    cav = _json(tools.list_caveats(registry, source_id="toy_cases", fmt="json"))
    assert cav["columns"] == ["source_id", "kind", "caveat"]
    assert cav["rows"][:5] == [
        ["toy_cases", "caveat", "Counts for the latest year are provisional."],
        ["toy_cases", "not yet confirmed", "Is 'N' a true zero or not notifiable?"],
        ["toy_cases", "unreliable values", "None stated (toy)."],
        [
            "toy_cases",
            "data checks",
            "This source's contract sets no checks of its own on these numbers. Only the general checks run: "
            "that the table has rows, and a warning when an optional column is empty in every row. Compare with "
            "the publisher's page before relying on them.",
        ],
        ["toy_cases", "review", "Contract is a draft; no maintainer has reviewed it yet."],
    ]
    assert [r[:2] for r in cav["rows"][5:]] == [["all", "geography"]] * len(tools.GEOGRAPHY_TRAPS)
    other = _json(tools.list_caveats(registry, source_id="nope", fmt="json"))
    assert all(r[0] == "all" for r in other["rows"])
    assert all(
        r[0] == "all" for r in _json(tools.list_caveats(registry, condition="mumps", fmt="json"))["rows"]
    )


def test_query_sql_tool_exact(registry: Registry, lake: Path) -> None:
    eng = Engine(registry=registry, lake=lake)
    out = _json(tools.query_sql(eng, "SELECT count(*) AS n FROM toy_cases", fmt="json"))
    assert out["columns"] == ["n"] and out["rows"] == [[6]]
    assert out["notes"][:2] == ["SQL run: " + out["notes"][0][9:], "Method: Read-only SQL."]
    assert out["notes"][0].startswith("SQL run: SELECT")
    assert out["notes"][2].startswith("Provenance for toy_cases: ")
    assert "retrieved_at 2026-11-24T12:00:00; raw file sha256 " in out["notes"][2]
    assert "contract_status: draft" in out["notes"][2] and "; cite as: " in out["notes"][2]
    bad = _json(tools.query_sql(eng, "DELETE FROM toy_cases", fmt="json"))
    assert bad["message"].startswith("That query can't run: ") and bad["message"].endswith(
        " The other tools (get_series, describe_dataset) can usually answer the same question."
    )
    assert bad["refused"] != "refused"


def test_plain_and_refusal_shapes(registry: Registry, tmp_path: Path) -> None:
    eng = Engine(registry=registry, lake=tmp_path / "none")
    try:
        eng.manifest("toy_cases")
    except Exception as e:  # noqa: BLE001 - checking the plain words of the refusal
        msg = tools.plain(e)
        assert msg.endswith(
            " Run the Something's wrong helper, which walks you through loading this source again."
        )
        assert tools.refusal(e, "json") == {
            "message": msg,
            "refused": "no_snapshot",
            "next_step": "Run the Something's wrong helper, which walks you through loading this source again.",
        }
        assert tools.refusal(e) == msg
    assert tools.refusal(ValueError("x"), "json")["refused"] == "error"  # type: ignore[index]
    assert tools.message("hi", "json", a=1) == {"message": "hi", "a": 1}
    assert tools.message("hi") == "hi"
    assert tools.find_prior_work(registry, condition="C. auris", geography="Michigan", fmt="json") == {
        "message": "The index of conference abstracts, case reports and health department reports isn't built yet, "
        "so nothing is shown rather than a partial list. For now, search PubMed (and OpenEvidence if you use it) "
        "for 'C. auris' in Michigan, and check IDWeek abstracts in the Open Forum Infectious Diseases supplements.",
        "refused": "index_not_built",
        "next_step": "Search PubMed for the condition and place in the meantime.",
    }
