from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from health_data_registry import tools
from health_data_registry.adapters import socrata, who_gho
from health_data_registry.query import Engine, QueryRefused, compile_metric
from health_data_registry.registry import Registry, RegistryError, load_registry
from health_data_registry.staging import typed_select as select_list
from health_data_registry.store import latest_snapshot
from tests.conftest import CATALOG, CONTRACT, SEMANTIC, raw_fetch, write_registry

# ------------------------------------------------------------------ registry: strict by design


def test_registry_loads(registry: Registry) -> None:
    ds = registry.get("toy_cases")
    assert ds.contract.total_rows == "reporting_area = 'US RESIDENTS'"
    assert ds.contract.column("cases").missing_codes == ("-", "N")
    assert registry.hosts == frozenset({"data.cdc.gov"})


@pytest.mark.parametrize(
    ("kind", "old", "new", "msg"),
    [
        (
            "semantic",
            "measure: rate, aggregation: none",
            "measure: rate, aggregation: sum",
            "only counts may be summed",
        ),
        ("semantic", "column: reporting_area", "column: nope", "unknown column"),
        ("catalog", "status: draft", "status: draft\nsurprise: 1", "Extra inputs"),
        ("catalog", "api_url: https://", "api_url: http://", "https"),
        (
            "contract",
            "{property: role, value: dimension}, {property: sourceField, value: label}",
            "{property: sourceField, value: label}",
            "role",
        ),
        (
            "contract",
            "      - {property: totalRows, value: \"reporting_area = 'US RESIDENTS'\"}\n",
            "",
            "totalRows",
        ),
        ("contract", "physicalType: DOUBLE", "physicalType: FLOAT8", "physicalType"),
        ("contract", "apiVersion: v3.0.2", "apiVersion: v2.2.0", "ODCS v3"),
    ],
)
def test_registry_refuses_bad_files(tmp_path: Path, kind: str, old: str, new: str, msg: str) -> None:
    texts = {"catalog": CATALOG, "contract": CONTRACT, "semantic": SEMANTIC}
    assert old in texts[kind], old
    texts[kind] = texts[kind].replace(old, new)
    root = write_registry(
        tmp_path, catalog=texts["catalog"], contract=texts["contract"], semantic=texts["semantic"]
    )
    with pytest.raises(RegistryError, match=msg):
        load_registry(root)


def test_unknown_dataset_names_the_catalogued_ones(registry: Registry) -> None:
    with pytest.raises(KeyError, match="toy_cases"):
        registry.get("nope")


# ------------------------------------------------------------------ store: contract-driven normalisation


def test_snapshot_normalises_by_contract(lake: Path, registry: Registry) -> None:
    found = latest_snapshot(lake, "toy_cases")
    assert found is not None
    path, manifest = found
    assert manifest.row_count == 6
    assert manifest.source_urls == ["https://data.cdc.gov/resource/toy0-toy0.json"]
    assert (lake / "raw" / manifest.raw_sha256[0]).is_file()
    assert (path / "data.parquet").is_file()
    eng = Engine(registry=registry, lake=lake)
    rows = eng.connect().execute("SELECT cases FROM toy_cases WHERE year = 2024").fetchall()
    assert rows == [(None,)]  # the codebook's "N" became NULL, not zero


def test_select_list_quotes_source_fields(registry: Registry) -> None:
    sql = select_list(registry.get("toy_cases").contract)
    assert '"states" END' not in sql and 'AS "reporting_area"' in sql
    assert "IN ('-', 'N')" in sql


# ------------------------------------------------------------------ metrics: sum counts, never rates


def test_counts_sum_and_exclude_total_rows(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake)
    out = eng.query_metric("toy_cases", "case_count", filters={"condition": "Legionellosis", "year": 2023})
    assert out["rows"] == [[710, 0]]  # Michigan 310 + Ohio 400; the US RESIDENTS total row (9000) is excluded
    assert "total rows excluded" in out["method"]
    assert out["source"]["retrieved_at"].startswith("2026-11-24")
    assert any("draft" in c for c in out["caveats"])


def test_group_by_and_period(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake)
    out = eng.query_metric(
        "toy_cases",
        "case_count",
        filters={"state": ["MICHIGAN"], "condition": "Legionellosis"},
        group_by=["year"],
        period={"start": 2022, "end": 2023},
    )
    assert out["columns"] == ["year", "case_count", "suppressed_rows"]
    assert out["rows"] == [[2022, 300, 0], [2023, 310, 0]]


