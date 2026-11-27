# SPDX-License-Identifier: Apache-2.0
"""Fuzz sitemaps.parse with arbitrary bytes (a publisher's sitemap, plain or gzipped).

Property: parse returns pages or child sitemap URLs, or raises SitemapError, its documented refusal. Any other
exception is a finding. Time and memory are bounded by the libFuzzer -timeout and -rss_limit_mb set in
fuzz/fuzz_sitemap_parse.options (a gzip or entity bomb shows up as a timeout or an out-of-memory crash).
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports():
    from health_data_registry.sitemaps import SitemapError, parse


# A known finding, filed with reproducers as issue #59; skipped so the harness keeps finding new ones. Set to
# False when the fix lands. An unknown or multi-byte encoding in the XML declaration escapes as LookupError or a
# bare ValueError ("unknown encoding", "multi-byte encodings are not supported").
KNOWN_ENCODING_ERRORS = True


class Finding(AssertionError):
    """parse returned something outside its contract."""


def TestOneInput(data: bytes) -> None:
    try:
        pages, children = parse(data)
    except SitemapError:
        return
    except (LookupError, ValueError) as e:
        if KNOWN_ENCODING_ERRORS and ("encoding" in str(e)):
            return
        raise
    if pages and children:
        raise Finding("a sitemap was read as both a urlset and an index")
    for page in pages:
        if not page.loc or page.loc != page.loc.strip():
            raise Finding(f"an empty or unstripped loc: {page.loc!r}")
        if page.lastmod is not None and (not page.lastmod or page.lastmod != page.lastmod.strip()):
            raise Finding(f"an empty or unstripped lastmod: {page.lastmod!r}")
    for child in children:
        if not child or child != child.strip():
            raise Finding(f"an empty or unstripped child sitemap: {child!r}")


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
