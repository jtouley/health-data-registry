"""Source adapters: a closed set of generic adapter types, each turning a catalog entry into raw fetches and records.

ADR 0016 (crawler factory). Every source is read by one type in ``TYPES``; what differs between two sources of a
type is their catalog config, plus named extensions (``adapters.extensions``), never code. A type declares its
behaviour as capabilities (what it accepts, what a browser load keeps, whether it checks its own layout, which
runner builds it) rather than being tested by name anywhere else.

Adapters never change a value. They page through the publisher, keep every response's bytes for the raw store, and
hand back records keyed by the publisher's own field names; the contract does the rest. ``pages`` is the one way
in: it keeps every fetch inside the source's own scope, and runs the row-level guard (hard rule 4) on every page
and every table a page was read from, whatever the type, with no way to switch it off.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from health_data_registry.adapters.errors import LayoutError
from health_data_registry.registry import CatalogEntry
from health_data_registry.source_config import Strict
from health_data_registry.store import RawFetch

Fetcher = Callable[[str], RawFetch]
__all__ = ["LayoutError"]  # re-exported: every type's layout error subclasses it (adapters/errors.py)


@dataclass(frozen=True)
class GuardTable:
    """One table a page's records were read from, by its own field labels: checked by the row guard as it is."""

    name: str
    fields: Sequence[str]
    rows: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class Page:
    fetch: RawFetch
    records: list[dict[str, Any]]
    # The sha256 of each table's column list this page was read from (Tableau worksheets), for the manifest.
    layout: dict[str, str] = field(default_factory=dict)
    # The tables the records were projected from, when that is not the records themselves (dashboard worksheets):
    # the core guards each one by its own labels as well as the page.
    tables: tuple[GuardTable, ...] = ()
    # Provenance beyond the fetched URL: the member read inside an archive and its own sha256 (manifest notes).
    provenance: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Read:
    """What a type's reader gets: the entry, its validated config, a scoped getter and the read's limits."""

    entry: CatalogEntry
    config: Any
    get: Fetcher
    max_rows: int | None = None
    fields: list[str] | None = None
    live: bool = False
    lake: Path | None = None  # where a long crawl keeps its checkpoints and pace (refresh only)
    pace_for: Callable[[str], float] | None = (
        None  # the access policy's pace for a host, when the getter has one
    )


@dataclass(frozen=True)
class AdapterType:
    """One generic adapter type and its declared capabilities."""

    name: str
    config_model: type[Strict]
    pages: Callable[[Read], Iterator[Page]] | None  # None: built by its own runner, never paged here
    publisher_fields: Callable[[CatalogEntry, Fetcher], set[str]] | None
    version: str = "1"  # bumped when a change to the type changes the records it hands on (snapshot identity)
    accept: str = "application/json"  # the media types it asks the publisher for
    browser_capture: tuple[str, ...] = ()  # response paths a browser load keeps (the data, not the page)
    checks_layout_on_read: bool = False  # no separate drift check: every read checks the layout itself
    runner: Literal["refresh", "harvest"] = "refresh"
    follows_redirects: bool = True  # False: one call is one request; a redirect is reported, not followed
    host_is_publisher: bool = True  # False: its hosts are shared APIs, so a busy message names the host
    build_command: str | None = None  # how its private store is built, with {source} fields (refresh refuses)
    # Exact paths on the source's host the type reads besides its catalogued URLs (Socrata's column metadata).
    own_paths: Callable[[CatalogEntry], list[str]] = lambda _entry: []
    # Metadata the type adds to every row it reads (hard rule 15): its contract declares a column reading each.
    adds_fields: tuple[str, ...] = ()


TYPES: dict[str, AdapterType] = {}

# Metadata a document type adds to every row it reads (hard rule 8): which document the row was read from, as the
# provenance shows it. Added, never a publisher value, so ELT allows it; a contract declares the ones it keeps.
DOCUMENT_FIELDS: tuple[str, str, str] = ("document_url", "document_sha256", "document_retrieved_at")


def document_fields(fetch: RawFetch) -> dict[str, str]:
    """The provenance of the document a row was read from: its URL with any key stripped, the sha256 of its raw
    bytes and when it was fetched (ISO 8601, UTC)."""
    from health_data_registry.access import redact_url

    return dict(
        zip(
            DOCUMENT_FIELDS,
            (redact_url(fetch.url), fetch.sha256, fetch.retrieved_at.isoformat()),
            strict=True,
        )
    )


def with_document(records: Sequence[Mapping[str, Any]], fetch: RawFetch) -> list[dict[str, Any]]:
    """``records``, each with its document's provenance; a publisher label named like one is refused."""
    meta = document_fields(fetch)
    clash = sorted({k for r in records for k in r} & set(meta))
    if clash:
        raise LayoutError(
            f"the publisher's own labels include {', '.join(clash)}, a name kept for provenance"
        )
    return [{**r, **meta} for r in records]


def register(t: AdapterType) -> AdapterType:
    if t.name in TYPES:
        raise ValueError(f"adapter type {t.name} is registered twice")
    TYPES[t.name] = t
    return t


