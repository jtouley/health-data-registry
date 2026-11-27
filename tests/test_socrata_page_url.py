"""The Socrata page URL: every selected field is quoted, so a field named after a SoQL keyword is read as a field."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from health_data_registry.adapters.socrata import page_url


def test_selected_fields_are_backtick_quoted_and_pages_keep_their_order() -> None:
    url = page_url(
        "https://data.cdc.gov/resource/abcd-1234.json", limit=10, offset=20, fields=["topic", "group"]
    )
    q = parse_qs(urlsplit(url).query)
    assert q["$select"] == ["`group`,`topic`"]
    assert (q["$order"], q["$limit"], q["$offset"]) == ([":id"], ["10"], ["20"])


def test_no_fields_means_no_select() -> None:
    url = page_url("https://data.cdc.gov/resource/abcd-1234.json?$select=x", limit=5, offset=0)
    assert "$select" not in parse_qs(urlsplit(url).query)
