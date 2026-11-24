"""The DOI abstract adapter (Crossref, OpenAlex, Europe PMC) and the harvest-doi command. Synthetic answers only."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from health_data_registry import cli, doi_harvest, private, research_tags
from health_data_registry.adapters import doi_abstracts as doi
from health_data_registry.fetch import FetchRefused
from health_data_registry.registry import RegistryError, Supplement, load_registry
from health_data_registry.store import RawFetch, read_fetch_log

SUP = Supplement(
    year=2025,
    issn="2328-8957",
    volume="13",
    issue="Supplement_1",
    doi_prefix="10.1093/ofid/ofaf695.",
    exclude_dois=["10.1093/ofid/ofaf695.999"],
    from_pub_date="2025-10-01",
    until_pub_date="2026-03-31",
    evidence="synthetic",
    verified=True,
)
CC_BY = "https://creativecommons.org/licenses/by/4.0/"
WHEN = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ fixtures


def cr_item(n: int, **over: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "DOI": f"10.1093/ofid/ofaf695.{n:03d}",
        "title": [f"{n}. <i>Candida auris</i> in a cohort &amp; more"],
        "author": [{"given": "Ana", "family": "Ruiz"}, {"name": "IDWeek Study Group"}, {"family": "Lee"}],
        "container-title": ["Open Forum Infectious Diseases"],
        "volume": "13",
        "issue": "Supplement_1",
        "page": "S1",
        "published": {"date-parts": [[2026, 1, 9]]},
        "license": [
            {"URL": "https://www.elsevier.com/tdm/userlicense/1.0/", "content-version": "tdm"},
            {"URL": CC_BY, "content-version": "vor"},
        ],
        "abstract": "<jats:p>Background  we studied\nMRSA.</jats:p>",
    }
    item.update(over)
    return item


def crossref_body(items: list[Any], *, cursor: str | None = "next", total: int | None = None) -> bytes:
    msg: dict[str, Any] = {"items": items, "total-results": len(items) if total is None else total}
    if cursor is not None:
        msg["next-cursor"] = cursor
    return json.dumps({"status": "ok", "message": msg}).encode()


def oa_work(n: int, **over: Any) -> dict[str, Any]:
    w: dict[str, Any] = {
        "doi": f"https://doi.org/10.1093/OFID/ofaf695.{n:03d}",
        "title": f"{n}. Candida auris in a cohort",
        "publication_date": "2026-01-09",
        "publication_year": 2026,
        "authorships": [{"raw_author_name": "Ana Ruiz"}, {"author": {"display_name": "B. Lee"}}, "junk"],
        "abstract_inverted_index": {"in": [1], "Kenya": [2], "Cohort": [0]},
        "primary_location": {"license": "cc-by", "source": {"display_name": "OFID"}},
        "biblio": {"volume": "13", "issue": "Supplement_1", "first_page": "S10", "last_page": "S11"},
    }
    w.update(over)
    return w


def epmc_result(n: int, **over: Any) -> dict[str, Any]:
    r: dict[str, Any] = {
        "doi": f"10.1093/ofid/ofaf695.{n:03d}",
        "title": f"{n}. Candida auris in a cohort.",
        "authorString": "Ruiz A, Lee B.",
        "journalInfo": {"volume": "13", "issue": "Suppl 1", "journal": {"title": "Open Forum Infect Dis"}},
        "pageInfo": "S12",
        "firstPublicationDate": "2026-01-10",
        "abstractText": "Europe PMC text.",
        "license": "cc by",
        "pmid": "41000001",
        "pmcid": "PMC9000001",
    }
    r.update(over)
    return r


def raw(url: str, body: bytes) -> RawFetch:
    return RawFetch(url=url, content=body, sha256=hashlib.sha256(body).hexdigest(), retrieved_at=WHEN)


# ------------------------------------------------------------------------------------------------ small helpers


@pytest.mark.parametrize(
    ("value", "want"),
    [
        ("10.1093/OFID/ofaf695.001", "10.1093/ofid/ofaf695.001"),
        ("https://doi.org/10.1/x", None),  # too short a registrant
        ("https://doi.org/10.1093/ofid/X", "10.1093/ofid/x"),
        ("http://doi.org/10.1093/a", "10.1093/a"),
        ("https://dx.doi.org/10.1093/a", "10.1093/a"),
        ("http://dx.doi.org/10.1093/a", "10.1093/a"),
        ("DOI:10.1093/a", "10.1093/a"),
        ("  10.1093/a  ", "10.1093/a"),
        ("10.1093/a b", None),
        ("11.1093/a", None),
        ("x10.1093/a", None),
        ("", None),
        (None, None),
        (10.1, None),
    ],
)
def test_normalize_doi(value: object, want: str | None) -> None:
    assert doi.normalize_doi(value) == want


def test_plain_removes_markup_and_space() -> None:
    assert doi.plain("<jats:p>A &amp;\n  <b>B</b></jats:p>") == "A & B"
    assert doi.plain("a<br/>b") == "a b"
    assert doi.plain(" <p> </p> ") is None
    assert doi.plain(None) is None and doi.plain(3) is None


@pytest.mark.parametrize(
    ("title", "want"),
    [
        ("568. Comparative outcomes", "568"),
        ("P-1589. Home infusion", "P-1589"),
        ("  12. x", "12"),
        ("LB-12. late breaker", "LB-12"),
        ("568.Comparative", None),
        ("568 Comparative", None),
        ("ABCD-1. x", None),
        ("123456. x", None),
        ("Comparative 568. outcomes", None),
        ("568.  ", None),
        ("", None),
        (None, None),
    ],
)
def test_abstract_number(title: str | None, want: str | None) -> None:
    assert doi.abstract_number(title) == want


@pytest.mark.parametrize(
    ("licence", "kind"),
    [
        (CC_BY, "cc-by"),
        ("http://creativecommons.org/licenses/by/3.0/igo/", "cc-by"),
        ("https://creativecommons.org/licenses/by-nc-nd/4.0/", "cc-other"),
        ("cc-by", "cc-by"),
        ("CC BY", "cc-by"),
        ("cc by-nc", "cc-other"),
        ("cc-by-nc-nd", "cc-other"),
        ("https://www.elsevier.com/tdm/userlicense/1.0/", "other"),
        ("publisher-specific-oa", "other"),
        ("  ", "other"),
        ("", "none"),
        (None, "none"),
    ],
)
def test_licence_kind(licence: str | None, kind: str) -> None:
    assert doi.licence_kind(licence) == kind


def test_abstract_tier_follows_the_licence_and_needs_text() -> None:
    assert doi.abstract_tier(CC_BY, has_text=True) == "public"
    assert doi.abstract_tier("cc-by-nc-nd", has_text=True) == "private"
    assert doi.abstract_tier("https://www.elsevier.com/tdm/userlicense/1.0/", has_text=True) == "link_only"
    assert doi.abstract_tier(None, has_text=True) == "link_only"
    assert doi.abstract_tier(CC_BY, has_text=False) == "link_only"


# ------------------------------------------------------------------------------------------------ Crossref


def test_crossref_url_filters_by_publication_date_and_pages_by_cursor() -> None:
    url = doi.crossref_url(SUP, "abc+/=", 20)
    parts = urlsplit(url)
    assert (parts.scheme, parts.hostname, parts.path) == (
        "https",
        "api.crossref.org",
        "/journals/2328-8957/works",
    )
    q = parse_qs(parts.query)
    assert q["filter"] == ["from-pub-date:2025-10-01,until-pub-date:2026-03-31"]
    assert q["rows"] == ["20"] and q["cursor"] == ["abc+/="]
    assert q["select"][0].split(",")[:2] == ["DOI", "title"] and "license" in q["select"][0]
    assert "cursor=*" in doi.crossref_url(SUP) and "rows=1000" in doi.crossref_url(SUP)
    assert "rows=1&" in doi.crossref_url(SUP, rows=1)
    for bad in (0, doi.CROSSREF_ROWS + 1):
        with pytest.raises(ValueError, match="rows must be"):
            doi.crossref_url(SUP, rows=bad)


def test_in_supplement_applies_every_structural_filter() -> None:
    assert doi.in_supplement(cr_item(1), SUP)
    assert not doi.in_supplement(cr_item(999), SUP)  # excluded by the catalog
    assert not doi.in_supplement(cr_item(1, DOI="not a doi"), SUP)
    assert not doi.in_supplement(cr_item(1, volume="12"), SUP)
    assert not doi.in_supplement(cr_item(1, issue="2"), SUP)
    assert not doi.in_supplement(cr_item(1, DOI="10.1093/ofid/ofae631.001"), SUP)
    paged = SUP.model_copy(update={"page_prefix": "S", "issue": None, "doi_prefix": None})
    assert doi.in_supplement(cr_item(1, issue="6", DOI="10.1016/j.x.1"), paged)
    assert not doi.in_supplement(cr_item(1, page="e12"), paged)
    assert not doi.in_supplement(cr_item(1, page=None), paged)
    assert doi.in_supplement(cr_item(1, DOI="10.1093/OFID/OFAF695.002"), SUP)  # DOIs compare in lower case


def test_crossref_authors_and_licence() -> None:
    assert doi.crossref_authors(cr_item(1)["author"]) == "Ana Ruiz; IDWeek Study Group; Lee"
    assert doi.crossref_authors([{"given": " "}, "x"]) is None
    assert doi.crossref_authors(None) is None
    assert doi.crossref_licence(cr_item(1)["license"]) == CC_BY
    unspecified = [{"URL": "https://pub/own", "content-version": "unspecified"}, {"URL": CC_BY}]
    assert doi.crossref_licence(unspecified) == CC_BY  # Creative Commons first among equals
    tdm = [{"URL": "https://www.elsevier.com/tdm/userlicense/1.0/", "content-version": "tdm"}]
    assert doi.crossref_licence(tdm) == "https://www.elsevier.com/tdm/userlicense/1.0/"
    am = [{"URL": CC_BY, "content-version": "am"}]
    assert doi.crossref_licence(am) is None
    assert doi.crossref_licence([{"content-version": "vor"}, "x"]) is None
    assert doi.crossref_licence(None) is None


def test_parse_crossref_keeps_only_the_supplement() -> None:
    items = [cr_item(1), cr_item(2, volume="12"), "junk", cr_item(3, abstract=None, license=None, title=[])]
    page = doi.parse_crossref(crossref_body(items, total=40), SUP)
    assert (page.total, page.items, page.next_cursor) == (40, 4, "next")
    first, third = page.records
    assert first == doi.ApiRecord(
        doi="10.1093/ofid/ofaf695.001",
        api="crossref",
        title="1. Candida auris in a cohort & more",
        authors="Ana Ruiz; IDWeek Study Group; Lee",
        journal="Open Forum Infectious Diseases",
        volume="13",
        issue="Supplement_1",
        pages="S1",
        published="2026-01-09",
        abstract="Background we studied MRSA.",
        licence=CC_BY,
    )
    assert third.title is None and third.abstract is None and third.licence is None
    # An empty page ends the cursor walk even when Crossref still sends a cursor.
    assert doi.parse_crossref(crossref_body([]), SUP).next_cursor is None
    assert doi.parse_crossref(crossref_body([cr_item(1)], cursor=None), SUP).next_cursor is None


def test_crossref_dates() -> None:
    def one(**over: Any) -> str | None:
        return doi.parse_crossref(crossref_body([cr_item(1, **over)]), SUP).records[0].published

    assert one(published={"date-parts": [[2026, 1]]}) == "2026-01"
    assert one(published={"date-parts": [[2026]]}) == "2026"
    assert one(published={"date-parts": [[2026, 1, 9, 5]]}) == "2026-01-09"
    assert one(published=None, issued={"date-parts": [[2025, 12, 1]]}) == "2025-12-01"
    assert one(published={"date-parts": [[None]]}, issued=None) is None
    assert one(published={"date-parts": [[]]}) is None


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"{}",
        json.dumps({"status": "ok", "message": {"items": []}}).encode(),
        json.dumps({"status": "failed", "message": {"items": [], "total-results": 0}}).encode(),
        json.dumps({"status": "ok", "message": {"items": {}, "total-results": 0}}).encode(),
        json.dumps({"status": "ok", "message": {"items": [], "total-results": "x"}}).encode(),
        json.dumps([]).encode(),
    ],
)
def test_parse_crossref_refuses_other_shapes(body: bytes) -> None:
    with pytest.raises(doi.ApiLayoutError, match="Crossref answered in a shape") as e:
        doi.parse_crossref(body, SUP)
    assert e.value.api == "Crossref"


# ------------------------------------------------------------------------------------------------ OpenAlex


def test_openalex_url_and_batch_limits() -> None:
    url = doi.openalex_url(["10.1000/a", "10.1000/b"])
    parts = urlsplit(url)
    assert (parts.hostname, parts.path) == ("api.openalex.org", "/works")
    q = parse_qs(parts.query)
    assert q["filter"] == ["doi:10.1000/a|10.1000/b"] and q["per_page"] == ["100"]
    assert "abstract_inverted_index" in q["select"][0]
    assert "doi:10.1000/a|10.1000/b" in url  # readable in the fetch log
    doi.openalex_url(["10.1000/a"] * doi.OPENALEX_BATCH)
    for bad in ([], ["10.1000/a"] * (doi.OPENALEX_BATCH + 1)):
        with pytest.raises(ValueError, match="OpenAlex"):
            doi.openalex_url(bad)


def test_inverted_index_back_to_text() -> None:
    assert doi.inverted_to_text({"b": [1, 3], "a": [0, 2], "x": ["y"]}) == "a b a b"
    assert doi.inverted_to_text({"a": "0"}) is None
    assert doi.inverted_to_text({}) is None and doi.inverted_to_text(None) is None


def test_parse_openalex() -> None:
    body = json.dumps(
        {"results": [oa_work(1), "junk", oa_work(2, doi=None), oa_work(3, biblio={"first_page": "S4"})]}
    ).encode()
    one, three = doi.parse_openalex(body)
    assert one == doi.ApiRecord(
        doi="10.1093/ofid/ofaf695.001",
        api="openalex",
        title="1. Candida auris in a cohort",
        authors="Ana Ruiz; B. Lee",
        journal="OFID",
        volume="13",
        issue="Supplement_1",
        pages="S10-S11",
        published="2026-01-09",
        abstract="Cohort in Kenya",
        licence="cc-by",
    )
    assert three.pages == "S4" and three.volume is None
    same = oa_work(4, biblio={"first_page": "S4", "last_page": "S4"}, publication_date=None)
    (four,) = doi.parse_openalex(json.dumps({"results": [same]}).encode())
    assert four.pages == "S4" and four.published == "2026"
    oa_only = oa_work(5, primary_location={"license": None}, best_oa_location={"license": "cc-by-nc"})
    (five,) = doi.parse_openalex(json.dumps({"results": [oa_only]}).encode())
    assert five.licence == "cc-by-nc" and five.journal is None
    bare = oa_work(6, primary_location=None, authorships=None, abstract_inverted_index=None)
    (six,) = doi.parse_openalex(json.dumps({"results": [bare]}).encode())
    assert six.licence is None and six.authors is None and six.abstract is None


@pytest.mark.parametrize("body", [b"<html>", b"{}", b'{"results": {}}', b"[]"])
def test_parse_openalex_refuses_other_shapes(body: bytes) -> None:
    with pytest.raises(doi.ApiLayoutError, match="OpenAlex"):
        doi.parse_openalex(body)


# ------------------------------------------------------------------------------------------------ Europe PMC


def test_europepmc_url_and_batch_limits() -> None:
    url = doi.europepmc_url(["10.1000/a", "10.1000/b"])
    parts = urlsplit(url)
    assert (parts.hostname, parts.path) == ("www.ebi.ac.uk", "/europepmc/webservices/rest/search")
    q = parse_qs(parts.query)
    assert q["query"] == ['DOI:"10.1000/a" OR DOI:"10.1000/b"']
    assert (q["resultType"], q["format"], q["pageSize"]) == (["core"], ["json"], ["100"])
    assert "%20OR%20" in url and "+" not in url
    doi.europepmc_url(["10.1000/a"] * doi.EUROPEPMC_BATCH)
    for bad in ([], ["10.1000/a"] * (doi.EUROPEPMC_BATCH + 1)):
        with pytest.raises(ValueError, match="Europe PMC"):
            doi.europepmc_url(bad)
    # A full batch stays under Europe PMC's 1,500-character query limit.
    full = doi.europepmc_url([f"10.1093/ofid/ofaf695.{n:04d}" for n in range(doi.EUROPEPMC_BATCH)])
    assert len(unquote(parse_qs(urlsplit(full).query)["query"][0])) < 1500


def test_parse_europepmc() -> None:
    body = {
        "resultList": {
            "result": [epmc_result(1), "x", epmc_result(2, doi=None), epmc_result(3, pubYear="2026")]
        }
    }
    one, three = doi.parse_europepmc(json.dumps(body).encode())
    assert one == doi.ApiRecord(
        doi="10.1093/ofid/ofaf695.001",
        api="europepmc",
        title="1. Candida auris in a cohort.",
        authors="Ruiz A, Lee B.",
        journal="Open Forum Infect Dis",
        volume="13",
        issue="Suppl 1",
        pages="S12",
        published="2026-01-10",
        abstract="Europe PMC text.",
        licence="cc by",
        pmid="41000001",
        pmcid="PMC9000001",
    )
    assert three.doi == "10.1093/ofid/ofaf695.003"
    late = epmc_result(4, firstPublicationDate=None, pubYear="2026", journalInfo=None)
    (four,) = doi.parse_europepmc(json.dumps({"resultList": {"result": [late]}}).encode())
    assert four.published == "2026" and four.journal is None and four.volume is None


@pytest.mark.parametrize("body", [b"x", b"{}", b'{"resultList": {}}', b'{"resultList": {"result": {}}}'])
def test_parse_europepmc_refuses_other_shapes(body: bytes) -> None:
    with pytest.raises(doi.ApiLayoutError, match="Europe PMC"):
        doi.parse_europepmc(body)


# ------------------------------------------------------------------------------------------------ merge


def test_merge_takes_each_field_from_the_first_api_that_gives_it() -> None:
    cr = doi.ApiRecord(doi="10.1000/A", api="crossref", title="T", volume="13", licence=CC_BY)
    oa = doi.ApiRecord(
        doi="10.1000/a", api="openalex", title="T2", authors="A B", abstract="oa text", licence="cc-by"
    )
    ep = doi.ApiRecord(
        doi="10.1000/a", api="europepmc", pmid="1", pmcid="PMC1", abstract="ep text", licence="x"
    )
    (a,) = doi.merge([ep, oa, cr]).values()
    assert (a.doi, a.title, a.authors, a.volume, a.pmid, a.pmcid) == (
        "10.1000/a",
        "T",
        "A B",
        "13",
        "1",
        "PMC1",
    )
    assert (a.abstract, a.abstract_api, a.licence) == (
        "oa text",
        "openalex",
        "cc-by",
    )  # text and licence together
    assert a.apis == ("crossref", "openalex", "europepmc")
    assert a.tier == "public"


def test_merge_licence_without_text_and_dropped_titles() -> None:
    cr = doi.ApiRecord(doi="10.1000/a", api="crossref", title="568. T")
    oa = doi.ApiRecord(doi="10.1000/a", api="openalex", licence="cc-by")
    ep = doi.ApiRecord(doi="10.1000/a", api="europepmc", abstract="text")  # text with no licence
    (a,) = doi.merge([cr, oa, ep]).values()
    assert (a.abstract, a.abstract_api, a.licence) == ("text", "europepmc", "cc-by")
    assert a.abstract_number == "568"
    (b,) = doi.merge([cr, oa]).values()
    assert (b.abstract, b.abstract_api, b.licence, b.tier) == (None, None, "cc-by", "link_only")
    assert doi.merge([doi.ApiRecord(doi="10.1000/b", api="openalex")]) == {}  # no title anywhere
    assert doi.merge([doi.ApiRecord(doi="nonsense", api="crossref", title="T")]) == {}


def test_merge_keeps_the_fuller_of_two_answers_from_one_api() -> None:
    thin = doi.ApiRecord(doi="10.1000/a", api="crossref", title="T")
    full = doi.ApiRecord(doi="10.1000/a", api="crossref", title="T", authors="A")
    assert doi.merge([thin, full])["10.1000/a"].authors == "A"
    assert doi.merge([full, thin])["10.1000/a"].authors == "A"
    b1 = doi.ApiRecord(doi="10.1000/a", api="crossref", title="B")
    a1 = doi.ApiRecord(doi="10.1000/a", api="crossref", title="A")
    assert doi.merge([b1, a1])["10.1000/a"].title == doi.merge([a1, b1])["10.1000/a"].title == "A"


_dois = st.sampled_from([f"10.1093/ofid/ofaf695.{n:03d}" for n in range(6)])
_text = st.one_of(st.none(), st.sampled_from(["T", "568. U", "x y"]))
_records = st.builds(
    doi.ApiRecord,
    doi=st.one_of(_dois, _dois.map(str.upper), _dois.map(lambda d: f"https://doi.org/{d}")),
    api=st.sampled_from(doi.API_ORDER),
    title=_text,
    authors=_text,
    abstract=_text,
    licence=st.one_of(st.none(), st.sampled_from([CC_BY, "cc-by-nc", "other"])),
    pmid=_text,
)


@settings(max_examples=150, deadline=None)
@given(st.lists(_records, max_size=25), st.randoms(use_true_random=False))
def test_dedupe_across_apis_gives_one_record_per_doi(records: list[doi.ApiRecord], rnd: Any) -> None:
    merged = doi.merge(records)
    titled = {doi.normalize_doi(r.doi) for r in records if r.title}
    assert set(merged) == titled  # one record per DOI with a title, whichever API and spelling it came in
    assert all(a.doi == k for k, a in merged.items())
    assert all(len(set(a.apis)) == len(a.apis) for a in merged.values())
    shuffled = list(records)
    rnd.shuffle(shuffled)
    assert doi.merge(shuffled) == merged  # arrival order doesn't matter
    assert doi.merge(records + records) == merged  # a repeated page changes nothing


# ------------------------------------------------------------------------------------------------ tags


def test_research_tags_are_fixed_word_rules() -> None:
    t = research_tags.tag_record(
        "Candida auris outbreak in a Kenya ICU: a retrospective cohort", "NHSN data, USA"
    )
    assert t == {
        "condition_tags": "candida_auris",
        "design_tags": "cohort; outbreak_investigation",
        "data_source_tags": "surveillance",
        "geography_tags": "kenya; usa",
    }
    assert research_tags.tag_record("usa is lower case here", None)["geography_tags"] == ""
    assert research_tags.tags({"a": (r"x",)}, "X") == "a"
    assert research_tags.tags({"a": (r"x",)}, "X", case_sensitive=frozenset({"a"})) == ""
    assert research_tags.GEOGRAPHY_RULES["côte_divoire"] == (r"\bCôte\ d'Ivoire\b",)


# ------------------------------------------------------------------------------------------------ harvest


class FakeApis:
    """A getter that answers like the three APIs, from synthetic records, and remembers what it was asked."""

    def __init__(self, crossref_pages: list[bytes], *, epmc_fails: bool = False) -> None:
        self.pages = list(crossref_pages)
        self.urls: list[str] = []
        self.epmc_fails = epmc_fails

    def __call__(self, url: str) -> RawFetch:
        self.urls.append(url)
        host = urlsplit(url).hostname
        if host == doi.CROSSREF_HOST:
            return raw(url, self.pages.pop(0))
        if host == doi.OPENALEX_HOST:
            asked = parse_qs(urlsplit(url).query)["filter"][0].removeprefix("doi:").split("|")
            works = [oa_work(int(d.rsplit(".", 1)[1]), doi=f"https://doi.org/{d}") for d in asked]
            works.append(oa_work(500))  # a DOI nobody asked for is ignored
            return raw(url, json.dumps({"results": works}).encode())
        if self.epmc_fails:
            raise FetchRefused("http_error", "The publisher answered with status 503.", status=503)
        return raw(url, json.dumps({"resultList": {"result": [epmc_result(1), epmc_result(501)]}}).encode())


@pytest.fixture
def registry() -> Any:
    return load_registry()


def two_pages() -> list[bytes]:
    return [
        crossref_body(
            [cr_item(1), cr_item(2, volume="12"), cr_item(3, license=None, abstract=None)], total=5
        ),
        crossref_body([cr_item(4), cr_item(1)], cursor="last", total=5),
        crossref_body([], cursor=None, total=5),
    ]


def test_harvest_reads_every_page_and_writes_one_row_per_doi(registry: Any, tmp_path: Path) -> None:
    get = FakeApis(two_pages())
    s = doi_harvest.harvest(registry, tmp_path, "idweek", year=2025, get=get)
    assert (s.source, s.year, s.listed, s.rows, s.stored_rows) == ("idweek_abstracts", 2025, 3, 3, 3)
    assert s.complete and s.crossref_total == 5 and s.skipped == []
    assert s.requests == {"crossref": 3, "openalex": 1, "europepmc": 1}
    assert s.tiers == {"public": 3}
    rows = {r["doi"]: r for r in private.read_rows(tmp_path, registry.get("idweek_abstracts")).rows}  # type: ignore[union-attr]
    r1 = rows["10.1093/ofid/ofaf695.001"]
    assert r1["conference"] == "IDWeek" and r1["year"] == 2025 and r1["granularity"] == "abstract"
    assert r1["abstract_number"] == "1" and r1["abstract_text"] == "Background we studied MRSA."
    assert r1["abstract_tier"] == "public" and r1["abstract_licence"] == CC_BY
    assert r1["page_url"] == r1["doi_url"] == "https://doi.org/10.1093/ofid/ofaf695.001"
    assert r1["poster_url"] is None and r1["category"] is None and r1["session_type"] is None
    assert r1["pubmed_pmid"] == "41000001" and r1["pmcid"] == "PMC9000001"
    assert r1["metadata_sources"] == "crossref; openalex; europepmc"
    assert r1["metadata_terms"].startswith("Crossref metadata") and "CC0" in r1["metadata_terms"]
    assert r1["condition_tags"] == "candida_auris; mrsa" and r1["design_tags"] == "cohort"
    assert r1["retrieved_at"] == "2026-11-24T12:00:00+00:00"
    # Each row points at the Crossref page that listed it, and those bytes are in the lake.
    first = get.urls[0]
    assert r1["source_url"] == first and "cursor=*" in first
    assert (tmp_path / "raw" / r1["raw_sha256"]).read_bytes() == two_pages()[0]
    assert rows["10.1093/ofid/ofaf695.004"]["source_url"] == get.urls[1]
    # A DOI with no Crossref licence or text takes OpenAlex's.
    r3 = rows["10.1093/ofid/ofaf695.003"]
    assert (r3["abstract_text"], r3["abstract_licence"]) == ("Cohort in Kenya", "cc-by")
    assert set(rows["10.1093/ofid/ofaf695.004"]) == registry.get("idweek_abstracts").contract.column_names
    assert any("doi:10.1093/ofid/ofaf695.001|10.1093/ofid/ofaf695.003|" in u for u in get.urls)
    # Running again upserts: still one row per DOI.
    again = doi_harvest.harvest(registry, tmp_path, "idweek", year=2025, get=FakeApis(two_pages()))
    assert again.stored_rows == 3
    assert "the supplement was read in full" in again.lines()


def test_harvest_sample_stops_at_max_records(registry: Any, tmp_path: Path) -> None:
    get = FakeApis(two_pages())
    s = doi_harvest.harvest(registry, tmp_path, "idweek", max_records=2, enrich=False, get=get)
    assert s.listed == s.rows == 2 and not s.complete and s.requests == {"crossref": 1}
    assert "rows=100&" in get.urls[0]  # a small page for a small sample
    assert "a sample: the supplement was not read in full" in s.lines()
    s1 = doi_harvest.harvest(
        registry, tmp_path / "b", "idweek", max_records=1, enrich=False, get=FakeApis(two_pages())
    )
    assert s1.listed == 1
    big = FakeApis(two_pages())
    doi_harvest.harvest(registry, tmp_path / "c", "idweek", max_records=60, enrich=False, get=big)
    assert "rows=1000&" in big.urls[0]


def test_harvest_notes_an_api_it_could_not_read(registry: Any, tmp_path: Path) -> None:
    s = doi_harvest.harvest(registry, tmp_path, "idweek", get=FakeApis(two_pages(), epmc_fails=True))
    assert s.rows == 3 and s.skipped == [
        "Europe PMC could not be read (The publisher answered with status 503.); its fields are missing this run"
    ]
    assert "note: Europe PMC could not be read" in "\n".join(s.lines())


def test_harvest_link_only_unverified_and_congress_supplements(registry: Any, tmp_path: Path) -> None:
    ds = registry.get("idweek_abstracts")
    sup = ds.entry.source.supplements[0]
    ds.entry.source.supplements[0] = sup.model_copy(
        update={"text_policy": "link_only", "verified": False, "granularity": "congress"}
    )
    try:
        get = FakeApis(two_pages())
        s = doi_harvest.harvest(registry, tmp_path, "idweek", max_records=50, get=get)
    finally:
        ds.entry.source.supplements[0] = sup
    assert s.listed == 1  # unverified: one record, whatever was asked
    (r,) = private.read_rows(tmp_path, ds).rows  # type: ignore[union-attr]
    assert r["abstract_text"] is None and r["abstract_tier"] == "link_only" and r["abstract_licence"] == CC_BY
    assert r["granularity"] == "congress" and r["abstract_number"] is None


def test_harvest_refusals(registry: Any, tmp_path: Path) -> None:
    with pytest.raises(doi_harvest.HarvestRefused, match="choose one of apic, icid, idweek, shea"):
        doi_harvest.harvest(registry, tmp_path, "croi", get=FakeApis([]))
    with pytest.raises(
        doi_harvest.HarvestRefused, match="no supplement catalogued for 1999; catalogued years: 2025"
    ):
        doi_harvest.harvest(registry, tmp_path, "idweek", year=1999, get=FakeApis([]))
    with pytest.raises(doi_harvest.HarvestRefused, match="at least 1"):
        doi_harvest.harvest(registry, tmp_path, "idweek", max_records=0, get=FakeApis([]))
    with pytest.raises(doi.ApiLayoutError):
        doi_harvest.harvest(registry, tmp_path, "idweek", get=FakeApis([b"{}"]))


def test_conferences_and_supplements_come_from_the_catalog(registry: Any) -> None:
    confs = doi_harvest.conferences(registry)
    assert sorted(confs) == ["apic", "icid", "idweek", "shea"]
    assert {ds.entry.tier for ds in confs.values()} == {"private"}
    ds = confs["idweek"]
    assert doi_harvest.supplement(ds, None).year == max(s.year for s in ds.entry.source.supplements)
    newer = ds.entry.source.supplements[0].model_copy(update={"year": 2030})
    older = ds.entry.source.supplements[0].model_copy(update={"year": 2001})
    ds.entry.source.supplements[:] = [newer, *ds.entry.source.supplements, older]
    try:
        assert doi_harvest.supplement(ds, None).year == 2030
        assert doi_harvest.supplement(ds, 2001).year == 2001
    finally:
        del ds.entry.source.supplements[0]
        ds.entry.source.supplements.pop()
    ds.entry.source.supplements[:], saved = [], list(ds.entry.source.supplements)
    try:
        with pytest.raises(doi_harvest.HarvestRefused, match="lists no supplements"):
            doi_harvest.supplement(ds, None)
    finally:
        ds.entry.source.supplements[:] = saved
    for ds in confs.values():
        cols = ds.contract.column_names
        assert set(doi_harvest.RESEARCH_INDEX_COLUMNS) <= cols, ds.id
        assert private.primary_key(ds) == ("doi",)
        assert set(ds.entry.source.enrich_hosts) == {doi.OPENALEX_HOST, doi.EUROPEPMC_HOST}
        assert ds.entry.source.host == doi.CROSSREF_HOST
    assert {doi.CROSSREF_HOST, doi.OPENALEX_HOST, doi.EUROPEPMC_HOST} <= registry.hosts


def test_the_catalog_checks_doi_sources(tmp_path: Path) -> None:
    import shutil

    import yaml

    root = Path(load_registry().root)
    for d in ("catalog", "contracts", "semantic"):
        (tmp_path / d).mkdir()
    for name in (
        "catalog/idweek_abstracts.yaml",
        "contracts/idweek_abstracts.odcs.yaml",
        "semantic/idweek_abstracts.yaml",
    ):
        shutil.copy(root / name, tmp_path / name)
    cat_path = tmp_path / "catalog" / "idweek_abstracts.yaml"
    good = yaml.safe_load(cat_path.read_text())
    assert load_registry(tmp_path).get("idweek_abstracts").entry.source.conference == "idweek"

    def broken(change: Any) -> None:
        bad = json.loads(json.dumps(good))
        change(bad)
        cat_path.write_text(yaml.safe_dump(bad))
        with pytest.raises((RegistryError, ValueError)):
            load_registry(tmp_path)

    broken(lambda c: c["source"].update(conference=None))
    broken(lambda c: c["source"].update(conference="IDWeek"))
    broken(lambda c: c["source"].update(conference_name=None))
    broken(lambda c: c["source"].update(supplements=[]))
    broken(lambda c: c["source"].update(supplements=c["source"]["supplements"] * 2))
    broken(lambda c: c["source"].update(enrich_hosts=["API.openalex.org"]))
    broken(lambda c: c["source"]["supplements"][0].update(issn="2328-89570"))
    broken(lambda c: c["source"]["supplements"][0].update(from_pub_date="2025-10"))
    broken(lambda c: c["source"]["supplements"][0].update(exclude_dois=["10.1093/OFID/X"]))
    broken(lambda c: c["source"]["supplements"][0].update(granularity="session"))
    broken(lambda c: c.update(hosting="public"))
    broken(lambda c: c["source"].update(portal="socrata"))


# ------------------------------------------------------------------------------------------------ CLI


def test_cli_harvest_doi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def fake(registry: Any, lake: Path, conference: str, **kw: Any) -> doi_harvest.HarvestSummary:
        seen.update(kw, conference=conference, lake=lake)
        return doi_harvest.HarvestSummary(source="shea_abstracts", year=2026, listed=2, rows=2, stored_rows=2)

    monkeypatch.setenv("HDR_LAKE", str(tmp_path))
    monkeypatch.setattr(doi_harvest, "harvest", fake)
    assert cli.main(["harvest-doi", "shea", "--year", "2026", "--max-records", "2", "--no-enrich"]) == 0
    assert seen == {"conference": "shea", "lake": tmp_path, "year": 2026, "max_records": 2, "enrich": False}
    assert "shea_abstracts 2026: 2 DOIs listed, 2 rows written" in capsys.readouterr().out
    assert cli.main(["harvest-doi", "apic"]) == 0
    assert seen["year"] is None and seen["max_records"] is None and seen["enrich"] is True

    for exc, words in (
        (doi_harvest.HarvestRefused("no such conference"), "no such conference"),
        (FetchRefused("rate_limited", "wait longer"), "wait longer"),
        (doi.ApiLayoutError("OpenAlex", "no results"), "OpenAlex answered"),
    ):

        def boom(*a: Any, _e: Exception = exc, **kw: Any) -> Any:
            raise _e

        monkeypatch.setattr(doi_harvest, "harvest", boom)
        assert cli.main(["harvest-doi", "idweek"]) == 1
        assert f"harvest stopped: {words}" in capsys.readouterr().err


def test_check_contracts_and_refresh_skip_doi_sources(registry: Any, tmp_path: Path) -> None:
    from health_data_registry import crawl
    from health_data_registry.adapters import pages, publisher_fields

    ds = registry.get("apic_abstracts")
    with pytest.raises(crawl.PrivateTier, match="harvest-doi apic --year"):
        crawl.refresh_dataset(ds, tmp_path, lambda u: raw(u, b""))
    with pytest.raises(ValueError, match="checked by the parser"):
        publisher_fields(ds.entry, lambda u: raw(u, b""))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not read through a portal adapter"):
        list(pages(ds.entry, lambda u: raw(u, b"")))  # type: ignore[arg-type]
    assert read_fetch_log(tmp_path, "apic_abstracts") == []
