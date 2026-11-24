"""The https_csv adapter and the catalog rules around it, on a tiny hand-written file (no public data)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from health_data_registry.adapters import https_csv, pages, publisher_fields
from health_data_registry.registry import CatalogEntry, SemanticModel, Source, WideToLong
from health_data_registry.store import RawFetch

URL = "https://www2.census.gov/toy/co-est-toy.csv"
FILE = (
    "SUMLEV,STATE,COUNTY,STNAME,CTYNAME,ESTIMATESBASE2020,POPESTIMATE2020,POPESTIMATE2021,BIRTHS2021\n"
    "040,35,000,New Mexico,New Mexico,100,101,102,7\n"
    "050,35,013,New Mexico,Do\xf1a Ana County,60,61,62,4\n"
    "050,35,001,New Mexico,Bernalillo County,40,40,40,3\n"
).encode("latin-1")


def entry(**source: object) -> CatalogEntry:
    base: dict[str, object] = {
        "portal": "https_csv",
        "host": "www2.census.gov",
        "dataset": "toy",
        "landing_url": "https://www.census.gov/toy.html",
        "api_url": URL,
        "encoding": "latin-1",
        "reshape": {
            "wide_to_long": {
                "keep": ["SUMLEV", "STATE", "COUNTY", "STNAME", "CTYNAME"],
                "stubs": ["POPESTIMATE"],
                "key_field": "YEAR",
            }
        },
    }
    base.update(source)
    return CatalogEntry.model_validate(
        {
            "id": "toy_pop", "title": "t", "agency": "a", "publisher": "p", "level": "federal",
            "research_areas": ["denominator"], "years": {"start": 2020}, "geography": ["state"],
            "shape": "estimate_table", "grain": "g", "refresh": "annual",
            "licence": {"id": "public-domain-us-federal", "redistribution": True, "terms_url": "https://x.gov"},
            "citation": "c", "source": base, "codebook_url": "https://x.gov", "contract": "c", "semantic_model": "s",
            "status": "draft",
        }
    )  # fmt: skip


def fetcher(content: bytes = FILE):  # type: ignore[no-untyped-def]
    calls: list[str] = []

    def get(url: str) -> RawFetch:
        calls.append(url)
        return RawFetch(url, content, hashlib.sha256(content).hexdigest(), datetime(2026, 11, 24, tzinfo=UTC))

    get.calls = calls  # type: ignore[attr-defined]
    return get


def test_pivot_is_structural_and_keeps_values_and_raw_bytes() -> None:
    get = fetcher()
    got = list(pages(entry(), get))
    assert len(got) == 1 and get.calls == [URL]  # one fetch, no paging
    assert got[0].fetch.content == FILE  # raw bytes kept unchanged
    recs = got[0].records
    assert len(recs) == 3 * 2
    assert recs[0] == {"SUMLEV": "040", "STATE": "35", "COUNTY": "000", "STNAME": "New Mexico",
                       "CTYNAME": "New Mexico", "YEAR": "2020", "POPESTIMATE": "101"}  # fmt: skip
    assert recs[3]["CTYNAME"] == "Doña Ana County" and recs[3]["YEAR"] == "2021"
    assert recs[3]["POPESTIMATE"] == "62"
    # Every published value comes through once, unchanged; nothing else is computed.
    assert sorted(r["POPESTIMATE"] for r in recs) == sorted(["101", "102", "61", "62", "40", "40"])
    assert all("ESTIMATESBASE2020" not in r and "BIRTHS2021" not in r for r in recs)


def test_fields_and_max_rows_and_no_reshape() -> None:
    recs = list(pages(entry(), fetcher(), fields=["YEAR", "POPESTIMATE"], max_rows=4))[0].records
    assert len(recs) == 4 and set(recs[0]) == {"YEAR", "POPESTIMATE"}
    plain = https_csv.records(entry(reshape=None), FILE)
    assert len(plain) == 3 and plain[1]["POPESTIMATE2021"] == "62"
    assert publisher_fields(entry(reshape=None), fetcher()) >= {"POPESTIMATE2020", "SUMLEV"}


def test_publisher_fields_report_drift() -> None:
    assert publisher_fields(entry(), fetcher()) == {"SUMLEV", "STATE", "COUNTY", "STNAME", "CTYNAME", "YEAR",
                                                     "POPESTIMATE"}  # fmt: skip
    renamed = FILE.replace(b"STNAME", b"STATENAME")
    assert "STNAME" not in publisher_fields(entry(), fetcher(renamed))
    with pytest.raises(ValueError, match="no columns named"):
        publisher_fields(entry(), fetcher(FILE.replace(b"POPESTIMATE", b"POPEST")))


@pytest.mark.parametrize(
    "content,match",
    [
        (b"", "no header"),
        (b"A,A\n1,2\n", "repeats a column"),
        (b"A,B\n1\n", "one value per column"),
        (b"A,B\n1,2,3\n", "one value per column"),
    ],
)
def test_malformed_files_are_refused(content: bytes, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        https_csv.read_rows(entry(), content)


def test_pivot_refuses_layouts_it_cannot_pivot_exactly() -> None:
    header = ["ID", "A2020", "A2021", "B2020"]
    rows = [{"ID": "1", "A2020": "5", "A2021": "6", "B2020": "7"}]
    ragged = WideToLong(keep=["ID"], stubs=["A", "B"], key_field="YEAR")
    with pytest.raises(ValueError, match="same keys"):
        https_csv.wide_to_long(ragged, header, rows)
    with pytest.raises(ValueError, match="no longer has"):
        https_csv.wide_to_long(WideToLong(keep=["GONE"], stubs=["A"], key_field="YEAR"), header, rows)
    with pytest.raises(ValueError, match="overwrite"):
        https_csv.wide_to_long(WideToLong(keep=["ID"], stubs=["A"], key_field="ID"), header, rows)
    two = https_csv.wide_to_long(
        WideToLong(keep=["ID"], stubs=["A", "B"], key_field="Y"),
        ["ID", "A2020", "B2020", "A2021", "B2021"],
        [{"ID": "1", "A2020": "5", "B2020": "7", "A2021": "6", "B2021": "8"}],
    )
    assert two == [{"ID": "1", "Y": "2020", "A": "5", "B": "7"}, {"ID": "1", "Y": "2021", "A": "6", "B": "8"}]


def test_source_rules() -> None:
    with pytest.raises(ValidationError, match="https_csv"):
        Source(portal="socrata", host="data.cdc.gov", dataset="x", landing_url="https://a", api_url="https://a",
               encoding="latin-1")  # fmt: skip
    with pytest.raises(ValidationError):
        entry(reshape={"wide_to_long": {"keep": [], "stubs": ["A"], "key_field": "Y", "key_pattern": "("}})


def test_weekly_series_rules() -> None:
    base = {
        "dataset": "d", "version": "1",
        "dimensions": [{"name": "y", "column": "y", "type": "time"}, {"name": "w", "column": "w", "type": "time"},
                       {"name": "c", "column": "c", "type": "categorical"}],
        "measures": [{"name": "m", "column": "m", "kind": "count"}],
        "metrics": [{"name": "s", "description": "d", "measure": "m", "aggregation": "sum"},
                    {"name": "n", "description": "d", "measure": "m", "aggregation": "none"}],
    }  # fmt: skip
    ok = {"condition": "x", "label": "X", "metric": "s", "measure": "cases", "time_dimension": "y"}
    SemanticModel.model_validate({**base, "series": [{**ok, "week_dimension": "w", "age_dimension": "c"}]})
    for bad in ({**ok, "week_dimension": "c"}, {**ok, "week_dimension": "w", "metric": "n"},
                {**ok, "age_dimension": "nope"}):  # fmt: skip
        with pytest.raises(ValidationError):
            SemanticModel.model_validate({**base, "series": [bad]})


def test_the_layouts_year_columns_are_required_exactly() -> None:
    """Outside review: a renamed, missing or extra POPESTIMATE<year> column fails loudly, never pivots."""
    from health_data_registry.adapters import pages

    def with_years(years: list[str]) -> CatalogEntry:
        e = entry()
        assert e.source.reshape is not None
        wtl = e.source.reshape.wide_to_long.model_copy(update={"expected_keys": years})
        reshape = e.source.reshape.model_copy(update={"wide_to_long": wtl})
        return e.model_copy(update={"source": e.source.model_copy(update={"reshape": reshape})})

    assert list(pages(with_years(["2020", "2021"]), fetcher(), fields=["YEAR", "POPESTIMATE"]))
    for years in (["2020", "2021", "2022"], ["2020"]):
        with pytest.raises(ValueError, match="layout lists exactly"):
            list(pages(with_years(years), fetcher(), fields=["YEAR", "POPESTIMATE"]))
    renamed = FILE.replace(b"POPESTIMATE2021", b"POPESTIMATE2O21")
    with pytest.raises(ValueError, match="layout lists exactly"):
        list(pages(with_years(["2020", "2021"]), fetcher(renamed), fields=["YEAR", "POPESTIMATE"]))
