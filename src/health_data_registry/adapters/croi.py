"""CROI abstracts (croiconference.org): discovery from the search listing and parsing of one abstract page.

The site is server-rendered WordPress. What this adapter reads, as seen on 24 Nov 2026:

- Listing: ``/search-abstracts/[page/N/]?aa=&at=&an=&sn=&kw=&yr=<year>&st=<type>&q=1``, 10 results per page.
  ``p.abstract-search-results-count`` says "1277 Results"; each result is ``div.post-entry-content`` holding
  ``h3.entry-title > a[href=/abstract/<internal id>-<year>/]``, whose title is "<abstract number> – <title>".
  The year and session-type filters are ``<select name="yr">`` and ``<select name="st">`` in the search form.
- Abstract page: ``h1.entry-title`` ("<number> – <title>"); ``article.abstract`` whose classes carry
  ``abstract-session-type-<slug>`` and ``abstract-category-<slug>``; the body in ``div.entry-content``; a sidebar
  of ``div.abstract-sidebar-content`` blocks, each a heading ("Abstract Number", "Session Title", "Authors") and
  its text; and, when there is one, an ``a`` titled "Download Poster" linking the poster PDF.

The internal page id (``2112`` in ``/abstract/2112-2025/``) is not the abstract number: records are keyed on
the abstract number from the page and the year. When the structure is not what is described here the parser
raises LayoutError and never guesses. Poster PDFs and figures are never fetched; only the poster's link is kept.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

HOST = "www.croiconference.org"
BASE = f"https://{HOST}"
FIRST_YEAR, LAST_YEAR = 2014, 2027
PER_PAGE = 10
COPYRIGHT = (
    "Abstract text © CROI Foundation; personal, non-commercial use only. Read it in full on CROI's site."
)

# /abstract/<internal id>-<year>[-<suffix>]/, e.g. /abstract/2112-2025/ or /abstract/1739-2025-td/
_ABSTRACT_PATH = re.compile(r"^/abstract/([a-z0-9][a-z0-9-]*)-(\d{4})(-[a-z]+)?/$")
_NUMBER_TITLE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9.-]*)\s+[–—-]\s+(.+?)\s*$", re.S)
_COUNT = re.compile(r"^\s*([\d,]+)\s+Results?\s*$", re.I)
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
_BLOCK = frozenset({"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"})


class LayoutError(ValueError):
    """CROI's page no longer looks the way this adapter reads it; nothing from it is kept."""

    def __init__(self, url: str, what: str) -> None:
        super().__init__(
            f"CROI's page {url} has changed layout ({what}), so nothing from it was kept. The CROI adapter needs "
            "updating before the crawl can go on."
        )
        self.url = url
        self.what = what


# ------------------------------------------------------------------------------------------- tiny DOM


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str]

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def iter(self) -> list[Node]:
        out: list[Node] = []
        stack: list[Node] = [self]
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(reversed([c for c in n.children if isinstance(c, Node)]))
        return out

    def find_all(self, tag: str | None = None, cls: str | None = None, **attrs: str) -> list[Node]:
        return [
            n
            for n in self.iter()[1:]
            if (tag is None or n.tag == tag)
            and (cls is None or cls in n.classes)
            and all(n.attrs.get(k) == v for k, v in attrs.items())
        ]

    def text(self, *, blocks: bool = False) -> str:
        parts: list[str] = []

        def walk(n: Node) -> None:
            if n.tag in ("script", "style"):
                return
            if blocks and n.tag in _BLOCK:
                parts.append("\n")
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                else:
                    walk(c)
            if blocks and n.tag in _BLOCK:
                parts.append("\n")

        walk(self)
        raw = "".join(parts).replace("\xa0", " ")
        if not blocks:
            return re.sub(r"\s+", " ", raw).strip()
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n")]
        return "\n".join(line for line in lines if line)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, [])
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs}, [])
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(Node(tag, {k: v or "" for k, v in attrs}, []))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return  # an end tag with no open element is ignored, as browsers do

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse_html(html: str | bytes) -> Node:
    b = _Builder()
    b.feed(html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html)
    b.close()
    return b.root


# ------------------------------------------------------------------------------------------- URLs


def check_year(year: int) -> int:
    if not FIRST_YEAR <= year <= LAST_YEAR:
        raise ValueError(
            f"CROI abstracts are searchable for {FIRST_YEAR}-{LAST_YEAR}; {year} is outside that"
        )
    return year


def listing_url(year: int, page: int = 1, session_type: str = "") -> str:
    check_year(year)
    if page < 1:
        raise ValueError("pages start at 1")
    query = urlencode(
        {"aa": "", "at": "", "an": "", "sn": "", "kw": "", "yr": str(year), "st": session_type, "q": "1"}
    )
    path = "/search-abstracts/" + (f"page/{page}/" if page > 1 else "")
    return f"{BASE}{path}?{query}"


