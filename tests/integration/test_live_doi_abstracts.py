"""Live route check for the DOI-harvested conferences: Crossref still lists each latest supplement as catalogued,
and OpenAlex answers for its DOIs. Fetches at test time through the harvester's own polite getter (robots.txt,
each API's rate limit, Retry-After) and writes nothing to disk."""

from __future__ import annotations

import pytest

from health_data_registry.adapters import doi_abstracts as doi
from health_data_registry.crawl import Robots, fetcher
from health_data_registry.doi_harvest import conferences, supplement
from health_data_registry.registry import load_registry

pytestmark = pytest.mark.network


def test_each_conference_route_answers_as_catalogued() -> None:
    reg = load_registry()
    # Only the DOI sources' own hosts: the enrich hosts are not in the registry-wide allowlist.
    hosts = frozenset().union(*(ds.entry.source.hosts for ds in conferences(reg).values()))
    get = fetcher(reg, None, robots=Robots(hosts, api_intervals=doi.MIN_INTERVAL_S), hosts=hosts)
    for key, ds in sorted(conferences(reg).items()):
        sup = supplement(ds, None)
        page = doi.parse_crossref(get(doi.crossref_url(sup, rows=200)).content, sup)
        assert page.total > 0, key
        assert page.records, f"{key}: no work in the first 200 passes the supplement filters"
        for r in page.records:
            assert r.volume == sup.volume and r.title, (key, r.doi)
        dois = [r.doi for r in page.records[:5]]
        found = {r.doi for r in doi.parse_openalex(get(doi.openalex_url(dois)).content)}
        assert found and found <= set(dois), key
