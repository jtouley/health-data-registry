# SPDX-License-Identifier: Apache-2.0
"""Fuzz sitemaps.parse with arbitrary bytes (a publisher's sitemap, plain or gzipped).

Property: parse returns pages or child sitemap URLs, or raises SitemapError, its documented refusal. Any other
exception is a finding. Time and memory are bounded by the libFuzzer -timeout and -rss_limit_mb set in
fuzz/fuzz_sitemap_parse.options (a gzip or entity bomb shows up as a timeout or an out-of-memory crash).
"""

from __future__ import annotations

import re
import sys
import zlib

import atheris

with atheris.instrument_imports():
    from health_data_registry.sitemaps import SitemapError, parse


# A known finding, filed with reproducers as issue #59; skipped so the harness keeps finding new ones. Set to
# False when the fix lands. An unknown or multi-byte encoding in the XML declaration escapes as LookupError or a
# bare ValueError, with exactly these messages; any other LookupError or ValueError still crashes the run.
KNOWN_ENCODING_ERRORS = True
_UNKNOWN_ENCODING = re.compile(r"unknown encoding: .*", re.DOTALL)
_MULTI_BYTE = "multi-byte encodings are not supported"
# The input shape #59 needs: an encoding pseudo-attribute in the document (the XML declaration, after any gzip).
_DECLARES_ENCODING = re.compile(rb"encoding\s*=")


class Finding(AssertionError):
    """parse returned something outside its contract."""


def _declares_encoding(data: bytes) -> bool:
    if data[:2] == b"\x1f\x8b":
        try:
            # Only the start, where the XML declaration is: a bounded read, so a gzip bomb costs nothing here.
            data = zlib.decompressobj(zlib.MAX_WBITS | 16).decompress(data, 4096)
        except zlib.error:
            return False
    return _DECLARES_ENCODING.search(data[:4096]) is not None


def known_parse_error(data: bytes, e: Exception) -> bool:
    """Whether an exception escaping parse is exactly issue #59."""
    if not KNOWN_ENCODING_ERRORS or not _declares_encoding(data):
        return False
    if type(e) is LookupError:
        return _UNKNOWN_ENCODING.fullmatch(str(e)) is not None
    return type(e) is ValueError and str(e) == _MULTI_BYTE


def TestOneInput(data: bytes) -> None:
    try:
        pages, children = parse(data)
    except SitemapError:
        return
    except Exception as e:
        if known_parse_error(data, e):
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