def test_rates_are_never_added_up(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake)
    # Several published rates for one year: each on its own row, with the note (was refused; ADR 0018).
    many = eng.query_metric(
        "toy_cases", "case_rate", group_by=["year"], filters={"condition": "Legionellosis"}
    )
    years = [r[0] for r in many["rows"]]
    assert len(years) > len(set(years)) and "sum(" not in many["sql"].lower()
    assert "each is shown on its own row as published, never added up or averaged" in many["method"]
    ok = eng.query_metric(
        "toy_cases",
        "case_rate",
        group_by=["state", "year"],
        filters={"condition": "Legionellosis", "year": 2023},
    )
    assert ok["rows"] == [
        ["MICHIGAN", 2023, 3.1, False], ["OHIO", 2023, 3.4, False], ["US RESIDENTS", 2023, 2.7, False]
    ]  # fmt: skip
    assert "not summed" in ok["method"]


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"metric_name": "nope"}, "unknown_metric"),
        ({"metric_name": "case_count", "group_by": ["county"]}, "unknown_dimension"),
        ({"metric_name": "case_count", "filters": {"state": []}}, "empty_filter"),
    ],
)
def test_compile_refusals(registry: Registry, kwargs: dict, code: str) -> None:
    with pytest.raises(QueryRefused) as e:
        compile_metric(registry.get("toy_cases"), **kwargs)
    assert e.value.code == code


@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(value=st.text(min_size=1, max_size=40))
def test_filter_values_are_bound_never_spliced(registry: Registry, value: str) -> None:
    ds = registry.get("toy_cases")
    cm = compile_metric(ds, "case_count", filters={"state": value, "condition": "Legionellosis"})
    assert (
        cm.sql == compile_metric(ds, "case_count", filters={"state": "x", "condition": "Legionellosis"}).sql
    )  # SQL never depends on the value
    assert cm.params == [value, "Legionellosis"]


def test_no_snapshot_is_a_plain_refusal(registry: Registry, tmp_path: Path) -> None:
    eng = Engine(registry=registry, lake=tmp_path / "empty")
    out = tools.get_series(registry, eng, condition="legionellosis", locations=["Michigan"], measure="cases")
    assert (
        "has reading data turned off" in out and "Something's wrong helper" in out and "Traceback" not in out
    )


def test_query_sql_guarded(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake)
    out = tools.query_sql(eng, "SELECT reporting_area, cases FROM toy_cases WHERE year = 2022", fmt="json")
    assert out["rows"] == [["MICHIGAN", 300]]
    bad = tools.query_sql(eng, "SELECT * FROM read_csv('/etc/passwd')")
    assert bad.startswith("That query can't run")


# ------------------------------------------------------------------ catalog tools


def test_search_datasets(registry: Registry) -> None:
    hit = tools.search_datasets(registry, topic="legionella in states", geography="state", fmt="json")
    assert hit["rows"][0][0] == "toy_cases"
    assert "legionella" in hit["rows"][0][hit["columns"].index("why_it_fits")]
    for kw in ({"topic": "cancer"}, {"geography": "county"}, {"year_start": 2030}):
        assert tools.search_datasets(registry, fmt="json", **kw)["refused"] == "no_dataset_matched"


def test_suggest_datasets_says_why_not(registry: Registry) -> None:
    yes = tools.suggest_datasets(registry, "Is syphilis rising in Michigan since 2022?", fmt="json")
    assert yes["rows"][0][:2] == ["toy_cases", "yes"]
    no = tools.suggest_datasets(registry, "Syphilis by county in Michigan in 2010", fmt="json")
    why = no["rows"][0][2]
    assert "county" in why and "2022-2024" in why


def test_describe_dataset(registry: Registry, lake: Path) -> None:
    eng = Engine(registry=registry, lake=lake)
    d = tools.describe_dataset(registry, "toy_cases", eng, fmt="json")
    assert d["total_rows"] == "reporting_area = 'US RESIDENTS'"
    assert "rate: rate (never added up)" in d["counts_and_rates"]
    assert d["open_questions"]
    text = tools.describe_dataset(registry, "toy_cases", eng)
    assert "Legionellosis: cases; case status: confirmed and probable" in text
    assert "Use search_datasets" in tools.describe_dataset(registry, "nope")


# ------------------------------------------------------------------ series tools


