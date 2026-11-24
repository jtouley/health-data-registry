"""Conference abstracts published by DOI in journal supplements: Crossref lists them, OpenAlex and Europe PMC add.

IDWeek (Open Forum Infectious Diseases), SHEA Spring (Antimicrobial Stewardship & Healthcare Epidemiology),
APIC (American Journal of Infection Control) and ICID (International Journal of Infectious Diseases) publish
their abstracts as journal supplements, one DOI per abstract. This adapter reads them from three open
bibliographic APIs, never from the publishers' sites:

- Crossref REST (api.crossref.org): ``/journals/<ISSN>/works`` filtered by publication date, paged with
  ``cursor``; each item is kept only if it passes the supplement's structural filters (volume, issue, page
  prefix, DOI prefix; see registry.Supplement). Crossref's documentation (Access and authentication, read 24 Nov
  2026): without a ``mailto`` (we send none) list queries are limited to 1 a second, one at a time, and 429
  means back off. Its metadata is free to use for any purpose; some abstracts may be under copyright.
- OpenAlex (api.openalex.org): ``/works?filter=doi:<a>|<b>...`` for the DOIs Crossref listed (at most 100 OR
  values a filter, ``per_page`` at most 100; no key: 10 requests a second). Its data is CC0.
- Europe PMC REST (www.ebi.ac.uk/europepmc/webservices/rest/search): ``DOI:"<a>" OR DOI:"<b>"`` with
  ``resultType=core`` for the abstract, licence and PubMed ids. www.ebi.ac.uk's robots.txt asks for 10 seconds
  between requests.

Each API's answer becomes ``ApiRecord`` rows; ``merge`` folds them into one ``Abstract`` per DOI (the same DOI
from two APIs is one record). Nothing is inferred: titles and author names are as the API gives them (markup
removed), and an abstract's text is kept only when its licence allows (``abstract_tier``).
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import quote, urlencode

from health_data_registry.registry import Supplement

CROSSREF_HOST = "api.crossref.org"
OPENALEX_HOST = "api.openalex.org"
EUROPEPMC_HOST = "www.ebi.ac.uk"
CROSSREF_ROWS = 1000  # the most Crossref returns a page
OPENALEX_BATCH = 50  # DOIs a request (OpenAlex allows 100 OR values; half keeps URLs short)
EUROPEPMC_BATCH = 25
# Published per-second limits, as the gap between requests (Crossref public pool for list queries: 1 a second;
# OpenAlex without a key: 10 a second). robots.txt crawl delays apply on top (crawl.Robots).
MIN_INTERVAL_S: Mapping[str, float] = {CROSSREF_HOST: 1.0, OPENALEX_HOST: 0.1}

ApiName = Literal["crossref", "openalex", "europepmc"]
API_ORDER: tuple[ApiName, ...] = ("crossref", "openalex", "europepmc")  # whose metadata wins, first to last
# What each API's terms say about its metadata (read 24 Nov 2026), recorded on every row.
METADATA_TERMS: Mapping[ApiName, str] = {
    "crossref": "Crossref metadata: free to use for any purpose (Crossref REST API documentation)",
    "openalex": "OpenAlex: CC0",
    "europepmc": "Europe PMC: metadata as indexed by Europe PMC; abstract under the article's licence",
}
Tier = Literal["public", "private", "link_only"]

_CROSSREF_FIELDS = (
    "DOI,title,author,container-title,volume,issue,page,published,issued,license,abstract,URL,type"
)
_OPENALEX_FIELDS = (
    "doi,title,publication_year,publication_date,authorships,abstract_inverted_index,primary_location,"
    "best_oa_location,biblio"
)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
# IDWeek titles start with the abstract or poster number: "568. Comparative ..." or "P-1589. Home ...".
_NUMBER = re.compile(r"^\s*((?:[A-Z]{1,3}-)?\d{1,5})\.\s+\S")


class ApiLayoutError(ValueError):
    """An API answered in a shape this adapter doesn't read; nothing from that answer is kept."""

    def __init__(self, api: str, what: str) -> None:
        super().__init__(
            f"{api} answered in a shape the harvester doesn't read ({what}), so nothing from that answer was kept."
        )
        self.api = api


# ------------------------------------------------------------------------------------------------ records


@dataclass(frozen=True)
class ApiRecord:
    """One API's view of one DOI. Empty strings are never stored: a missing value is None."""

    doi: str
    api: ApiName
    title: str | None = None
    authors: str | None = None
    journal: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    published: str | None = None  # YYYY, YYYY-MM or YYYY-MM-DD as the API gives it
    abstract: str | None = None
    licence: str | None = None  # the licence of the article (and so of its abstract), as a URL or id
    pmid: str | None = None
    pmcid: str | None = None


