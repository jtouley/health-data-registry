"""The catalog schema of the crawler factory (ADR 0016): adapter type names and each type's strict config block.

A catalog entry's ``source`` names one adapter type (``type:``, or the older ``portal:`` for one release) and gives
that type's config as a block named after it. Every model here is strict (``extra="forbid"``, frozen), so a typo in
a catalog entry (``zip_memeber``, ``max_document``) is refused at load instead of silently switching a check off.

Safety caps are integers with a floor and a type-owned ceiling: ``0``, a negative number, ``null`` or a value past
the ceiling is refused, never read as "no cap". Patterns that scope a crawl are compiled when the catalog loads, are
matched whole (``fullmatch``) against a normalised path, and run on RE2, a linear-time engine, over input of bounded
length, because they run over pages a publisher controls.

This module imports nothing from the package, so ``registry.py`` can use it without an import cycle; the behaviour
of each type (how it pages, what it accepts, its capabilities) lives in ``adapters`` (``adapters.TYPES``).
"""

from __future__ import annotations

import codecs
import functools
import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The closed set of adapter types (ADR 0016: a new type is a new ADR). adapters.TYPES registers one behaviour for
# each, and a test checks that the two agree.
TypeName = Literal["socrata", "odata", "https_csv", "https_file", "html_table", "pdf_table", "tableau", "doi"]
TYPE_NAMES: Final[tuple[str, ...]] = TypeName.__args__  # type: ignore[attr-defined]
# The older ``portal:`` values, read for one release, and the type each one means.
PORTAL_TYPES: Final[dict[str, str]] = {
    "socrata": "socrata",
    "who_gho": "odata",
    "https_csv": "https_csv",
    "tableau": "tableau",
    "doi": "doi",
}
# Source fields that belong to one type and sit on the shared block for one release (they predate the nested
# blocks). Set on a source of another type, they are refused: one place for each setting.
LEGACY_FIELDS: Final[dict[str, str]] = {
    "encoding": "https_csv",
    "reshape": "https_csv",
    "tableau": "tableau",
    "supplements": "doi",
    "enrich_hosts": "doi",
    "conference": "doi",
    "conference_name": "doi",
}
# Legacy fields a type cannot do without.
REQUIRED_LEGACY: Final[dict[str, tuple[str, ...]]] = {
    "tableau": ("tableau",),
    "doi": ("supplements", "conference", "conference_name"),
}
# Types whose nested block must be given (it has a setting with no safe default).
BLOCK_REQUIRED: Final[frozenset[str]] = frozenset({"https_file", "html_table", "pdf_table"})

# The longest text a catalogued pattern is ever matched against; longer input is refused before matching.
MAX_MATCH_INPUT: Final = 2048
HOOKS: Final[tuple[str, ...]] = ("resolve", "locate", "parse", "staging")
Hook = Literal["resolve", "locate", "parse", "staging"]
# Which hooks each type runs. A staging hook runs in the shared projection, so every type that stores or stages
# records takes one; the rest run inside the type's own reader.
TYPE_HOOKS: Final[dict[str, frozenset[str]]] = {
    "socrata": frozenset({"staging"}),
    "odata": frozenset({"staging"}),
    "https_csv": frozenset({"staging"}),
    "https_file": frozenset({"resolve", "locate", "staging"}),
    "html_table": frozenset({"resolve", "parse", "staging"}),
    "pdf_table": frozenset({"parse", "staging"}),  # parse: facts the document prints once, from its text
    "tableau": frozenset(),
    "doi": frozenset(),
}