def test_get_series_csv(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake)
    out = tools.get_series(
        registry, eng, condition="Legionellosis", locations=["MI", "ohio", "Atlantis"], measure="cases"
    )
    lines = out.splitlines()
    assert lines[0].startswith(
        "location,year,value,lower,upper,observation_type,period_status,flag,measure,condition,case_status"
    )
    assert lines[1].startswith(
        "Michigan,2022,300,,,as_published,the publisher marks no provisional or final status for this row,,cases,"
        "Legionellosis,confirmed and probable"
    )
    assert lines[3].startswith(
        "Michigan,2024,,,,as_published,no total: its one value is suppressed or missing in the source,,cases"
    )  # the publisher's 'N' stays empty, not 0
    assert lines[4].startswith(
        "Ohio,2023,400,,,as_published,the publisher marks no provisional or final status for this row,,cases"
    )
    assert "# toy_cases has no rows for: Atlantis." in out
    assert "never a zero" in out


def test_get_series_mixed_measures_warns(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake)
    out = tools.get_series(registry, eng, condition="legionellosis", locations=["Michigan"], year_start=2023,
                           year_end=2023, fmt="json")  # fmt: skip
    assert {r[8] for r in out["rows"]} == {"cases", "rate"}
    assert out["notes"][0].startswith("Rows mix measures")


def test_get_series_refusals(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake)
    assert tools.get_series(registry, eng, condition="mumps") == (
        "No loaded source reports 'mumps'.\nNext step: Open describe_dataset for a source search_datasets lists, "
        "pick one of its condition labels, and ask again with that label."
    )
    assert "both-sexes" in tools.get_series(registry, eng, condition="syphilis", sex="female")


def test_aligned_with_gbd(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake)
    out = tools.get_aligned_with_gbd(registry, eng, condition="legionellosis", locations=["Michigan"],
                                     year_start=2022, year_end=2025, fmt="json")  # fmt: skip
    cols = out["columns"]
    row = dict(zip(cols, out["rows"][0], strict=True))
    assert [row["gbd_location_name"], row["fips"], row["location_as_published"], row["year"]] == [
        "Michigan", "26", "Michigan", 2022
    ]  # fmt: skip
    assert [row["gbd_cause"], row["gbd_pathogen"]] == ["Lower respiratory infections", "Legionella spp"]
    call = row["suggested_gbd_call"]
    assert call.startswith("get_etiology(pathogen_name='Legionella spp'") and "year=2023" in call
    syph = tools.get_aligned_with_gbd(registry, eng, condition="syphilis", locations=["Michigan"], fmt="json")
    assert syph["rows"][0][-1].startswith("get_multi_location_trend(cause_name='Syphilis'")
    assert any("never calls GBD" in n for n in out["notes"])
    # The same dataset notes get_series carries: method, caveats, draft contract.
    assert any(n.startswith("toy_cases: Sum of cases") for n in out["notes"])
    assert "toy_cases: Counts for the latest year are provisional." in out["notes"]
    assert any("still a draft" in n for n in out["notes"])


def test_rate_provenance_caveats_prior_work(lake: Path, registry: Registry) -> None:
    eng = Engine(registry=registry, lake=lake)
    assert "aren't loaded yet" in tools.get_rate(registry, eng, condition="syphilis", year=2023)
    prov = tools.get_provenance(registry, eng, source_id="toy_cases", fmt="json")
    assert prov["rows"][0][1] == "https://data.cdc.gov/resource/toy0-toy0.json"
    assert len(prov["rows"][0][3]) == 64
    assert "CSTE 2005" in prov["notes"][0]
    cav = tools.list_caveats(registry, condition="syphilis", fmt="json")
    assert ["toy_cases", "not yet confirmed", "Is 'N' a true zero or not notifiable?"] in cav["rows"]
    assert "isn't built yet" in tools.find_prior_work(registry, condition="C. auris")
    assert "Use search_datasets" in tools.get_provenance(registry, eng, source_id="nope")


def test_list_sources(lake: Path, registry: Registry) -> None:
    out = tools.list_sources(registry, Engine(registry=registry, lake=lake), fmt="json")
    assert out["rows"][0][0] == "toy_cases" and out["rows"][0][6] == "2026-11-24"
    assert tools.list_sources(registry, condition="mumps", fmt="json")["rows"] == []


# ------------------------------------------------------------------ adapters


def test_socrata_pages_until_short_page(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socrata, "PAGE_SIZE", 2)
    data = [{"a": str(i), "loc": {"x": 1}} for i in range(5)]
    calls: list[str] = []

    def fake(url: str):  # type: ignore[no-untyped-def]
        calls.append(url)
        off = int(url.split("%24offset=")[1])
        return raw_fetch(url, data[off : off + 2])

    pages = list(socrata.pages(registry.get("toy_cases").entry, fake))
    assert [len(p.records) for p in pages] == [2, 2, 1]
    assert "%24order=%3Aid" in calls[0]
    assert pages[0].records[0]["loc"] == '{"x": 1}'
    assert len(list(socrata.pages(registry.get("toy_cases").entry, fake, max_rows=3))) == 2