def page_year(url: str) -> int | None:
    """The year in an abstract page's link (/abstract/<id>-<year>/), or None if it is not one."""
    parts = urlsplit(url)
    m = _ABSTRACT_PATH.match(parts.path)
    if parts.scheme != "https" or parts.hostname != HOST or m is None:
        return None
    return int(m.group(2))


# ------------------------------------------------------------------------------------------- listing


@dataclass(frozen=True)
class ListingEntry:
    abstract_number: str
    title: str
    page_url: str


@dataclass(frozen=True)
class Listing:
    url: str
    total: int
    entries: list[ListingEntry]
    years: list[int]
    session_types: dict[str, str]  # st value -> label shown on the form

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / PER_PAGE))


def _options(root: Node, name: str, url: str) -> dict[str, str]:
    selects = root.find_all("select", name=name)
    if not selects:
        raise LayoutError(url, f"no '{name}' filter in the search form")
    return {o.attrs.get("value", ""): o.text() for o in selects[0].find_all("option") if o.attrs.get("value")}


def split_number_title(text: str, url: str) -> tuple[str, str]:
    m = _NUMBER_TITLE.match(text)
    if not m:
        raise LayoutError(url, f"a heading that isn't '<number> – <title>': {text[:60]!r}")
    return m.group(1), re.sub(r"\s+", " ", m.group(2))


def parse_listing(html: str | bytes, url: str, *, year: int) -> Listing:
    root = parse_html(html)
    counts = root.find_all("p", cls="abstract-search-results-count")
    if len(counts) != 1:
        raise LayoutError(url, "no results count")
    m = _COUNT.match(counts[0].text())
    if not m:
        raise LayoutError(url, f"a results count that isn't 'N Results': {counts[0].text()[:40]!r}")
    total = int(m.group(1).replace(",", ""))
    years = sorted(int(v) for v in _options(root, "yr", url) if v.isdigit())
    session_types = _options(root, "st", url)
    containers = root.find_all("div", id="abstract-search-results-list")
    if len(containers) != 1:
        raise LayoutError(url, "no results list")
    entries: list[ListingEntry] = []
    for item in containers[0].find_all("div", cls="post-entry-content"):
        links = [a for h in item.find_all("h3", cls="entry-title") for a in h.find_all("a")]
        if len(links) != 1:
            raise LayoutError(url, "a result without exactly one title link")
        href = links[0].attrs.get("href", "")
        if page_year(href) != year:
            raise LayoutError(url, f"a result link that isn't a {year} abstract page: {href[:80]!r}")
        number, title = split_number_title(links[0].attrs.get("title") or links[0].text(), url)
        entries.append(ListingEntry(number, title, href))
    if total and not entries:
        raise LayoutError(url, f"{total} results counted but none listed")
    if len(entries) > PER_PAGE:
        raise LayoutError(url, f"{len(entries)} results on one page, more than {PER_PAGE}")
    return Listing(url, total, entries, years, session_types)


# ------------------------------------------------------------------------------------------- abstract page


@dataclass(frozen=True)
class AbstractPage:
    abstract_number: str
    year: int
    title: str
    authors: str | None
    session_type: str | None
    session_title: str | None
    category: str | None
    page_url: str
    poster_url: str | None
    text: str  # the abstract body: kept only in the private text store, never in the index


def _class_value(classes: set[str], prefix: str) -> str | None:
    found = sorted(c.removeprefix(prefix) for c in classes if c.startswith(prefix))
    return found[0].replace("-", " ") if found else None


def parse_abstract(html: str | bytes, url: str) -> AbstractPage:
    year = page_year(url)
    if year is None:
        raise LayoutError(url, "not a CROI abstract page link")
    root = parse_html(html)
    h1 = root.find_all("h1", cls="entry-title")
    if len(h1) != 1:
        raise LayoutError(url, "no single page heading")
    heading_number, title = split_number_title(h1[0].text(), url)
    articles = root.find_all("article", cls="abstract")
    if len(articles) != 1:
        raise LayoutError(url, "no single abstract article")
    body = articles[0].find_all("div", cls="entry-content")
    if len(body) != 1:
        raise LayoutError(url, "no abstract body")
    sidebar: dict[str, str] = {}
    for block in root.find_all("div", cls="abstract-sidebar-content"):
        head = block.find_all("h4")
        value = block.find_all("div", cls="abstract-sidebar-text")
        if len(head) == 1 and len(value) == 1:
            sidebar[head[0].text().lower()] = value[0].text()
    number = sidebar.get("abstract number")
    if not number:
        raise LayoutError(url, "no 'Abstract Number' in the sidebar")
    if number != heading_number:
        raise LayoutError(url, f"the heading's number {heading_number} differs from the sidebar's {number}")
    posters = [
        a.attrs["href"]
        for a in root.find_all("a")
        if a.attrs.get("title") == "Download Poster" and a.attrs.get("href", "").startswith(f"{BASE}/")
    ]
    classes = articles[0].classes
    return AbstractPage(
        abstract_number=number,
        year=year,
        title=title,
        authors=sidebar.get("authors") or None,
        session_type=_class_value(classes, "abstract-session-type-"),
        session_title=sidebar.get("session title") or None,
        category=_class_value(classes, "abstract-category-"),
        page_url=url,
        poster_url=posters[0] if posters else None,
        text=body[0].text(blocks=True),
    )