class Strict(BaseModel):
    """The base of every catalog model: unknown keys are refused and a loaded value never changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def cap(default: int, *, ceiling: int, floor: int = 1) -> Any:
    """A bounded safety cap: an integer from ``floor`` to ``ceiling``; never ``None`` or 0 meaning "no cap"."""
    return Field(default=default, ge=floor, le=ceiling)


@functools.lru_cache(maxsize=512)
def compile_pattern(pattern: str, *, ignore_case: bool = False) -> Any:
    """A catalogued pattern compiled for RE2, the linear-time engine every catalogued pattern runs on.

    RE2 never backtracks, so a crafted page can't stall a crawl; it refuses what it can't run in linear time
    (back-references, look-arounds), and so a pattern that uses them is refused when the catalog loads.
    """
    import re2

    try:
        return re2.compile(f"(?i){pattern}" if ignore_case else pattern)
    except re2.error as e:
        detail = e.args[0].decode() if e.args and isinstance(e.args[0], bytes) else str(e)
        raise ValueError(
            f"{pattern!r} can't be matched in linear time (RE2 refuses it: {detail}); write it without "
            "back-references or look-arounds"
        ) from e


def safe_pattern(pattern: str, *, groups: int | None = None) -> str:
    """Check a catalogued pattern: it compiles for RE2, and has ``groups`` groups when given."""
    compiled = compile_pattern(pattern)
    if groups is not None and compiled.groups != groups:
        raise ValueError(f"{pattern!r} must have exactly {groups} group(s), it has {compiled.groups}")
    return pattern


def check_selector(selector: str) -> str:
    """A CSS selector the HTML reader supports (adapters.html), checked when the catalog loads."""
    from health_data_registry.adapters.html import SelectorError, parse_selector

    try:
        parse_selector(selector)
    except SelectorError as e:
        raise ValueError(str(e)) from e
    return selector


def match_whole(pattern: str, text: str) -> Any:
    """``fullmatch`` of a catalogued pattern, with RE2, on bounded input: what every scope check uses.

    The match (``group``, ``groups``), or None; input longer than MAX_MATCH_INPUT never matches."""
    if len(text) > MAX_MATCH_INPUT:
        return None
    return compile_pattern(pattern).fullmatch(text)


# --------------------------------------------------------------------------------------------- per-type blocks


class SocrataConfig(Strict):
    """SODA 2.x: rows per page (this repo's default, 50,000; SODA itself returns 1,000 when no $limit is sent)."""

    page_size: int = cap(50_000, ceiling=50_000)


class ODataConfig(Strict):
    """OData v4: the most ``@odata.nextLink`` pages read before the source is refused as changed."""

    max_pages: int = cap(200, ceiling=1_000)


class HttpsCsvConfig(Strict):
    """https_csv keeps its settings (``encoding``, ``reshape``) on the shared block for one release; no block."""


def _member_path(v: str) -> str:
    if not v or v.startswith(("/", "\\")) or "\\" in v or any(p in ("", ".", "..") for p in v.split("/")):
        raise ValueError(
            f"zip_member {v!r} must be a plain relative path inside the archive (no /, .., or \\)"
        )
    return v


class HttpsFileConfig(Strict):
    """One file over HTTPS: csv or xlsx, alone or as one named member of a zip archive.

    Records are keyed by the header row's labels, every value as stored: CSV text as written; for xlsx the cell's
    stored value (a number as stored, never the rounded or percent display) with its number format beside it,
    a formula's cached value, never a recalculation.
    """

    format: Literal["csv", "xlsx"]
    encoding: str = "utf-8"  # csv only, from the publisher's documentation
    zip_member: str | None = None  # the one member read from a .zip, by exact path
    sheet: str | None = None  # xlsx only, and required: by name, never by position
    header_row: int = cap(1, ceiling=1_000)  # 1-based
    skip_footer_rows: int = Field(default=0, ge=0, le=1_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Caps on what an archive (a zip, or the xlsx container itself) may expand to.
    max_uncompressed_bytes: int = cap(200_000_000, ceiling=500_000_000)
    max_ratio: int = cap(100, ceiling=1_000)
    max_members: int = cap(1_000, ceiling=10_000)
    max_rows: int = cap(1_000_000, ceiling=5_000_000)

    @field_validator("zip_member")
    @classmethod
    def _member(cls, v: str | None) -> str | None:
        return None if v is None else _member_path(v)

    @field_validator("encoding")
    @classmethod
    def _codec(cls, v: str) -> str:
        try:
            codecs.lookup(v)
        except LookupError as e:
            raise ValueError(f"{v!r} is not a text encoding") from e
        return v

    @model_validator(mode="after")
    def _format_fields(self) -> HttpsFileConfig:
        if (self.format == "xlsx") != (self.sheet is not None):
            raise ValueError("an xlsx file names its sheet (sheet:), and only an xlsx file does")
        if self.format == "xlsx" and self.encoding != "utf-8":
            raise ValueError("encoding applies to csv only; an xlsx file declares its own")
        return self


class ListingYears(Strict):
    first: int = Field(ge=1900, le=2100)
    last: int = Field(ge=1900, le=2100)

    @model_validator(mode="after")
    def _order(self) -> ListingYears:
        if not 0 <= self.last - self.first <= 60:
            raise ValueError("listing years run from first to last, at most 60 years")
        return self


_TEMPLATE_FIELDS: Final = frozenset({"host", "year", "page", "session"})


class ListingConfig(Strict):
    """A paginated search listing, then one page per item, read by declared selectors (html_table, listing)."""

    url_template: (
        str  # https://{host}/...; placeholders {host} {year} {session} {page} only, values URL-encoded
    )
    years: ListingYears
    sessions: list[str] = Field(default_factory=list)  # filter values, one partition each (with the year)
    page_size: int = cap(10, ceiling=200)  # items the listing shows per page, as the publisher pages it
    first_page: int = Field(default=1, ge=0, le=1)
    max_pages: int = cap(500, ceiling=5_000)  # listing pages per partition before the crawl is refused
    item_link_selector: str
    item_path_pattern: str  # fullmatch on the normalised path of each item link
    results_count_selector: str
    results_count_pattern: str  # one group: the listing's own total, digits with optional thousands commas
    fields: dict[str, str]  # field name -> selector on the item page; each value is the element's text

    @field_validator("url_template")
    @classmethod
    def _template(cls, v: str) -> str:
        if not v.startswith("https://{host}/"):
            raise ValueError(
                "url_template starts with https://{host}/, so it can only reach the source's own host"
            )
        names = set(re.findall(r"\{([^{}]*)\}", v))
        if names - _TEMPLATE_FIELDS:
            raise ValueError(
                f"url_template may use only {sorted(_TEMPLATE_FIELDS)}, not {sorted(names - _TEMPLATE_FIELDS)}"
            )
        if "page" not in names:
            raise ValueError("url_template must page with {page}")
        return v

    @field_validator("item_path_pattern")
    @classmethod
    def _item(cls, v: str) -> str:
        return safe_pattern(v)

    @field_validator("results_count_pattern")
    @classmethod
    def _count(cls, v: str) -> str:
        return safe_pattern(v, groups=1)

    @field_validator("fields")
    @classmethod
    def _fields(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("a listing reads at least one field from each item page")
        for sel in v.values():
            check_selector(sel)
        return v

    @field_validator("item_link_selector", "results_count_selector")
    @classmethod
    def _selectors(cls, v: str) -> str:
        return check_selector(v)

    @model_validator(mode="after")
    def _sessions(self) -> ListingConfig:
        if ("{session}" in self.url_template) != bool(self.sessions):
            raise ValueError("sessions are listed exactly when url_template uses {session}")
        return self


class HtmlTableConfig(Strict):
    """HTML pages: one ``<table>`` by selector (shape table), or a listing of item pages (shape listing)."""

    shape: Literal["table", "listing"]
    table_selector: str | None = None
    caption: str | None = None  # the table's caption must match exactly
    header_separator: str | None = (
        None  # spanning header cells joined by this; unset: a spanning header is refused
    )
    listing: ListingConfig | None = None

    @field_validator("table_selector")
    @classmethod
    def _selector(cls, v: str | None) -> str | None:
        return None if v is None else check_selector(v)

    @model_validator(mode="after")
    def _shape(self) -> HtmlTableConfig:
        if self.shape == "table" and (self.table_selector is None or self.listing is not None):
            raise ValueError("shape table names a table_selector and no listing")
        if self.shape == "listing" and (self.listing is None or self.table_selector is not None):
            raise ValueError("shape listing gives a listing block and no table_selector")
        return self


GENERIC_HEADER_WORDS: Final[tuple[str, ...]] = ("Total", "Totals", "Average", "Group", "YTD")


class PdfTableSpec(Strict):
    """Where a PDF table's label columns are and what marks a suppressed cell (reviewed facts, hard rule 1)."""

    group_header: str = "Group"
    label_header: str = "Disease"
    suppression_marks: list[str] = Field(default_factory=lambda: ["*"])
    trailing_total: str | None = None  # a grand-total row below the table, by its label
    allow_blank: bool = False
    header_words: list[str] = Field(default_factory=lambda: list(GENERIC_HEADER_WORDS))

    @field_validator("trailing_total")
    @classmethod
    def _total(cls, v: str | None) -> str | None:
        return None if v is None else safe_pattern(v)


class PdfLive(Strict):
    """How much one clinician's question may read live; the core clamps it to the hosted deadline as well."""

    max_documents: int = cap(3, ceiling=5)
    order_by: Literal["listed", "year_in_filename"] = "listed"


class ColumnSum(Strict):
    """A printed total column that should equal the sum of other printed columns in the same row.

    Columns pair by key: ``total`` and ``parts`` each have one group, and a total column whose key is k is checked
    against every part column whose key is k ("YTD (\\d{4})" against "Q[1-4]_(\\d{4})"), so headers that change
    from report to report still pair. Both match the whole header text, as printed."""

    total: str
    parts: str

    @field_validator("total", "parts")
    @classmethod
    def _pattern(cls, v: str) -> str:
        return safe_pattern(v, groups=1)


class Reconcile(Strict):
    """Which of a table's printed totals to check against its printed parts (ADR 0016; ELT: metadata only).

    The type records every failed check on the row it concerns (``failed_checks``) and changes no value. A total
    row (its label matching ``total_rows`` whole, or the structural grand-total line below the table) should equal
    the sum of the rows it covers: a subtotal its own group's rows, the grand total every non-total row. Only the
    columns matching ``columns`` are summed down; ``column_sums`` checks across a row. A check with a suppressed
    or blank value in it is skipped, since its true sum is unknown. More than ``max_failed_rows`` failing rows
    means the table was misread, and the document is refused."""

    total_rows: str | None = None  # label pattern (fullmatch) of a subtotal row inside the table
    columns: list[str] = Field(default_factory=lambda: [".*"])  # header patterns (fullmatch) summed down
    column_sums: list[ColumnSum] = Field(default_factory=list)
    tolerance: float = Field(default=0.0, ge=0.0, le=1_000_000.0)  # absolute difference still counted equal
    max_failed_rows: int = Field(default=0, ge=0, le=1_000)

    @field_validator("total_rows")
    @classmethod
    def _total(cls, v: str | None) -> str | None:
        return None if v is None else safe_pattern(v)

    @field_validator("columns")
    @classmethod
    def _columns(cls, v: list[str]) -> list[str]:
        return [safe_pattern(p) for p in v]


# The fields of a pdf_table long-layout record besides its group and label: the printed column header and the
# printed cell text, both as printed.
LONG_FIELDS: Final[tuple[str, str]] = ("column_header", "cell_text")


class PdfTableConfig(Strict):
    """Text PDFs linked from an index page (``api_url``), tables rebuilt from word positions and borders."""

    index_link_pattern: str  # fullmatch on the normalised path of each link on the index page
    max_documents: int = cap(40, ceiling=200)  # more matching links than this: the page changed; refuse
    table: PdfTableSpec = Field(default_factory=PdfTableSpec)
    # wide: one record per table row, keyed by each printed column header. long: one record per printed cell
    # (group, label, column_header, cell_text), for tables whose headers change from document to document.
    layout: Literal["wide", "long"] = "wide"
    reconcile: Reconcile | None = None
    live: PdfLive = Field(default_factory=PdfLive)
    max_pdf_bytes: int = cap(10_000_000, ceiling=50_000_000)
    max_pdf_pages: int = cap(30, ceiling=200)

    @field_validator("index_link_pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        return safe_pattern(v)

    @model_validator(mode="after")
    def _long_fields(self) -> PdfTableConfig:
        labels = {self.table.group_header, self.table.label_header}
        if self.layout == "long" and labels & set(LONG_FIELDS):
            raise ValueError(
                f"a long layout keeps {' and '.join(LONG_FIELDS)} for each cell; a label column can't be named so"
            )
        return self


class TableauConfig(Strict):
    """tableau keeps its views on the shared block (``source.tableau``) for one release; no block."""


class DoiConfig(Strict):
    """doi keeps its supplements on the shared block for one release; no block."""


CONFIG_MODELS: Final[dict[str, type[Strict]]] = {
    "socrata": SocrataConfig,
    "odata": ODataConfig,
    "https_csv": HttpsCsvConfig,
    "https_file": HttpsFileConfig,
    "html_table": HtmlTableConfig,
    "pdf_table": PdfTableConfig,
    "tableau": TableauConfig,
    "doi": DoiConfig,
}


class ExtensionUse(Strict):
    """A source opting in to one named extension at one hook, with its arguments (checked by the extension)."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,40}$")
    hook: Hook
    args: dict[str, Any] = Field(default_factory=dict)