def test_who_gho_follows_next_link(registry: Registry) -> None:
    entry = registry.get("toy_cases").entry
    docs = {
        entry.source.api_url: {"value": [{"Id": 1, "nested": {"a": 1}}], "@odata.nextLink": "https://x/next"},
        "https://x/next": {"value": [{"Id": 2}]},
    }
    pages = list(who_gho.pages(entry, lambda u: raw_fetch(u, docs[u])))
    assert [p.records for p in pages] == [[{"Id": 1}], [{"Id": 2}]]


def test_cli_refresh_and_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from health_data_registry import cli
    from tests.conftest import RECORDS

    root = write_registry(tmp_path / "reg")
    monkeypatch.setenv("HDR_REGISTRY", str(root))
    monkeypatch.setenv("HDR_LAKE", str(tmp_path / "lake"))
    monkeypatch.setattr(cli, "_fetcher", lambda reg, log=None: lambda url: raw_fetch(url, RECORDS))
    assert cli.main(["validate"]) == 0
    assert cli.main(["refresh", "toy_cases", "--max-rows", "10"]) == 0
    assert "6 rows" in capsys.readouterr().out
    manifest = json.loads(
        next((tmp_path / "lake" / "datasets" / "toy_cases").glob("run_date=*/*/manifest.json")).read_text()
    )
    assert manifest["partial"] is True


@pytest.mark.parametrize(
    "predicate",
    [
        "reporting_area IN (SELECT 1)",
        "secret_col = 1",
        "read_csv('/etc/passwd') IS NOT NULL",
        "t.reporting_area = 'x'",
        "reporting_area = 'x'; DROP TABLE x",
    ],
)
def test_total_rows_predicate_is_checked(tmp_path: Path, predicate: str) -> None:
    contract = CONTRACT.replace("\"reporting_area = 'US RESIDENTS'\"", json.dumps(predicate))
    with pytest.raises(RegistryError):
        load_registry(write_registry(tmp_path, contract=contract))


def test_allowed_predicate_functions(tmp_path: Path) -> None:
    contract = CONTRACT.replace(
        "\"reporting_area = 'US RESIDENTS'\"",
        json.dumps("upper(trim(reporting_area)) IN ('US RESIDENTS', 'TOTAL')"),
    )
    assert load_registry(write_registry(tmp_path, contract=contract))


def test_compound_predicates_allowed(tmp_path: Path) -> None:
    pred = "(reporting_area = 'US RESIDENTS' OR upper(reporting_area) LIKE '%TOTAL%') AND NOT (year IS NULL)"
    contract = CONTRACT.replace("\"reporting_area = 'US RESIDENTS'\"", json.dumps(pred))
    assert load_registry(write_registry(tmp_path, contract=contract))


def test_redistribution_needs_a_reviewed_licence() -> None:
    """sharp-edges 6: redistribution: true is accepted only for a licence id on the reviewed list."""
    from pydantic import ValidationError

    from health_data_registry.registry import Licence

    assert Licence(id="public-domain-us-federal", redistribution=True, terms_url="x").redistribution
    assert not Licence(id="CC-BY-NC-SA-3.0-IGO", redistribution=False, terms_url="x").redistribution
    with pytest.raises(ValidationError, match="reviewed list"):
        Licence(id="CC-BY-NC-SA-3.0-IGO", redistribution=True, terms_url="x")


def test_hosting_defaults_to_private_and_public_needs_redistribution(tmp_path: Path) -> None:
    """A source reaches the open release only through an explicit `hosting: public` in its catalog entry."""
    public = load_registry(write_registry(tmp_path / "public")).get("toy_cases").entry
    assert public.hosting == "public" and public.hosted
    unset = CATALOG.replace("hosting: public\n", "")
    private = load_registry(write_registry(tmp_path / "unset", catalog=unset)).get("toy_cases").entry
    assert private.hosting == "private" and not private.hosted
    with pytest.raises(RegistryError, match="hosting"):
        load_registry(write_registry(tmp_path / "other", catalog=unset + "hosting: maybe\n"))
    restricted = CATALOG.replace(
        "redistribution: true, terms_url", "redistribution: false, terms_url"
    ).replace("public-domain-us-federal", "CC-BY-NC-SA-3.0-IGO")
    with pytest.raises(RegistryError, match="hosting: public needs a licence"):
        load_registry(write_registry(tmp_path / "restricted", catalog=restricted))


def test_real_catalog_hosts_only_redistributable_sources() -> None:
    for ds in load_registry():
        assert ds.entry.hosted == ds.entry.licence.redistribution, ds.id