# ------------------------------------------------------------------------------------------- tags by rules

# Our own tags, from fixed word rules over the title and text: never a model's reading (ADR 0006).
CONDITION_RULES: dict[str, tuple[str, ...]] = {
    "hiv": (r"\bhiv\b", r"\bpwh\b", r"\bplwh\b", r"antiretroviral", r"\bart\b"),
    "hiv_prevention": (r"\bprep\b", r"pre-exposure prophylaxis", r"\bpep\b", r"lenacapavir", r"cab-la"),
    "hepatitis_b": (r"hepatitis b", r"\bhbv\b"),
    "hepatitis_c": (r"hepatitis c", r"\bhcv\b"),
    "tuberculosis": (r"tuberculosis", r"\btb\b", r"mycobacterium"),
    "covid19": (r"covid", r"sars-cov-2"),
    "mpox": (r"\bmpox\b", r"monkeypox"),
    "syphilis": (r"syphilis",),
    "stis": (r"gonorr", r"chlamydia", r"sexually transmitted", r"\bsti\b"),
    "cryptococcal": (r"cryptococ",),
    "hpv": (r"\bhpv\b", r"papillomavirus", r"anal cancer", r"cervical cancer"),
    "cmv": (r"\bcmv\b", r"cytomegalovirus"),
    "influenza": (r"influenza",),
}
DESIGN_RULES: dict[str, tuple[str, ...]] = {
    "randomized_trial": (r"randomi[sz]ed", r"\brct\b", r"placebo"),
    "cohort": (r"\bcohort\b", r"prospective", r"retrospective"),
    "cross_sectional": (r"cross-sectional",),
    "case_control": (r"case-control",),
    "modelling": (r"\bmodel(l)?ing\b", r"mathematical model", r"cost-effective"),
    "qualitative": (r"qualitative", r"in-depth interview", r"focus group"),
    "systematic_review": (r"systematic review", r"meta-analys"),
    "phylogenetic": (r"phylogen",),
}
DATA_SOURCE_RULES: dict[str, tuple[str, ...]] = {
    "surveillance": (r"surveillance", r"\bnndss\b", r"notifiable"),
    "ehr": (r"electronic health record", r"\behr\b", r"medical records"),
    "claims": (r"\bclaims\b", r"medicaid", r"medicare", r"marketscan"),
    "registry": (r"\bregistry\b",),
    "national_survey": (r"\bnhanes\b", r"\bphia\b", r"demographic and health survey", r"\bdhs\b"),
    "cohort_consortium": (
        r"na-accord",
        r"\bcnics\b",
        r"\bieDEA\b",
        r"\bmacs\b",
        r"\bwihs\b",
        r"\breprieve\b",
    ),
}
_PLACES = (
    "United States", "USA", "Canada", "Mexico", "Brazil", "Peru", "Argentina", "Colombia", "Haiti",
    "United Kingdom", "UK", "France", "Spain", "Italy", "Germany", "Netherlands", "Switzerland", "Denmark",
    "Russia", "Ukraine", "China", "India", "Thailand", "Vietnam", "Cambodia", "Indonesia", "Japan", "Australia",
    "South Africa", "Kenya", "Uganda", "Tanzania", "Zambia", "Zimbabwe", "Malawi", "Mozambique", "Botswana",
    "Eswatini", "Lesotho", "Nigeria", "Ghana", "Cameroon", "Rwanda", "Ethiopia", "Côte d'Ivoire",
    "sub-Saharan Africa", "Europe", "Latin America", "Asia",
)  # fmt: skip
GEOGRAPHY_RULES: dict[str, tuple[str, ...]] = {
    p.lower().replace(" ", "_").replace("'", ""): (rf"\b{re.escape(p)}\b",) for p in _PLACES
}
_CASE_SENSITIVE_PLACES = {"usa", "uk"}


def tags(
    rules: dict[str, tuple[str, ...]], text: str, *, case_sensitive: frozenset[str] = frozenset()
) -> str:
    """Every tag whose rule matches the text, sorted and joined with '; '."""
    hits = []
    for tag, patterns in rules.items():
        flags = 0 if tag in case_sensitive else re.I
        if any(re.search(p, text, flags) for p in patterns):
            hits.append(tag)
    return "; ".join(sorted(hits))


def tag_record(title: str, text: str) -> dict[str, str]:
    words = f"{title}\n{text}"
    return {
        "condition_tags": tags(CONDITION_RULES, words),
        "design_tags": tags(DESIGN_RULES, words),
        "data_source_tags": tags(DATA_SOURCE_RULES, words),
        "geography_tags": tags(GEOGRAPHY_RULES, words, case_sensitive=frozenset(_CASE_SENSITIVE_PLACES)),
    }