def adapter_type(entry: CatalogEntry) -> AdapterType:
    """The registered type that reads this entry; an unknown type is refused, never read as some other type."""
    t = TYPES.get(entry.source.kind)
    if t is None:  # the catalog model only accepts registered names; a type missing from TYPES is refused
        raise ValueError(f"{entry.id}: no adapter type is named {entry.source.kind!r}")
    return t


def guard_page(name: str, page: Page) -> Page:
    """Hard rule 4, for every page of every type: each table the page was read from, then the records handed on.

    A type cannot opt out: this is the only way a page leaves ``pages``.
    """
    from health_data_registry.adapters.row_guard import check_grain

    for table in page.tables:
        check_grain(f"{name}: {table.name}", list(table.fields), table.rows)
    check_grain(name, list(dict.fromkeys(k for r in page.records for k in r)), page.records)
    return page


def parse_page(content: bytes) -> Any:
    """A publisher's HTML page as a tree (adapters.html); a page too large or too deeply nested to read is refused
    as a layout change, in the same plain words as any other."""
    from health_data_registry.adapters.html import PageTooLarge, parse_html

    try:
        return parse_html(content)
    except PageTooLarge as e:
        raise LayoutError(str(e)) from e


def scoped(entry: CatalogEntry, get: Fetcher) -> Fetcher:
    """``get``, refusing before any request a URL outside this source's own hosts and declared paths.

    The URL fetched is the normalised one that was checked (access.normalise), never the string as given: a path
    the check and the server would resolve differently (``a%2Fx/../..``) can't pass the one and reach the other.
    A getter that can be bound to one source (``for_source``) is: its redirects and the capabilities it fetches
    with are this source's, never decided by looking the URL up across the catalog.
    """
    from health_data_registry.access import ScopeRefused, in_scope, normalise

    own = adapter_type(entry).own_paths(entry)
    bind = getattr(get, "for_source", None)
    inner: Fetcher = bind(entry.source) if callable(bind) else get

    def checked(url: str) -> RawFetch:
        try:
            norm = normalise(url)
        except ScopeRefused:
            norm = None
        if norm is None or not in_scope(entry.source, norm, own_paths=own):
            from health_data_registry.access import redact_url

            raise ScopeRefused(
                f"{redact_url(url)} is outside what the catalog entry {entry.id} may read (its own hosts and "
                "declared paths), so it was not fetched"
            )
        return inner(norm)

    return checked


def pages(
    entry: CatalogEntry,
    fetcher: Fetcher,
    *,
    max_rows: int | None = None,
    fields: list[str] | None = None,
    live: bool = False,
    lake: Path | None = None,
) -> Iterator[Page]:
    """Every page of a source, each checked by the row-level guard before it is handed on (hard rule 4).

    ``fields`` are the contract's source fields; when the source stages through an extension, the raw fields that
    extension reads are handed on instead (staging.raw_fields).
    """
    from health_data_registry.staging import raw_fields

    t = adapter_type(entry)
    if t.pages is None:
        raise ValueError(f"{entry.id} is not read through a portal adapter; it is built by its own command")
    read = Read(
        entry=entry,
        config=entry.source.config(),
        get=scoped(entry, fetcher),
        max_rows=max_rows,
        fields=None if fields is None else raw_fields(entry, fields),
        live=live,
        lake=lake,
        pace_for=getattr(fetcher, "pace_for", None),
    )
    name = f"{entry.id} ({entry.source.host})"
    for page in t.pages(read):
        yield guard_page(name, page)


def publisher_fields(entry: CatalogEntry, fetcher: Fetcher) -> set[str]:
    """The field names the publisher serves today, for the contract drift check (any type that has one)."""
    t = adapter_type(entry)
    if t.publisher_fields is None or t.checks_layout_on_read:
        raise ValueError(f"{entry.id}: this source checks its layout itself on every read")
    return t.publisher_fields(entry, scoped(entry, fetcher))


def method_digest(entry: CatalogEntry) -> str:
    """A digest of everything besides the raw bytes that shapes a source's records: its type and the type's
    version, its config block (and, for the older types, their settings still on the shared block), and each
    extension's name, hook, arguments and version. Part of a snapshot's
    identity, so a config-only change builds a new snapshot instead of returning the old parse."""
    from health_data_registry.adapters.extensions import EXTENSIONS, _load
    from health_data_registry.source_config import LEGACY_FIELDS

    _load()
    t = adapter_type(entry)
    src = entry.source
    doc = {
        "type": t.name,
        "type_version": t.version,
        "config": src.config().model_dump(mode="json"),
        # The settings older types still keep on the shared block (https_csv's encoding and reshape, tableau's
        # views, a doi source's years), so a change to them is a new snapshot too.
        "legacy": {
            name: src.model_dump(mode="json", include={name}).get(name)
            for name, owner in sorted(LEGACY_FIELDS.items())
            if owner == t.name
        },
        "extensions": [
            {**use.model_dump(mode="json"), "version": EXTENSIONS[use.name].version}
            for use in entry.source.extensions
        ],
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def capabilities(entry: CatalogEntry) -> AdapterType:
    """The declared capabilities of the type that reads this entry (accept, runner, ...)."""
    return adapter_type(entry)


def _register_types() -> None:
    """Import the module that registers every type in TYPES (a static import, so a frozen build bundles it)."""
    from health_data_registry.adapters import _types

    del _types  # imported only so each type registers itself


_register_types()