@dataclass(frozen=True)
class Abstract:
    """One abstract after merging every API's record for its DOI."""

    doi: str
    title: str
    authors: str | None
    journal: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    published: str | None
    abstract: str | None
    licence: str | None
    pmid: str | None
    pmcid: str | None
    apis: tuple[ApiName, ...]
    abstract_api: ApiName | None = None

    @property
    def tier(self) -> Tier:
        return abstract_tier(self.licence, has_text=self.abstract is not None)

    @property
    def abstract_number(self) -> str | None:
        return abstract_number(self.title)


def normalize_doi(value: object) -> str | None:
    """A DOI in its one comparable form (lower case, no resolver prefix), or None if it isn't one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    low = text.lower()
    for p in _DOI_PREFIXES:
        if low.startswith(p):
            text = text[len(p) :]
            break
    text = text.strip().lower()
    return text if _DOI.fullmatch(text) else None


def plain(text: object) -> str | None:
    """Markup removed (JATS or HTML tags, entities) and runs of space collapsed; None for nothing."""
    if not isinstance(text, str):
        return None
    out = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()
    return out or None


def abstract_number(title: str | None) -> str | None:
    """The abstract or poster number a title starts with ("568. ..." -> 568; "P-1589. ..." -> P-1589)."""
    if not title:
        return None
    m = _NUMBER.match(title)
    return m.group(1) if m else None


def licence_kind(licence: str | None) -> Literal["cc-by", "cc-other", "other", "none"]:
    """cc-by: Creative Commons Attribution only (any version); cc-other: another CC licence (NC, ND, SA)."""
    if not licence:
        return "none"
    low = licence.strip().lower()
    m = re.search(r"creativecommons\.org/licenses/([a-z-]+)/", low)
    code = m.group(1) if m else None
    if code is None:
        short = re.fullmatch(r"cc[ -]([a-z -]+)", low)
        code = short.group(1).replace(" ", "-") if short else None
    if code is None:
        return "other"
    return "cc-by" if code == "by" else "cc-other"


def abstract_tier(licence: str | None, *, has_text: bool) -> Tier:
    """Where an abstract's text may go: public (CC BY), private (another CC licence) or link_only.

    link_only also covers every abstract whose licence is a publisher's own or unknown, and every DOI for which
    no API gave text: only the link and metadata are kept.
    """
    if not has_text:
        return "link_only"
    kind = licence_kind(licence)
    if kind == "cc-by":
        return "public"
    if kind == "cc-other":
        return "private"
    return "link_only"


def _obj(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ------------------------------------------------------------------------------------------------ Crossref


def crossref_url(sup: Supplement, cursor: str = "*", rows: int = CROSSREF_ROWS) -> str:
    if not 1 <= rows <= CROSSREF_ROWS:
        raise ValueError(f"rows must be 1 to {CROSSREF_ROWS}")
    query = urlencode(
        {
            "filter": f"from-pub-date:{sup.from_pub_date},until-pub-date:{sup.until_pub_date}",
            "rows": str(rows),
            "cursor": cursor,
            "select": _CROSSREF_FIELDS,
        },
        safe=":,*",
    )
    return f"https://{CROSSREF_HOST}/journals/{sup.issn}/works?{query}"


@dataclass(frozen=True)
class CrossrefPage:
    records: list[ApiRecord]
    next_cursor: str | None
    total: int
    items: int  # items on the page before the supplement filters


def in_supplement(item: Mapping[str, Any], sup: Supplement) -> bool:
    """Does one Crossref item pass every structural filter the catalog declares for the supplement?"""
    doi = normalize_doi(item.get("DOI"))
    if doi is None or doi in sup.exclude_dois:
        return False
    if _clean(item.get("volume")) != sup.volume:
        return False
    if sup.issue is not None and _clean(item.get("issue")) != sup.issue:
        return False
    if sup.page_prefix is not None and not str(item.get("page") or "").startswith(sup.page_prefix):
        return False
    return not (sup.doi_prefix is not None and not doi.startswith(sup.doi_prefix.lower()))


def _date_parts(value: Any) -> str | None:
    try:
        parts = value["date-parts"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if not parts or parts[0] is None:
        return None
    return "-".join(f"{int(p):02d}" if i else f"{int(p):04d}" for i, p in enumerate(parts[:3]))


def crossref_authors(authors: Any) -> str | None:
    """Authors as Crossref lists them, in order: "Given Family" (or the group's name), joined by '; '."""
    if not isinstance(authors, list):
        return None
    names = []
    for a in authors:
        if not isinstance(a, Mapping):
            continue
        name = " ".join(p for p in (_clean(a.get("given")), _clean(a.get("family"))) if p) or _clean(
            a.get("name")
        )
        if name:
            names.append(name)
    return "; ".join(names) or None


def crossref_licence(licences: Any) -> str | None:
    """The article's licence: the version-of-record one, else an unspecified one (never a text-mining licence)."""
    if not isinstance(licences, list):
        return None
    by_version: dict[str, list[str]] = {}
    for lic in licences:
        if isinstance(lic, Mapping) and _clean(lic.get("URL")):
            by_version.setdefault(str(lic.get("content-version") or "unspecified"), []).append(
                str(lic["URL"])
            )
    for version in ("vor", "unspecified"):
        urls = sorted(by_version.get(version, []), key=lambda u: licence_kind(u) == "other")
        if urls:
            return urls[0]
    return None


def parse_crossref(content: bytes, sup: Supplement) -> CrossrefPage:
    try:
        body = json.loads(content)
        message = body["message"]
        items = message["items"]
        total = int(message["total-results"])
    except (ValueError, KeyError, TypeError) as e:
        raise ApiLayoutError("Crossref", "no message.items or total-results") from e
    if body.get("status") != "ok" or not isinstance(items, list):
        raise ApiLayoutError("Crossref", f"status {body.get('status')!r}")
    records = []
    for item in items:
        if not isinstance(item, Mapping) or not in_supplement(item, sup):
            continue
        doi = normalize_doi(item.get("DOI"))
        assert doi is not None  # noqa: S101 - in_supplement checked it
        titles = item.get("title") or []
        journals = item.get("container-title") or []
        records.append(
            ApiRecord(
                doi=doi,
                api="crossref",
                title=plain(titles[0]) if titles else None,
                authors=crossref_authors(item.get("author")),
                journal=plain(journals[0]) if journals else None,
                volume=_clean(item.get("volume")),
                issue=_clean(item.get("issue")),
                pages=_clean(item.get("page")),
                published=_date_parts(item.get("published")) or _date_parts(item.get("issued")),
                abstract=plain(item.get("abstract")),
                licence=crossref_licence(item.get("license")),
            )
        )
    cursor = message.get("next-cursor")
    return CrossrefPage(
        records=records,
        next_cursor=str(cursor) if cursor and items else None,
        total=total,
        items=len(items),
    )


# ------------------------------------------------------------------------------------------------ OpenAlex


def openalex_url(dois: Sequence[str]) -> str:
    if not 1 <= len(dois) <= OPENALEX_BATCH:
        raise ValueError(f"ask OpenAlex for 1 to {OPENALEX_BATCH} DOIs at a time")
    query = urlencode(
        {"filter": "doi:" + "|".join(dois), "per_page": "100", "select": _OPENALEX_FIELDS}, safe=":|/,"
    )
    return f"https://{OPENALEX_HOST}/works?{query}"


def inverted_to_text(index: Any) -> str | None:
    """OpenAlex's abstract_inverted_index (word -> positions) back to the text, words in position order."""
    if not isinstance(index, Mapping) or not index:
        return None
    at: dict[int, str] = {}
    for word, positions in index.items():
        if isinstance(positions, list):
            for p in positions:
                if isinstance(p, int):
                    at[p] = str(word)
    return " ".join(at[p] for p in sorted(at)) or None


def _openalex_licence(work: Mapping[str, Any]) -> str | None:
    for key in ("primary_location", "best_oa_location"):
        loc = work.get(key)
        if isinstance(loc, Mapping) and _clean(loc.get("license")):
            return _clean(loc.get("license"))
    return None


def parse_openalex(content: bytes) -> list[ApiRecord]:
    try:
        results = json.loads(content)["results"]
    except (ValueError, KeyError, TypeError) as e:
        raise ApiLayoutError("OpenAlex", "no results") from e
    if not isinstance(results, list):
        raise ApiLayoutError("OpenAlex", "results is not a list")
    out = []
    for w in results:
        if not isinstance(w, Mapping):
            continue
        doi = normalize_doi(w.get("doi"))
        if doi is None:
            continue
        loc = _obj(w.get("primary_location"))
        src = _obj(loc.get("source"))
        biblio = _obj(w.get("biblio"))
        names = [
            _clean(a.get("raw_author_name")) or _clean(_obj(a.get("author")).get("display_name"))
            for a in (w.get("authorships") or [])
            if isinstance(a, Mapping)
        ]
        first, last = _clean(biblio.get("first_page")), _clean(biblio.get("last_page"))
        out.append(
            ApiRecord(
                doi=doi,
                api="openalex",
                title=plain(w.get("title")),
                authors="; ".join(n for n in names if n) or None,
                journal=_clean(src.get("display_name")),
                volume=_clean(biblio.get("volume")),
                issue=_clean(biblio.get("issue")),
                pages=(f"{first}-{last}" if first and last and first != last else first),
                published=_clean(w.get("publication_date")) or _clean(w.get("publication_year")),
                abstract=inverted_to_text(w.get("abstract_inverted_index")),
                licence=_openalex_licence(w),
            )
        )
    return out


# ------------------------------------------------------------------------------------------------ Europe PMC


def europepmc_url(dois: Sequence[str]) -> str:
    if not 1 <= len(dois) <= EUROPEPMC_BATCH:
        raise ValueError(f"ask Europe PMC for 1 to {EUROPEPMC_BATCH} DOIs at a time")
    query = " OR ".join(f'DOI:"{d}"' for d in dois)
    params = urlencode(
        {"query": query, "resultType": "core", "format": "json", "pageSize": "100"}, quote_via=quote
    )
    return f"https://{EUROPEPMC_HOST}/europepmc/webservices/rest/search?{params}"


def parse_europepmc(content: bytes) -> list[ApiRecord]:
    try:
        results = json.loads(content)["resultList"]["result"]
    except (ValueError, KeyError, TypeError) as e:
        raise ApiLayoutError("Europe PMC", "no resultList.result") from e
    if not isinstance(results, list):
        raise ApiLayoutError("Europe PMC", "result is not a list")
    out = []
    for r in results:
        if not isinstance(r, Mapping):
            continue
        doi = normalize_doi(r.get("doi"))
        if doi is None:
            continue  # Europe PMC sometimes omits the DOI on a match; without it the record can't be joined
        info = _obj(r.get("journalInfo"))
        journal = _obj(info.get("journal"))
        out.append(
            ApiRecord(
                doi=doi,
                api="europepmc",
                title=plain(r.get("title")),
                authors=_clean(r.get("authorString")),
                journal=_clean(journal.get("title")),
                volume=_clean(info.get("volume")),
                issue=_clean(info.get("issue")),
                pages=_clean(r.get("pageInfo")),
                published=_clean(r.get("firstPublicationDate")) or _clean(r.get("pubYear")),
                abstract=plain(r.get("abstractText")),
                licence=_clean(r.get("license")),
                pmid=_clean(r.get("pmid")),
                pmcid=_clean(r.get("pmcid")),
            )
        )
    return out


# ------------------------------------------------------------------------------------------------ merge


_FIELDS = ("title", "authors", "journal", "volume", "issue", "pages", "published", "pmid", "pmcid")


@dataclass
class _Acc:
    by_api: dict[ApiName, ApiRecord] = field(default_factory=dict)


def merge(records: Iterable[ApiRecord]) -> dict[str, Abstract]:
    """One Abstract per DOI. Each field comes from the first API in API_ORDER that gives it; the abstract text
    and its licence come together from the first API that gives text (else the licence alone, first given).

    The result doesn't depend on the order records arrive in, and merging a DOI's records again changes nothing.
    A record whose title is missing from every API is dropped (there is nothing to show).
    """
    acc: dict[str, _Acc] = {}
    for r in records:
        doi = normalize_doi(r.doi)
        if doi is None:
            continue
        slot = acc.setdefault(doi, _Acc())
        prev = slot.by_api.get(r.api)
        # Two answers from one API for one DOI (a repeated page): keep the fuller one, deterministically.
        if (
            prev is None
            or _fullness(r) > _fullness(prev)
            or (_fullness(r) == _fullness(prev) and repr(r) < repr(prev))
        ):
            slot.by_api[r.api] = replace(r, doi=doi)
    out: dict[str, Abstract] = {}
    for doi in sorted(acc):
        recs = [acc[doi].by_api[a] for a in API_ORDER if a in acc[doi].by_api]
        values: dict[str, str | None] = {
            f: next((getattr(r, f) for r in recs if getattr(r, f)), None) for f in _FIELDS
        }
        title = values.pop("title")
        if title is None:
            continue
        texted = next((r for r in recs if r.abstract), None)
        licence = texted.licence if texted is not None and texted.licence else None
        if licence is None:
            licence = next((r.licence for r in recs if r.licence), None)
        out[doi] = Abstract(
            doi=doi,
            title=title,
            abstract=texted.abstract if texted is not None else None,
            abstract_api=texted.api if texted is not None else None,
            licence=licence,
            apis=tuple(r.api for r in recs),
            **values,
        )
    return out


def _fullness(r: ApiRecord) -> int:
    return sum(1 for f in (*_FIELDS, "abstract", "licence") if getattr(r, f))
