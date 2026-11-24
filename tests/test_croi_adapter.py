"""The CROI adapter's parsers on tiny hand-written pages: what it reads, and that it refuses rather than guesses."""

from __future__ import annotations

import pytest

from health_data_registry.adapters import croi
from tests.test_croi_index import YEAR, abstract_html, listing_html, number, page_url, poster_url, title


def test_listing_url() -> None:
    first = croi.listing_url(YEAR)
    assert first.startswith(f"{croi.BASE}/search-abstracts/?")
    assert f"yr={YEAR}" in first and "q=1" in first and "/page/" not in first
    assert croi.listing_url(YEAR, 3).startswith(f"{croi.BASE}/search-abstracts/page/3/?")
    with pytest.raises(ValueError):
        croi.listing_url(YEAR, 0)
    with pytest.raises(ValueError):
        croi.listing_url(croi.FIRST_YEAR - 1)
    assert croi.check_year(croi.FIRST_YEAR) == croi.FIRST_YEAR
    assert croi.check_year(croi.LAST_YEAR) == croi.LAST_YEAR
    with pytest.raises(ValueError):
        croi.check_year(croi.LAST_YEAR + 1)


@pytest.mark.parametrize(
    ("url", "year"),
    [
        (f"{croi.BASE}/abstract/2112-2025/", 2025),
        (f"{croi.BASE}/abstract/1739-2025-td/", 2025),
        (f"{croi.BASE}/abstract/2112-2025", None),
        (f"http://{croi.HOST}/abstract/2112-2025/", None),
        ("https://evil.example.com/abstract/2112-2025/", None),
        (f"{croi.BASE}/wp-content/uploads/poster.pdf", None),
    ],
)
def test_page_year(url: str, year: int | None) -> None:
    assert croi.page_year(url) == year


def test_parse_listing() -> None:
    url = croi.listing_url(YEAR)
    listing = croi.parse_listing(listing_html([0, 1, 2], 1277), url, year=YEAR)
    assert listing.total == 1277 and listing.pages == 128
    assert listing.years == [YEAR]
    assert listing.session_types == {"poster": "Poster"}
    assert listing.entries[0] == croi.ListingEntry(number(0), title(0), page_url(0))
    assert [e.abstract_number for e in listing.entries] == [number(i) for i in range(3)]
    empty = croi.parse_listing(listing_html([], 0), url, year=YEAR)
    assert empty.total == 0 and empty.pages == 1 and empty.entries == []


@pytest.mark.parametrize(
    ("html", "what"),
    [
        (listing_html([0], 1).replace(b"abstract-search-results-count", b"x"), "no results count"),
        (listing_html([0], 1).replace(b"1 Results", b"one result"), "isn't 'N Results'"),
        (listing_html([0], 1).replace(b'name="yr"', b'name="year"'), "no 'yr' filter"),
        (listing_html([0], 1).replace(b"abstract-search-results-list", b"x"), "no results list"),
        (listing_html([], 5), "5 results counted but none listed"),
        (listing_html(list(range(11)), 11), "more than 10"),
        (listing_html([0], 1, year=2024), "isn't a 2025 abstract page"),
        (listing_html([0], 1).replace("–".encode(), b":"), "isn't '<number>"),
    ],
)
def test_parse_listing_refuses_a_changed_layout(html: bytes, what: str) -> None:
    with pytest.raises(croi.LayoutError) as e:
        croi.parse_listing(html, "https://x/", year=YEAR)
    assert what in e.value.what
    assert "changed layout" in str(e.value) and e.value.url == "https://x/"


def test_parse_abstract() -> None:
    page = croi.parse_abstract(abstract_html(0), page_url(0))
    assert page.abstract_number == number(0) and page.year == YEAR
    assert page.title == title(0)
    assert page.authors == "A. Author, B. Author"
    assert page.session_title == "(P-A1) Prevention"
    assert page.session_type == "poster" and page.category == "hiv prevention"
    assert page.poster_url == poster_url(0) and page.page_url == page_url(0)
    assert page.text.splitlines()[0] == "Background"
    assert "cohort" in page.text and "<p>" not in page.text


def test_a_poster_link_off_site_is_not_kept() -> None:
    html = abstract_html(0).replace(poster_url(0).encode(), b"https://evil.example.com/p.pdf")
    assert croi.parse_abstract(html, page_url(0)).poster_url is None


@pytest.mark.parametrize(
    ("html", "what"),
    [
        (abstract_html(0).replace(b'class="entry-title"', b'class="x"'), "no single page heading"),
        (abstract_html(0).replace(b'class="abstract ', b'class="x '), "no single abstract article"),
        (abstract_html(0).replace(b'class="entry-content"', b'class="x"'), "no abstract body"),
        (abstract_html(0).replace(b"Abstract Number", b"Number"), "no 'Abstract Number'"),
        (abstract_html(0).replace(f"{number(0)} –".encode(), b"7 \xe2\x80\x93"), "differs from the sidebar"),
    ],
)
def test_parse_abstract_refuses_a_changed_layout(html: bytes, what: str) -> None:
    with pytest.raises(croi.LayoutError) as e:
        croi.parse_abstract(html, page_url(0))
    assert what in e.value.what


def test_parse_abstract_needs_an_abstract_link() -> None:
    with pytest.raises(croi.LayoutError):
        croi.parse_abstract(abstract_html(0), f"{croi.BASE}/wp-content/uploads/poster.pdf")


def test_html_parsing_is_forgiving_of_stray_tags() -> None:
    root = croi.parse_html("<div><p>a<br>b</span></p><script>x()</script><img src=y></div>")
    assert root.text() == "ab"  # inline text only; block breaks only with blocks=True
    assert root.text(blocks=True) == "a\nb"


def test_tags_come_from_fixed_rules() -> None:
    t = croi.tag_record(
        "Randomized trial of PrEP in Kenya and the USA",
        "Hepatitis C and tuberculosis surveillance in a cohort; usa lowercase is not a place",
    )
    assert t["condition_tags"] == "hepatitis_c; hiv_prevention; tuberculosis"
    assert t["design_tags"] == "cohort; randomized_trial"
    assert t["data_source_tags"] == "surveillance"
    assert t["geography_tags"] == "kenya; usa"
    assert croi.tags(croi.GEOGRAPHY_RULES, "in the usa", case_sensitive=frozenset({"usa"})) == ""
    assert croi.tag_record("", "")["condition_tags"] == ""
