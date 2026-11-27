"""Load and cross-check the reviewed registry: catalog entries, ODCS contracts and semantic models.

Everything that decides what a number means lives in these three YAML files per dataset. The loader is
strict on purpose: an unknown key, a semantic model that names a column the contract lacks, or a rate
that a metric would sum is a load error, not a warning, so a bad file can't reach a clinician.
"""

from __future__ import annotations

import functools
import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_ID = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_SQL_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
# A publisher field name goes into generated SQL and dbt Jinja: braces, quotes, backslashes and control
# characters are refused rather than escaped in every place it lands.
_UNSAFE_FIELD = re.compile(r"[{}\\'\"\x00-\x1f\x7f]")

DUCKDB_TYPES = frozenset({"VARCHAR", "BIGINT", "INTEGER", "DOUBLE", "DATE", "BOOLEAN", "TIMESTAMP"})
# The ODCS logicalType each physical type may carry: the two must say the same thing about a column.
LOGICAL_TYPES = {
    "VARCHAR": frozenset({"string"}),
    "BIGINT": frozenset({"integer"}),
    "INTEGER": frozenset({"integer"}),
    "DOUBLE": frozenset({"number"}),
    "DATE": frozenset({"date"}),
    "TIMESTAMP": frozenset({"date", "timestamp"}),
    "BOOLEAN": frozenset({"boolean"}),
}


class RegistryError(ValueError):
    """A registry file is malformed or inconsistent with another file."""


class UnknownDataset(KeyError):
    """No catalogued dataset has this id. The message is safe to show a clinician as is."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else "unknown dataset"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------------------------- catalog


class Years(_Strict):
    start: int
    end: int | None = None


# Creative Commons Attribution (any version) and CC0 text may be shared publicly (owner decision, 24 Nov 2026),
# but only with full attribution on every record and every result: who wrote it, what it is called, where it was
# published, the DOI link and the licence. ATTRIBUTION_FIELDS names those five, in that order.
ATTRIBUTION_LICENCES = frozenset({"cc-by", "cc0"})
ATTRIBUTION_FIELDS: tuple[str, ...] = ("authors", "title", "journal", "doi_url", "licence")
# Licence ids a maintainer has reviewed as allowing a public mirror (ADR 0009). Any other id with
# redistribution: true is refused, so one boolean can never put restricted data on the public hub.
REDISTRIBUTABLE_LICENCES = frozenset({"public-domain-us-federal"}) | ATTRIBUTION_LICENCES


class AttributionMissing(ValueError):
    """A CC BY or CC0 record lacks a field its attribution needs, so it may not be shared publicly."""


class Attribution(_Strict):
    """Which column holds each attribution field, for a source whose licence requires attribution."""

    authors: str
    title: str
    journal: str
    doi_url: str
    licence: str


def require_attribution(record: Mapping[str, Any], columns: Attribution) -> None:
    """Refuse (AttributionMissing) a record that lacks any attribution field; a blank value is missing."""
    missing = [f for f in ATTRIBUTION_FIELDS if not str(record.get(getattr(columns, f)) or "").strip()]
    if missing:
        raise AttributionMissing(
            "this record can't be shared publicly: its licence requires attribution and it has no "
            + ", ".join(m.replace("_", " ") for m in missing)
        )


class Licence(_Strict):
    id: str
    redistribution: bool
    terms_url: str
    # Required when redistribution is true under an attribution licence (cc-by, cc0): the columns that carry
    # each attribution field on every record.
    attribution: Attribution | None = None

    @model_validator(mode="after")
    def _reviewed(self) -> Licence:
        if self.redistribution and self.id not in REDISTRIBUTABLE_LICENCES:
            raise ValueError(
                f"licence {self.id} is not on the reviewed list of licences that allow a public copy "
                f"({', '.join(sorted(REDISTRIBUTABLE_LICENCES))}); set redistribution: false"
            )
        if self.redistribution and self.id in ATTRIBUTION_LICENCES and self.attribution is None:
            raise ValueError(
                f"licence {self.id} allows a public copy only with attribution on every record: name the "
                f"columns for {', '.join(ATTRIBUTION_FIELDS)} under licence.attribution"
            )
        return self


class WideToLong(_Strict):
    """A purely structural pivot of a wide file: columns named <stub><key> become one record per key.

    Each `keep` column is copied unchanged onto every record; for each key (e.g. the year 2023 in
    POPESTIMATE2023) the record gets `key_field` = the key text and, for each stub, `<stub>` = that column's
    value exactly as published. No value is changed, computed or dropped.
    """

    keep: list[str]
    stubs: list[str]
    key_field: str
    key_pattern: str = r"\d{4}"
    # The exact keys the file layout (codebook) lists, e.g. the vintage's years; a file with any other set of
    # keys (a year renamed, missing or added) is refused rather than pivoted. Empty: not declared.
    expected_keys: list[str] = Field(default_factory=list)


class Reshape(_Strict):
    wide_to_long: WideToLong


_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
_ISSN = re.compile(r"^\d{4}-\d{3}[\dX]$")


class Supplement(_Strict):
    """One conference year's abstracts in a journal, as Crossref lists them (doi portal only; doi_abstracts.py).

    Every field is an exact structural fact read from Crossref's own metadata for the journal (volume, issue,
    page prefix, DOI prefix, publication month), each a filter a record must pass to belong to the year.
    Which conference year a supplement holds is a maintainer's declaration, cited in ``evidence``.
    """

    year: int  # the conference year, not the publication year
    issn: str
    volume: str
    issue: str | None = None  # Crossref's issue value, e.g. Supplement_1 or S1; None when Crossref gives none
    page_prefix: str | None = None  # e.g. "S": supplement pages, when the issue is shared with regular papers
    doi_prefix: str | None = None  # e.g. 10.1093/ofid/ofaf695.
    # A regular expression the whole lower-case DOI must match, when one prefix can't say it: e.g. a year whose
    # abstracts sit under several article DOIs, beside the section DOIs ("Oral abstracts") that aren't abstracts.
    doi_pattern: str | None = None
    exclude_dois: list[str] = Field(
        default_factory=list
    )  # items of the same supplement that are not abstracts
    from_pub_date: str  # Crossref from-pub-date filter, YYYY-MM-DD
    until_pub_date: str
    evidence: str  # where the supplement-to-conference mapping comes from
    # abstract: one DOI per abstract. congress: one DOI for the whole abstract book (one record per year).
    granularity: Literal["abstract", "congress"] = "abstract"
    # by_licence: keep text where the article licence allows (doi_abstracts.abstract_tier). link_only: never keep
    # text, whatever an API says (e.g. a subscriber-only supplement): metadata and the DOI link only.
    text_policy: Literal["by_licence", "link_only"] = "by_licence"
    # False until a real sample harvest has shown the route (ISSN, filters, licences) answers as catalogued; an
    # unverified year harvests one record at most (a route check), never a full year. Which conference year a
    # supplement holds stays the maintainer's to confirm (an open question in the contract) either way.
    verified: bool = False

    @field_validator("issn")
    @classmethod
    def _issn(cls, v: str) -> str:
        if not _ISSN.fullmatch(v):
            raise ValueError("issn must look like 1234-567X")
        return v

    @field_validator("from_pub_date", "until_pub_date")
    @classmethod
    def _date(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("publication dates are YYYY-MM-DD")
        return v

    @field_validator("doi_pattern")
    @classmethod
    def _pattern(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"doi_pattern is not a regular expression: {e}") from e
            if v != v.lower():
                raise ValueError("doi_pattern matches lower-case DOIs, so write it in lower case")
        return v

    @field_validator("exclude_dois")
    @classmethod
    def _dois(cls, v: list[str]) -> list[str]:
        bad = [d for d in v if not _DOI.fullmatch(d) or d != d.lower()]
        if bad:
            raise ValueError(f"not lower-case DOIs: {', '.join(bad)}")
        return v


class TableauView(_Strict):
    """One dashboard view (the URL name after /views/<workbook>/) and the worksheets read from it.

    Each worksheet maps to the sha256 of its column list (adapters/tableau.layout_hash), recorded when the
    contract was written: a dashboard whose columns change no longer matches, and the load stops.
    """

    view: str
    worksheets: dict[str, str]
    # A count the chart carries as a number while the dashboard shows it suppressed on the same mark (its tooltip
    # reads "<16"): count field -> the field whose marker suppresses it. Where that marker is shown, the count is
    # read as suppressed (empty, with the marker in "<count> flag"), never as the number behind the chart.
    suppressed_by: dict[str, str] = {}

    @field_validator("suppressed_by")
    @classmethod
    def _suppressed_by(cls, v: dict[str, str]) -> dict[str, str]:
        if any(not k.strip() or not m.strip() or k == m for k, m in v.items()):
            raise ValueError("suppressed_by maps a count field to another field that carries its marker")
        return v

    @field_validator("view")
    @classmethod
    def _view(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError("view must be the view's URL name (letters, digits, - and _)")
        return v

    @field_validator("worksheets")
    @classmethod
    def _hashes(cls, v: dict[str, str]) -> dict[str, str]:
        if not v or not all(re.fullmatch(r"[0-9a-f]{64}", h) for h in v.values()):
            raise ValueError("worksheets must map each worksheet name to its 64-character layout sha256")
        return v


class Tableau(_Strict):
    views: list[TableauView]


# The reviewed list of what counts as a government host under the maintainer's access policy (robots.txt on such a
# host does not stop the crawler; see crawl.Robots). One place in code: a catalog entry's `government_host: true`
# is refused for any other host, so a typo or a copied entry can't switch robots.txt off for a commercial host.
# Add a suffix or a domain only through review.
GOVERNMENT_HOST_SUFFIXES: tuple[str, ...] = (
    ".gov",  # US federal, state, county and city governments (.gov is restricted to US government bodies)
    ".mil",
    ".mi.us",  # Michigan state and local governments under the .us locality space
    ".state.us",
)
# Government bodies whose own domain is not under one of the suffixes above: the named county domains of the
# Detroit metropolitan area. michigan.gov and detroitmi.gov are listed for the record (they are .gov already).
GOVERNMENT_DOMAINS: tuple[str, ...] = (
    "michigan.gov",
    "detroitmi.gov",
    "waynecounty.com",  # Wayne County, Michigan
    "oakgov.com",  # Oakland County, Michigan
    "macombgov.org",  # Macomb County, Michigan
)


def is_government_host(host: str) -> bool:
    """True when ``host`` is on the reviewed list of government suffixes and domains (see above)."""
    h = host.lower().rstrip(".")
    return h.endswith(GOVERNMENT_HOST_SUFFIXES) or any(
        h == d or h.endswith("." + d) for d in GOVERNMENT_DOMAINS
    )


class Source(_Strict):
    portal: Literal["socrata", "who_gho", "https_csv", "tableau", "doi"]
    host: str
    dataset: str
    landing_url: str
    api_url: str
    encoding: str = "utf-8"  # https_csv only: the file's text encoding, from the publisher's documentation
    reshape: Reshape | None = None  # https_csv only
    tableau: Tableau | None = None  # tableau only: the views and worksheets read
    # A government host (only a host on the reviewed list GOVERNMENT_HOST_SUFFIXES / GOVERNMENT_DOMAINS): by the
    # maintainer's access policy a robots.txt disallow there does not stop the crawler, nor (owner decision,
    # 25 Nov 2026) does a 401, 403 or 429 on its robots.txt: the crawl goes on with the honest user agent and at
    # least 10 s between requests, and the override is recorded. Default false: robots.txt is honoured in full,
    # and a 401/403/429 on it blocks the host for the run. A 401/403/429 on a data page blocks every host.
    government_host: bool = False
    # The host answers only a real browser (browser_fetch.py): Tableau Server views, michigan.gov behind Akamai.
    browser: bool = False
    # doi only: the conference years, and the other bibliographic APIs read for the same DOIs (OpenAlex, Europe
    # PMC). Every host fetched is named here, so the fetcher's allowlist stays the catalog's.
    supplements: list[Supplement] = Field(default_factory=list)
    enrich_hosts: list[str] = Field(default_factory=list)
    # doi only: the conference's short key (``harvest-doi <conference>``) and its name on every row.
    conference: str | None = None
    conference_name: str | None = None

    @field_validator("host")
    @classmethod
    def _lower_host(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9.-]+", v):
            raise ValueError("host must be a bare lower-case DNS name")
        return v

    @property
    def hosts(self) -> frozenset[str]:
        """The hosts this one source is fetched from: its publisher host and, for a doi source, its enrich hosts."""
        return frozenset((self.host, *self.enrich_hosts))

    @field_validator("enrich_hosts")
    @classmethod
    def _lower_hosts(cls, v: list[str]) -> list[str]:
        for h in v:
            if not re.fullmatch(r"[a-z0-9.-]+", h):
                raise ValueError("enrich_hosts must be bare lower-case DNS names")
        return v

    @field_validator("api_url", "landing_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("publisher URLs must be https")
        return v

    @model_validator(mode="after")
    def _csv_only(self) -> Source:
        if self.portal != "https_csv" and (self.reshape is not None or self.encoding != "utf-8"):
            raise ValueError("encoding and reshape apply only to the https_csv portal")
        if (self.portal == "tableau") != (self.tableau is not None):
            raise ValueError(
                "a tableau source lists its views (tableau.views), and only a tableau source does"
            )
        if self.government_host and not is_government_host(self.host):
            raise ValueError(
                f"government_host is accepted only for hosts on the reviewed list of government suffixes and "
                f"domains (registry.GOVERNMENT_HOST_SUFFIXES, GOVERNMENT_DOMAINS); {self.host} is not on it"
            )
        doi_only = (self.supplements, self.enrich_hosts, self.conference, self.conference_name)
        if self.portal != "doi" and any(doi_only):
            raise ValueError("supplements, enrich_hosts and conference apply only to the doi portal")
        if self.portal == "doi":
            if not self.supplements:
                raise ValueError("a doi source lists at least one supplement")
            if not (self.conference and re.fullmatch(r"[a-z][a-z0-9_]{1,30}", self.conference)):
                raise ValueError("a doi source names its conference key (lower case, e.g. idweek)")
            if not self.conference_name:
                raise ValueError("a doi source names its conference in full (conference_name)")
            years = [s.year for s in self.supplements]
            if len(set(years)) != len(years):
                raise ValueError("a doi source lists each conference year once")
        if self.reshape is not None:
            try:
                re.compile(self.reshape.wide_to_long.key_pattern)
            except re.error as e:
                raise ValueError(f"reshape key_pattern is not a valid pattern: {e}") from e
        return self


class GbdCause(_Strict):
    condition: str
    cause: str
    pathogen: str | None = None


class Gbd(_Strict):
    location_column: str | None = None
    causes: list[GbdCause] = Field(default_factory=list)


Shape = Literal[
    "surveillance_series",
    "frequency_table",
    "rate_table",
    "estimate_table",
    "record_level",
    "registry",
    "wide_indicators",
]


# Where a source's rows may live (ADR 0009, licences decide mirroring); CatalogEntry.tier. Whether the open data
# release carries a public-tier source's files is a separate, reviewed choice (CatalogEntry.hosting).
#   public   redistribution allowed: snapshotted in the lake (published only when `hosting: public`)
#   live     redistribution forbidden: read from the publisher per question, in memory only, never stored
#   private  redistribution forbidden, `private_store: true`: kept only in that source's own private store
#            (lake/private/<id>/ locally, one private Hugging Face dataset per source for the hosted server)
Tier = Literal["public", "live", "private"]


# The hub's infectious-disease areas: one Hugging Face collection per area, and a tag on each dataset card.
IdArea = Literal[
    "hiv",
    "stis",
    "hepatitis",
    "tb",
    "hais_amr",
    "respiratory",
    "vaccine_preventable",
    "enteric",
    "vector_borne",
]


AliasReview = Literal["publisher_codebook", "pending_maintainer_review", "maintainer_reviewed"]


class ConditionAlias(_Strict):
    """Another name for one of the publisher's condition labels (hard rule 1).

    An alias comes only from the publisher's own codebook (review publisher_codebook, with its URL in source) or
    from a declaration a maintainer reviews (pending_maintainer_review until they do). Never from eval or trial
    wording.
    """

    alias: str
    label: str
    source: str
    review: AliasReview


class Conditions(_Strict):
    """The publisher's own condition labels for one dataset: the vocabulary get_series accepts.

    `dimension` is the semantic-model dimension that holds the label (None when the whole dataset is one
    condition). `labels` are copied exactly as the publisher writes them, and `source` says where the list comes
    from (the codebook or the contract's documented list). `validate` fails when a snapshot holds a label that
    is not listed here.
    """

    dimension: str | None = None
    source: str = ""
    labels: list[str] = Field(default_factory=list)
    aliases: list[ConditionAlias] = Field(default_factory=list)
    # Labels whose counts are not cases in people (animals, screening swabs), each with the reason in plain words,
    # read from the label itself: get_rate, compare_rates and get_trend(fit_on="rates") give no rate per 100,000
    # people for them.
    not_a_human_case_count: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> Conditions:
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("conditions.labels lists a label twice")
        if any(not str(x).strip() for x in self.labels):
            raise ValueError("conditions.labels has an empty label")
        if self.labels and not self.source.strip():
            raise ValueError("conditions.source must say where the labels come from (codebook or contract)")
        if self.dimension is not None and not self.labels:
            raise ValueError("conditions.dimension is set but no labels are listed")
        for a in self.aliases:
            if a.label not in self.labels:
                raise ValueError(
                    f"alias {a.alias!r} points to {a.label!r}, which is not in conditions.labels"
                )
            if not a.source.strip():
                raise ValueError(f"alias {a.alias!r} needs a source (codebook URL or reviewed declaration)")
        for label, why in self.not_a_human_case_count.items():
            if label not in self.labels:
                raise ValueError(f"not_a_human_case_count names {label!r}, which is not in conditions.labels")
            if not why.strip():
                raise ValueError(f"not_a_human_case_count for {label!r} needs a reason in plain words")
        return self


class CatalogEntry(_Strict):
    id: str
    title: str
    agency: str
    publisher: str
    level: Literal["federal", "state", "county", "city", "international"]
    country: str | None = None
    research_areas: list[str]
    id_areas: list[IdArea] = Field(default_factory=list)
    years: Years
    geography: list[str]
    shape: Shape
    grain: str
    refresh: Literal["daily", "weekly", "monthly", "quarterly", "annual", "irregular"]
    licence: Licence
    citation: str
    source: Source
    codebook_url: str
    caveats: list[str] = Field(default_factory=list)
    gbd: Gbd = Field(default_factory=Gbd)
    conditions: Conditions = Field(default_factory=Conditions)
    contract: str
    semantic_model: str
    status: Literal["draft", "reviewed"]
    # Whether the open data release may carry this source's data files. Private unless a maintainer's reviewed
    # catalog change says public; only `hosting: public` is planned by the weekly refresh or published.
    hosting: Literal["public", "private"] = "private"
    # Where the rows live when the licence forbids redistribution: unset, they are read live from the publisher
    # per question (live.py); true, they are kept only in the source's own private store (private.py), never in
    # the public lake or a release. Needs `hosting: private` and a licence that forbids redistribution.
    private_store: bool = False

    @property
    def tier(self) -> Tier:
        """Where the rows may live (Tier)."""
        if self.private_store:
            return "private"
        return "public" if self.licence.redistribution else "live"

    @model_validator(mode="after")
    def _private_store_is_restricted(self) -> CatalogEntry:
        if self.private_store and (self.licence.redistribution or self.hosting == "public"):
            raise ValueError(
                "private_store is for sources whose licence forbids redistribution (and hosting: private)"
            )
        return self

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.fullmatch(v):
            raise ValueError("id must be snake_case, 2-63 chars")
        return v

    @model_validator(mode="after")
    def _public_needs_redistribution(self) -> CatalogEntry:
        if self.hosting == "public" and not self.licence.redistribution:
            raise ValueError(
                f"hosting: public needs a licence that allows redistribution; {self.licence.id} does not"
            )
        return self

    @property
    def hosted(self) -> bool:
        """True only for `hosting: public` (which also needs a licence that allows redistribution)."""
        return self.hosting == "public"


# --------------------------------------------------------------------------------------------- contract

Role = Literal["dimension", "measure", "attribute"]


@dataclass(frozen=True)
class Column:
    name: str
    physical_type: str
    logical_type: str
    description: str
    required: bool
    role: Role
    source_field: str
    unit: str | None = None
    missing_codes: tuple[str, ...] = ()
    flag_for: str | None = None
    # A flag column's codes and what the publisher's codebook says each means, e.g. ("-", "no reported cases").
    flag_codes: tuple[tuple[str, str], ...] = ()
    # Place labels that cover only part of the place they are named after: (label, note); "*" means every label.
    partial_places: tuple[tuple[str, str], ...] = ()
    # Labels the publisher renamed between years: (later label, the earlier label it continues).
    renamed_labels: tuple[tuple[str, str], ...] = ()
    # The flag code the publisher's codebook defines as "no reported cases" (NNDSS: '-'). An empty count with it
    # has no number and keeps that meaning: never counted as 0, added up or turned into a rate (issue #173).
    no_cases_code: str | None = None
    # Condition labels whose counts are not new cases of disease in the population (NNDSS: C. auris screening,
    # which counts colonisation found by screening), each with the reason in plain words: (label, why). Declared
    # on the condition column; get_rate, compare_rates and get_trend on rates give no rate per 100,000 for them.
    not_incidence: tuple[tuple[str, str], ...] = ()
    # customProperties.mayBeEmpty: true when the contract says this field can be absent or empty in a whole
    # load (e.g. a field the publisher leaves empty in every row). Every other declared field must appear, and
    # it gets no fill-rate check (quality.fill_rate_rules).
    may_be_empty: bool = False

    def partial_note(self, label: str) -> str | None:
        """The contract's note when this place label covers only part of the place it is named after."""
        notes = {label_key(k): v for k, v in self.partial_places}
        return notes.get(label_key(label), notes.get("*"))

    def canonical_label(self, label: str) -> str:
        """The label key a renamed label continues (compared ignoring case, edge spaces and dots)."""
        k = label_key(label)
        return {label_key(a): label_key(b) for a, b in self.renamed_labels}.get(k, k)

    def display_label(self, label: str) -> str:
        """The label to show: both names of a renamed area show as the contract's earlier label, so every year
        of that area reads the same; any other label shows as published."""
        names = {label_key(x): b for a, b in self.renamed_labels for x in (a, b)}
        return names.get(label_key(label), str(label))


# The edge characters label_key trims, in Python and in SQL alike: space, tab, newline, vertical tab, form feed,
# carriage return. str.strip() with no argument also trims other Unicode spaces, which DuckDB's trim() does not.
LABEL_EDGE = " \t\n\v\f\r"


def label_key(text: str) -> str:
    """A publisher label compared ignoring case, edge whitespace and dots (U.S. RESIDENTS = US RESIDENTS)."""
    return str(text).strip(LABEL_EDGE).upper().replace(".", "")


def label_key_sql(expr: str) -> str:
    """DuckDB SQL computing label_key of `expr` (an SQL expression): the same edge characters, case and dots."""
    edge = " || ".join(["' '", *(f"chr({ord(c)})" for c in LABEL_EDGE[1:])])
    return f"regexp_replace(upper(trim(CAST({expr} AS VARCHAR), {edge})), '[.]', '', 'g')"


UNRELIABLE_KINDS = ("none", "min_count", "max_relative_ci_width", "publisher_suppressed")


@dataclass(frozen=True)
class UnreliableSpec:
    """The publisher's own rule for unreliable or suppressed values (contract property unreliableRule).

    kind none: the publisher states no rule. min_count: a rate from fewer than `threshold` cases is unreliable.
    max_relative_ci_width: a rate whose 95% interval is wider than `threshold` times the rate is unreliable.
    publisher_suppressed: the publisher itself blanks cells below `threshold` (e.g. NHSN: fewer than 20 isolates
    tested), so an empty cell there is suppressed, never zero. The threshold comes only from the codebook.
    """

    kind: str
    description: str
    threshold: float | None = None


def _unreliable(value: Any, where: str) -> UnreliableSpec:
    if not isinstance(value, Mapping):
        raise RegistryError(
            f"{where}: customProperties must declare unreliableRule as {{kind, description[, threshold]}}; kind is "
            f"one of {', '.join(UNRELIABLE_KINDS)} (none when the publisher states no rule)"
        )
    kind, desc = str(value.get("kind", "")), str(value.get("description") or "").strip()
    if kind not in UNRELIABLE_KINDS or not desc:
        raise RegistryError(
            f"{where}: unreliableRule needs a kind ({', '.join(UNRELIABLE_KINDS)}) and a description quoting the "
            "publisher"
        )
    threshold = value.get("threshold")
    if kind == "none":
        if threshold is not None:
            raise RegistryError(f"{where}: unreliableRule kind none takes no threshold")
        return UnreliableSpec(kind, desc)
    try:
        t = float(str(threshold))
    except (TypeError, ValueError):
        t = 0.0
    if not t > 0:
        raise RegistryError(
            f"{where}: unreliableRule kind {kind} needs a positive threshold from the codebook"
        )
    return UnreliableSpec(kind, desc, t)


LOAD_MODES = ("snapshot", "incremental")


@dataclass(frozen=True)
class RawLoad:
    """How a source lands in raw (contract property rawLoad). Raw is append-only either way.

    snapshot: every fetch stores the complete table, dated (sources with no update keys: dashboards, files).
    incremental: planned, not supported yet. A fetch would store only rows new or changed since the last load,
    found by the publisher's own update field (e.g. Socrata's :updated_at), and staging would keep the latest
    version of each row by `key` (required contract columns). Staging keeps only the newest load today, so a
    contract declaring it is refused when the registry loads rather than serving only the last delta.
    """

    mode: str = "snapshot"
    key: tuple[str, ...] = ()
    updated_field: str | None = None
    declared: bool = False


def _raw_load(value: Any, where: str, columns: frozenset[str], required: frozenset[str]) -> RawLoad:
    if value is None:
        return RawLoad()
    if not isinstance(value, Mapping) or value.get("mode") not in LOAD_MODES:
        raise RegistryError(f"{where}: rawLoad needs mode: snapshot or incremental")
    if value["mode"] == "snapshot":
        if value.get("key") or value.get("updatedField"):
            raise RegistryError(f"{where}: a snapshot load takes no key or updatedField")
        return RawLoad(declared=True)
    key = tuple(str(k) for k in value.get("key") or ())
    updated = str(value.get("updatedField") or "").strip()
    if not key or not updated:
        raise RegistryError(
            f"{where}: an incremental load needs key (the reviewed row identity, contract columns) and updatedField "
            "(the publisher's own update field)"
        )
    unknown = [k for k in key if k not in columns]
    if unknown:
        raise RegistryError(f"{where}: rawLoad key names unknown column {unknown[0]}")
    optional = [k for k in key if k not in required]
    if optional:
        raise RegistryError(
            f"{where}: rawLoad key column {optional[0]} must be required: a row with no key can't be matched "
            "to its earlier version"
        )
    # Staging keeps only the newest load, so an incremental load would serve just the last delta: refused until
    # staging keeps the latest version of each row by key.
    raise RegistryError(
        f"{where}: rawLoad mode incremental is declared but not supported yet (staging keeps only the newest "
        "load); declare mode: snapshot"
    )


@dataclass(frozen=True)
class Contract:
    """The subset of an ODCS v3 contract the server relies on. `raw` keeps the whole document."""

    id: str
    version: str
    status: str
    columns: tuple[Column, ...]
    total_rows: str | None
    table_shape: str
    open_questions: tuple[str, ...]
    raw: Mapping[str, Any] = field(repr=False, compare=False)
    # The rule, if any, by which the publisher suppresses or marks unreliable values (from its own codebook).
    unreliable: UnreliableSpec = field(default_factory=lambda: UnreliableSpec("none", "Not stated."))
    # What a place means here (e.g. "patient residence"); "unresolved: ..." blocks GBD comparisons by place.
    geography_basis: str | None = None
    # The population a published rate divides by, in plain words.
    rate_denominator: str | None = None
    # The contract's quality rules (quality.Rule), run on every snapshot and live read and as named dbt tests.
    quality: tuple[Any, ...] = ()
    raw_load: RawLoad = field(default_factory=RawLoad)

    @property
    def unreliable_rule(self) -> str:
        """The rule in the contract's words, for display."""
        return self.unreliable.description

    @property
    def geography_basis_unresolved(self) -> bool:
        return (self.geography_basis or "").lower().startswith("unresolved")

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)

    def flag_column(self, column: str) -> Column | None:
        """The publisher's flag column for a measure column, if the contract declares one."""
        return next((c for c in self.columns if c.flag_for == column), None)


def _custom(props: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in props or []:
        if not isinstance(item, Mapping) or "property" not in item:
            raise RegistryError(f"customProperties entries need 'property' and 'value': {item!r}")
        out[str(item["property"])] = item.get("value")
    return out


_PAIR_PROPS = ("flagCodes", "partialPlaces", "renamedLabels", "notIncidence")


def _pairs(value: Any, where: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping) or not all(str(v or "").strip() for v in value.values()):
        raise RegistryError(f"{where} must map each label or code to a non-empty text")
    return tuple((str(k), str(v)) for k, v in value.items())


def parse_contract(doc: Mapping[str, Any], *, expected_id: str) -> Contract:
    if doc.get("kind") != "DataContract":
        raise RegistryError(f"{expected_id}: contract kind must be DataContract")
    if not str(doc.get("apiVersion", "")).startswith("v3."):
        raise RegistryError(f"{expected_id}: contract apiVersion must be ODCS v3.x")
    schema = doc.get("schema") or []
    if len(schema) != 1:
        raise RegistryError(f"{expected_id}: contract must have exactly one schema object")
    obj = schema[0]
    if obj.get("name") != expected_id:
        raise RegistryError(f"{expected_id}: schema object name must equal the dataset id")
    obj_custom = _custom(obj.get("customProperties"))
    if "partialAreas" in obj_custom:
        # One fact, one place: the get_rate check and the answer's note both read partialPlaces on the column.
        raise RegistryError(
            f"{expected_id}: declare places that cover only part of a place once, as partialPlaces on the place "
            "column, not as a schema-level partialAreas"
        )
    total = obj_custom.get("totalRows")
    if total is None:
        raise RegistryError(
            f"{expected_id}: schema customProperties must declare totalRows (a predicate or 'none')"
        )
    cols: list[Column] = []
    for p in obj.get("properties") or []:
        name = str(p.get("name", ""))
        if not _SQL_IDENT.fullmatch(name):
            raise RegistryError(f"{expected_id}: column name {name!r} must be a lower-case SQL identifier")
        cp = _custom(p.get("customProperties"))
        role = cp.get("role")
        if role not in ("dimension", "measure", "attribute"):
            raise RegistryError(
                f"{expected_id}.{name}: customProperties.role must be dimension, measure or attribute"
            )
        ptype = str(p.get("physicalType", "")).upper()
        if ptype not in DUCKDB_TYPES:
            raise RegistryError(
                f"{expected_id}.{name}: physicalType {ptype!r} is not a supported DuckDB type"
            )
        logical = str(p.get("logicalType", ""))
        if logical not in LOGICAL_TYPES[ptype]:
            raise RegistryError(
                f"{expected_id}.{name}: logicalType {logical!r} does not match physicalType {ptype} "
                f"(expected {' or '.join(sorted(LOGICAL_TYPES[ptype]))})"
            )
        desc = str(p.get("description") or "").strip()
        if not desc:
            raise RegistryError(f"{expected_id}.{name}: every column needs a description in clinical words")
        missing = cp.get("missingCodes") or []
        if not isinstance(missing, list):
            raise RegistryError(f"{expected_id}.{name}: missingCodes must be a list")
        if missing and cp.get("flagFor") is not None:
            # Hard rule 4: publishers' suppression and availability flags are kept as published, never blanked.
            raise RegistryError(
                f"{expected_id}.{name}: a flag column keeps its codes; it can't have missingCodes"
            )
        source_field = str(cp.get("sourceField") or name)
        if _UNSAFE_FIELD.search(source_field):
            raise RegistryError(
                f"{expected_id}.{name}: sourceField {source_field!r} has a brace, quote, backslash or control "
                "character, which generated SQL and dbt can't carry safely"
            )
        may_be_empty = cp.get("mayBeEmpty", False)
        if not isinstance(may_be_empty, bool):
            raise RegistryError(f"{expected_id}.{name}: mayBeEmpty must be true or false")
        pairs = {k: _pairs(cp.get(k), f"{expected_id}.{name}.{k}") for k in _PAIR_PROPS}
        zero = cp.get("noCasesCode")
        if zero is not None and (cp.get("flagFor") is None or str(zero) not in dict(pairs["flagCodes"])):
            raise RegistryError(
                f"{expected_id}.{name}: noCasesCode must be one of this flag column's flagCodes, quoted from the "
                "publisher's codebook"
            )
        cols.append(
            Column(
                name=name,
                physical_type=ptype,
                logical_type=logical,
                description=desc,
                required=bool(p.get("required", False)),
                role=role,
                source_field=source_field,
                unit=None if cp.get("unit") is None else str(cp.get("unit")),
                missing_codes=tuple(str(m) for m in missing),
                flag_for=None if cp.get("flagFor") is None else str(cp.get("flagFor")),
                flag_codes=pairs["flagCodes"],
                partial_places=pairs["partialPlaces"],
                renamed_labels=pairs["renamedLabels"],
                not_incidence=pairs["notIncidence"],
                no_cases_code=None if zero is None else str(zero),
                may_be_empty=may_be_empty,
            )
        )
    if not cols:
        raise RegistryError(f"{expected_id}: contract declares no properties")
    names = [c.name for c in cols]
    if len(set(names)) != len(names):
        raise RegistryError(f"{expected_id}: duplicate column names in contract")
    top_custom = _custom(doc.get("customProperties"))
    oq = top_custom.get("openQuestions") or []
    total_s = str(total).strip()
    return Contract(
        id=expected_id,
        version=str(doc.get("version", "")),
        status=str(doc.get("status", "")),
        columns=tuple(cols),
        total_rows=None if total_s.lower() == "none" else total_s,
        table_shape=str(obj_custom.get("tableShape", "")),
        open_questions=tuple(str(q) for q in oq),
        raw=doc,
        unreliable=_unreliable(top_custom.get("unreliableRule"), expected_id),
        geography_basis=_text(top_custom.get("geographyBasis")),
        rate_denominator=_text(top_custom.get("rateDenominator")),
        quality=_quality(doc, expected_id),
        raw_load=_raw_load(
            top_custom.get("rawLoad"),
            expected_id,
            frozenset(names),
            frozenset(c.name for c in cols if c.required),
        ),
    )


def _quality(doc: Mapping[str, Any], where: str) -> tuple[Any, ...]:
    from health_data_registry.quality import QualityRuleInvalid, parse_rules

    try:
        return parse_rules(doc, where)
    except QualityRuleInvalid as e:
        raise RegistryError(str(e)) from e


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------------------------- semantic

# count: a frequency count, added up. running_count: a running total (year to date), added across places only at
# a single value of the dimensions in the metric's single_value_of. maximum: a published maximum, never added.
MeasureKind = Literal[
    "count", "running_count", "maximum", "rate", "estimate", "lower_bound", "upper_bound", "percent"
]
COUNT_KINDS = frozenset({"count", "running_count", "maximum"})


class Dimension(_Strict):
    name: str
    column: str
    type: Literal["categorical", "time"]
    description: str = ""
    # True when the dimension's values overlap (a Total or All row beside its parts, Confirmed + Probable = Total)
    # or count different things (different conditions, a count beside a rate): a summed metric must then pin it to
    # one value or group by it, never add across it (hard rule 2). A model with a summed metric says true or false
    # for every dimension, so a new model can't forget; so does any model that declares one (query_sql reads them).
    non_additive: bool | None = None
    # Why the values can't be added, in plain clinical words: refusals and describe_dataset show it to clinicians.
    non_additive_reason: str | None = None

    @model_validator(mode="after")
    def _reason_goes_with_flag(self) -> Dimension:
        if self.non_additive and not (self.non_additive_reason or "").strip():
            raise ValueError(
                f"dimension {self.name}: non_additive: true needs a non_additive_reason saying why"
            )
        if self.non_additive_reason is not None and not self.non_additive:
            raise ValueError(f"dimension {self.name}: non_additive_reason only goes with non_additive: true")
        return self


class Measure(_Strict):
    name: str
    column: str
    kind: MeasureKind
    unit: str = ""
    description: str = ""


class Uncertainty(_Strict):
    lower: str
    upper: str


# Measure kinds a publisher may give an uncertainty range for: a metric reading one names its bound measures, or
# says `uncertainty: none` with a reason, so a published range is never dropped silently (hard rule 2). Rates are
# included: age-adjusted and estimated rates often come with an interval.
UNCERTAIN_KINDS = frozenset({"estimate", "percent", "rate"})


class Restatement(_Strict):
    """A later table's running total for the earlier period (e.g. NNDSS's prior-year year-to-date column).

    It is returned beside the period's own running total, never in its place: nothing in the publisher's
    codebook says the two count the same thing. It covers the whole earlier period only once it reaches that
    period's own last order_by value (week 53 in a 53-week MMWR year).
    """

    measure: str  # cumulative count for the previous period, as published in the current period's table
    # How the two figures are reconciled. The only policy so far: a domain reviewer must approve that they count
    # the same thing before either is called final; until then the restatement is an unverified observation.
    resolution_policy: Literal["require_domain_review"]


class Metric(_Strict):
    name: str
    description: str
    measure: str
    # sum: add counts; none: read as published; cumulative_latest: read a running total at the last
    # order_by value of each period, or the next period's restatement of it once that is complete.
    aggregation: Literal["sum", "none", "cumulative_latest"]
    exclude_total_rows: bool = True
    filters: list[str] = Field(default_factory=list)
    # The publisher's own bounds, or "none" with uncertainty_reason saying why there are none to return.
    uncertainty: Uncertainty | Literal["none"] | None = None
    uncertainty_reason: str | None = None
    order_by: str | None = None
    restated_by: Restatement | None = None
    # Dimensions a summed running total must be filtered to one value of (NNDSS year to date: the week), so
    # every week's running total is never added up. For a plain count, the dimensions whose values overlap or
    # count different things (Detroit within Michigan; one condition and another): each answer row then holds one
    # value of them, by a filter or by grouping. The compiler refuses the metric otherwise.
    single_value_of: list[str] = Field(default_factory=list)


SeriesMeasure = Literal["cases", "deaths", "rate", "percent", "estimate", "population"]


class Series(_Strict):
    """How a clinical question ("legionellosis cases in Michigan by year") maps onto one metric.

    `condition` is the internal code shared with the catalog's gbd.causes, so get_aligned_with_gbd can key
    a series to its GBD cause and pathogen. Case status and case definition are stated, never inferred.
    """

    condition: str
    label: str
    metric: str
    measure: SeriesMeasure
    filters: dict[str, str | int | list[str | int]] = Field(default_factory=dict)
    location_dimension: str | None = None
    # For a series with no location_dimension: what its one figure per period covers, in plain words.
    place_coverage: str | None = None
    time_dimension: str
    case_status: str | None = None
    case_definition: str | None = None
    # A weekly series (check_signal): its metric adds up counts per week, grouped by this within-year week.
    # get_series never reads weekly series, so a year is never built by adding up provisional weeks.
    week_dimension: str | None = None
    # An age-specific series (age_adjust): counts or populations split by this age-group dimension.
    age_dimension: str | None = None
    # Words of which a question must use at least one for this series to answer it, e.g. "vaccination" and
    # "coverage" for a vaccination coverage series, or "deaths" for a percent of deaths. Such a figure names a
    # disease but is not a measure of the disease itself, so a plain disease question ("COVID-19 in Ohio") never
    # gets it unasked. Empty: any question that names the series' condition.
    asked_with: list[str] = Field(default_factory=list)

    def held_dimensions(self) -> list[str]:
        """The dimensions each answer row of this series holds at one value: filtered to one value
        (is_single_value) or grouped by (time, place, week, age). The load-time check uses these, and get_series
        compiles the same filters and grouping, so the two can't drift apart."""
        fixed = [d for d, v in self.filters.items() if is_single_value(v)]
        grouped = [self.time_dimension, self.location_dimension, self.week_dimension, self.age_dimension]
        return [*fixed, *(d for d in grouped if d)]


def is_single_value(value: Any) -> bool:
    """A filter value that pins a dimension to exactly one value: a scalar, or a one-item list or tuple."""
    if value is None:
        return False
    return len(value) == 1 if isinstance(value, list | tuple) else True


class GenericSeries(_Strict):
    """A series for ANY of the catalog's condition labels: the same metric, places and years as a named series,
    with the condition dimension pinned to one publisher label at query time (bind). It reads the source's count
    measure through a reviewed metric, so every aggregation rule of that metric still applies: a frequency count
    summed without total rows, a running total read at one week, a rate never summed."""

    metric: str
    measure: SeriesMeasure
    filters: dict[str, str | int | list[str | int]] = Field(default_factory=dict)
    location_dimension: str | None = None
    place_coverage: str | None = None
    time_dimension: str
    case_status: str | None = None
    case_definition: str | None = None
    week_dimension: str | None = None
    age_dimension: str | None = None
    asked_with: list[str] = Field(default_factory=list)  # as on Series

    def bind(self, label: str, dimension: str | None) -> Series:
        """The series for one publisher label: the template with `dimension` filtered to exactly that label."""
        fields = self.model_dump()
        if dimension is not None:
            fields["filters"] = {**self.filters, dimension: label}
        return Series(condition=label, label=label, **fields)


class SemanticModel(_Strict):
    dataset: str
    version: str
    dimensions: list[Dimension]
    measures: list[Measure]
    metrics: list[Metric]
    series: list[Series] = Field(default_factory=list)
    # Templates for any catalogued condition label (catalog conditions.labels), pinned on conditions.dimension.
    generic_series: list[GenericSeries] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rules(self) -> SemanticModel:
        measures = {m.name: m for m in self.measures}
        for metric in self.metrics:
            m = measures.get(metric.measure)
            if m is None:
                raise ValueError(f"metric {metric.name} names unknown measure {metric.measure}")
            # The spec's core rule: sum a frequency count, never sum a rate, an estimate or a maximum; a running
            # total only at one value of its order (single_value_of).
            if metric.aggregation == "sum" and m.kind not in ("count", "running_count"):
                raise ValueError(f"metric {metric.name} would sum a {m.kind}; only counts may be summed")
            if metric.aggregation == "sum" and m.kind == "running_count" and not metric.single_value_of:
                raise ValueError(
                    f"metric {metric.name} would sum a running total over every week; name the dimension it is "
                    "read at in single_value_of"
                )
            unknown = [d for d in metric.single_value_of if d not in {x.name for x in self.dimensions}]
            if unknown:
                raise ValueError(
                    f"metric {metric.name}: single_value_of names unknown dimension {unknown[0]}"
                )
            if metric.aggregation == "cumulative_latest":
                if m.kind != "running_count":
                    raise ValueError(f"metric {metric.name}: cumulative_latest needs a running_count measure")
                if metric.order_by is None or metric.order_by not in {d.name for d in self.dimensions}:
                    raise ValueError(f"metric {metric.name}: cumulative_latest needs order_by, a dimension")
                if metric.restated_by:
                    r = measures.get(metric.restated_by.measure)
                    if r is None:
                        raise ValueError(f"metric {metric.name}: restated_by names an unknown measure")
                    if r.kind != "running_count":
                        raise ValueError(f"metric {metric.name}: restated_by needs a running_count measure")
            elif metric.order_by is not None or metric.restated_by is not None:
                raise ValueError(
                    f"metric {metric.name}: order_by and restated_by only apply to cumulative_latest"
                )
            _check_uncertainty(metric, m, measures)
        dims = {d.name: d for d in self.dimensions}
        # A running total kept within each period (cumulative_latest's order_by) makes that dimension one whose
        # per-period counts never add up to the period (late reports reach only the running total): the model
        # declares it non_additive, and every such metric names the same one (query.within_period reads it).
        within = {
            str(m.order_by) for m in self.metrics if m.aggregation == "cumulative_latest" and m.order_by
        }
        if len({dims[w].column for w in within}) > 1:
            raise ValueError(
                "cumulative_latest metrics keep running totals along different columns "
                f"({', '.join(sorted(within))}); order them all by one"
            )
        for w in within:
            if not dims[w].non_additive:
                raise ValueError(
                    f"dimension {w}: a cumulative_latest metric keeps a running total along it, so counts never "
                    "add up across it; declare non_additive: true with a non_additive_reason"
                )
        summed = [m.name for m in self.metrics if m.aggregation == "sum"]
        undeclared = [d.name for d in self.dimensions if d.non_additive is None]
        declared = len(undeclared) < len(self.dimensions)
        if (summed or declared) and undeclared:
            why = f"metric {summed[0]} adds up counts" if summed else "the model declares non_additive"
            raise ValueError(
                f"{why}, so every dimension must say non_additive: true (its values "
                f"overlap or count different things) or false; not declared on: {', '.join(undeclared)}"
            )
        named = {
            b
            for x in self.metrics
            if isinstance(x.uncertainty, Uncertainty)
            for b in (x.uncertainty.lower, x.uncertainty.upper)
        }
        unused = [
            m.name for m in self.measures if m.kind in ("lower_bound", "upper_bound") and m.name not in named
        ]
        if unused:
            raise ValueError(
                f"bound measure {unused[0]} is named by no metric's uncertainty, so its range would never be "
                "returned"
            )
        metrics = {m.name: m for m in self.metrics}
        templates = [g.bind("(any catalogued condition)", None) for g in self.generic_series]
        for ser in [*self.series, *templates]:
            met = metrics.get(ser.metric)
            if met is None:
                raise ValueError(f"series {ser.condition} names unknown metric {ser.metric}")
            for d in [
                *ser.filters,
                ser.time_dimension,
                *([ser.location_dimension] if ser.location_dimension else []),
                *([ser.week_dimension] if ser.week_dimension else []),
                *([ser.age_dimension] if ser.age_dimension else []),
            ]:
                if d not in dims:
                    raise ValueError(f"series {ser.condition} names unknown dimension {d}")
            if dims[ser.time_dimension].type != "time":
                raise ValueError(f"series {ser.condition}: {ser.time_dimension} is not a time dimension")
            if ser.week_dimension is not None and (
                dims[ser.week_dimension].type != "time" or met.aggregation != "sum"
            ):
                raise ValueError(
                    f"series {ser.condition}: a weekly series needs a time week_dimension and a summed metric"
                )
            kind = measures[met.measure].kind
            if ser.measure in ("cases", "deaths", "population") and kind not in ("count", "running_count"):
                raise ValueError(f"series {ser.condition} says {ser.measure} but its metric reads a {kind}")
            if ser.measure in ("rate", "percent", "estimate") and kind in COUNT_KINDS:
                raise ValueError(f"series {ser.condition} says {ser.measure} but its metric reads a count")
        for group in (self.dimensions, self.measures, self.metrics):
            names = [g.name for g in group]
            if len(set(names)) != len(names):
                raise ValueError("duplicate names in semantic model")
        return self

    def metric(self, name: str) -> Metric:
        for m in self.metrics:
            if m.name == name:
                return m
        raise KeyError(name)

    def measure(self, name: str) -> Measure:
        for m in self.measures:
            if m.name == name:
                return m
        raise KeyError(name)

    def dimension(self, name: str) -> Dimension:
        for d in self.dimensions:
            if d.name == name:
                return d
        raise KeyError(name)


def _check_uncertainty(metric: Metric, m: Measure, measures: Mapping[str, Measure]) -> None:
    """A percent, estimate or rate names its publisher's bounds, or says there are none and why (hard rule 2)."""
    u = metric.uncertainty
    if u == "none":
        if not (metric.uncertainty_reason or "").strip():
            raise ValueError(
                f"metric {metric.name}: uncertainty: none needs an uncertainty_reason saying why"
            )
        return
    if metric.uncertainty_reason is not None:
        raise ValueError(f"metric {metric.name}: uncertainty_reason only goes with uncertainty: none")
    if u is None:
        if m.kind in UNCERTAIN_KINDS:
            raise ValueError(
                f"metric {metric.name} reads a {m.kind}: name its uncertainty bound measures (uncertainty: "
                "{lower, upper}), or say uncertainty: none with an uncertainty_reason"
            )
        return
    for b, kind in ((u.lower, "lower_bound"), (u.upper, "upper_bound")):
        bound = measures.get(b)
        if bound is None:
            raise ValueError(f"metric {metric.name} uncertainty names unknown measure {b}")
        if bound.kind != kind:
            raise ValueError(f"metric {metric.name} uncertainty {b} is a {bound.kind}, not a {kind}")
        if bound.unit != m.unit:
            raise ValueError(
                f"metric {metric.name} uncertainty {b} is in {bound.unit!r}, but {m.name} is in {m.unit!r}"
            )


@functools.cache
def _pinned_columns(filters: tuple[str, ...]) -> frozenset[str]:
    import sqlglot
    from sqlglot import exp

    from health_data_registry.sql_guard import pinned_columns

    out: set[str] = set()
    for f in filters:
        out |= {c for _q, c in pinned_columns(sqlglot.parse_one(f, dialect="duckdb", into=exp.Condition))}
    return frozenset(out)


def filter_pins(filters: Sequence[str], dimensions: Sequence[Dimension]) -> frozenset[str]:
    """Dimensions a metric's own reviewed filters fix to one value, by sql_guard.pinned_columns (the one rule
    query_sql uses too): `column = 'literal'` (the column may be wrapped in lower, upper or trim), or `column IN
    (...)` of one label spelled several ways, joined by AND. Anything under OR or NOT pins nothing."""
    cols = _pinned_columns(tuple(filters))
    return frozenset(d.name for d in dimensions if d.column.casefold() in cols)


def unpinned_non_additive(semantic: SemanticModel, metric: Metric, pinned: Iterable[str]) -> list[str]:
    """The non-additive dimensions a summed metric would add across: neither fixed by its own filters nor in
    `pinned` (the dimensions a question filters to one value or groups by). Empty for any other aggregation."""
    if metric.aggregation != "sum":
        return []
    fixed = set(pinned) | filter_pins(metric.filters, semantic.dimensions)
    return [d.name for d in semantic.dimensions if d.non_additive and d.name not in fixed]


# --------------------------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class Dataset:
    entry: CatalogEntry
    contract: Contract
    semantic: SemanticModel

    @property
    def id(self) -> str:
        return self.entry.id

    @property
    def condition_labels(self) -> list[str]:
        """The publisher's own condition labels, as catalogued."""
        return list(self.entry.conditions.labels)

    def labels_for(self, text: str) -> list[str]:
        """Every catalogued label `text` names, exactly or through a declared alias, as the publisher writes them.

        Labels are compared by spelling only (case, spaces and punctuation ignored), so a condition the publisher
        spells two ways in different years ("Hepatitis B, acute" and "Hepatitis, B, acute") returns both labels.
        Each stays its own series: the variants are never merged or dropped (hard rule 2)."""
        labels = self.entry.conditions.labels
        key = spelling_key(text)
        targets = {key} | {
            spelling_key(a.label) for a in self.entry.conditions.aliases if spelling_key(a.alias) == key
        }
        return [x for x in labels if spelling_key(x) in targets]

    def not_a_human_case_count(self, label: str) -> str | None:
        """Why this label's counts are not cases in people (so no rate per 100,000 people), or None."""
        return self.entry.conditions.not_a_human_case_count.get(label)

    def not_incidence(self, label: str) -> str | None:
        """Why this publisher label's counts are not new cases of disease (so no rate per 100,000 people), or None.

        Read only from the contract's notIncidence on the condition column, never guessed from the label's words.
        """
        dim = self.entry.conditions.dimension
        if dim is None:
            return None
        column = self.contract.column(self.semantic.dimension(dim).column)
        return dict(column.not_incidence).get(label)

    def condition_series(self, label: str) -> list[Series]:
        """Series for one catalogued label: the generic templates pinned to it, or, for a dataset that is one
        condition throughout (no condition dimension), its own named series."""
        if label not in self.entry.conditions.labels:
            raise KeyError(label)
        dim = self.entry.conditions.dimension
        if dim is None:
            return list(self.semantic.series)
        return [g.bind(label, dim) for g in self.semantic.generic_series]


def spelling_key(text: str) -> str:
    """A label reduced to its letters and digits, lower case: two labels with the same key are the same words
    spelled differently ("Hepatitis, B, acute" = "Hepatitis B, acute"; "SalmonellaParatyphi infection" = "Salmonella
    Paratyphi infection"). Used to find every spelling, never to merge them."""
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def norm_label(text: str) -> str:
    """A label compared ignoring case, punctuation and repeated spaces ("Jamestown Canyon  virus" = "jamestown
    canyon virus")."""
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


@dataclass(frozen=True)
class Registry:
    root: Path
    datasets: Mapping[str, Dataset]

    def get(self, dataset_id: str) -> Dataset:
        try:
            return self.datasets[dataset_id]
        except KeyError:
            known = ", ".join(sorted(self.datasets)) or "none"
            raise UnknownDataset(f"unknown dataset {dataset_id!r}; catalogued: {known}") from None

    def __iter__(self) -> Iterator[Dataset]:
        return iter(self.datasets[k] for k in sorted(self.datasets))

    @property
    def hosts(self) -> frozenset[str]:
        """Every source's own publisher host: the allowlist of every fetcher but a DOI harvest's. A doi source's
        enrich_hosts are left out, so they are reachable only from that harvest's fetcher (Source.hosts)."""
        return frozenset(d.entry.source.host for d in self.datasets.values())


def default_root() -> Path:
    """HDR_REGISTRY, else the repo checkout (when run from source), else the copy packed in the wheel."""
    env = os.environ.get("HDR_REGISTRY")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    repo = here.parents[2]
    if (repo / "catalog").is_dir() and (repo / "contracts").is_dir():
        return repo
    return here.parent / "_registry"


def _yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RegistryError(f"{path}: not valid YAML: {e}") from e


def load_dataset(root: Path, entry_path: Path) -> Dataset:
    raw = _yaml(entry_path)
    try:
        entry = CatalogEntry.model_validate(raw)
    except ValidationError as e:
        raise RegistryError(f"{entry_path.name}: {e}") from e
    if entry.id != entry_path.stem:
        raise RegistryError(f"{entry_path.name}: id {entry.id!r} must equal the file name")
    contract_path = root / entry.contract
    semantic_path = root / entry.semantic_model
    for p in (contract_path, semantic_path):
        if not p.is_file():
            raise RegistryError(f"{entry.id}: missing {p.relative_to(root)}")
    contract = parse_contract(_yaml(contract_path), expected_id=entry.id)
    try:
        semantic = SemanticModel.model_validate(_yaml(semantic_path))
    except ValidationError as e:
        raise RegistryError(f"{semantic_path.name}: {e}") from e
    _cross_check(entry, contract, semantic)
    return Dataset(entry=entry, contract=contract, semantic=semantic)


_PREDICATE_FUNCS = frozenset(
    {"lower", "upper", "trim", "coalesce", "starts_with", "ends_with", "contains", "length"}
)


def _check_additive_needs_totals_excluded(
    dataset_id: str, contract: Contract, semantic: SemanticModel
) -> None:
    """A place or group declared additive (non_additive: false) adds up only once the publisher's total rows are
    gone (NNDSS: the states add up to US RESIDENTS only without TOTAL, US RESIDENTS and the census divisions). So a
    summed metric that keeps the total rows must say which side of the split it reads: filter that dimension to
    one value, or a dimension the totalRows predicate tests (census: the state rows, sumlev = '040')."""
    if contract.total_rows is None:
        return
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(contract.total_rows, dialect="duckdb", into=exp.Condition)
    tested = {c.name.casefold() for c in tree.find_all(exp.Column)}
    for metric in semantic.metrics:
        if metric.aggregation != "sum" or metric.exclude_total_rows:
            continue
        pins = filter_pins(metric.filters, semantic.dimensions)
        if any(semantic.dimension(d).column.casefold() in tested for d in pins):
            continue
        loose = [
            d.name
            for d in semantic.dimensions
            if d.type == "categorical" and d.non_additive is False and d.name not in pins
        ]
        if loose:
            raise RegistryError(
                f"{dataset_id}: metric {metric.name} keeps the publisher's total rows (exclude_total_rows: false) "
                f"but adds up {', '.join(loose)}, declared additive only once total rows are excluded; set "
                "exclude_total_rows: true, or filter it to one value"
            )


def check_predicate(sql: str, columns: frozenset[str], *, where: str) -> None:
    """A reviewed predicate (totalRows, metric filters) may only compare contract columns with literals.

    These strings are spliced into SQL, so they get the same suspicion as user SQL: one boolean expression,
    no subqueries, no table references, only contract columns and a few string functions.
    """
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, dialect="duckdb", into=exp.Condition)
    except Exception as e:  # sqlglot raises several error types
        raise RegistryError(f"{where}: not a valid predicate: {sql!r} ({e})") from e
    for node in tree.walk():
        if isinstance(node, exp.Subquery | exp.Select | exp.Table | exp.Placeholder | exp.Parameter):
            raise RegistryError(f"{where}: predicate may not contain {type(node).__name__}: {sql!r}")
        if isinstance(node, exp.Column):
            if node.table or node.name not in columns:
                raise RegistryError(f"{where}: predicate names unknown column {node.sql()!r}")
        elif isinstance(node, exp.Anonymous):
            raise RegistryError(f"{where}: predicate calls unknown function {node.name!r}")
        elif isinstance(node, exp.Func) and not isinstance(
            node, exp.Cast | exp.TryCast | exp.Connector | exp.Predicate | exp.Binary | exp.Unary | exp.Paren
        ):
            name = (node.sql_name() or "").lower()
            if name not in _PREDICATE_FUNCS:
                raise RegistryError(f"{where}: predicate function {name!r} is not allowed")


def _cross_check(entry: CatalogEntry, contract: Contract, semantic: SemanticModel) -> None:
    if semantic.dataset != entry.id:
        raise RegistryError(f"{entry.id}: semantic model names dataset {semantic.dataset!r}")
    top = _custom(contract.raw.get("customProperties"))
    if "redistribution" in top and bool(top["redistribution"]) != entry.licence.redistribution:
        raise RegistryError(f"{entry.id}: the contract and the catalog disagree on redistribution")
    if "tier" in top and top["tier"] != entry.tier:
        raise RegistryError(
            f"{entry.id}: the contract says tier {top['tier']!r} but the catalog says {entry.tier!r}"
        )
    if entry.tier == "private" and top.get("tier") != "private":
        raise RegistryError(f"{entry.id}: a private-store source's contract must say tier: private too")
    cols = contract.column_names
    if contract.total_rows:
        check_predicate(contract.total_rows, cols, where=f"{entry.id}: totalRows")
    for metric in semantic.metrics:
        for f in metric.filters:
            check_predicate(f, cols, where=f"{entry.id}: metric {metric.name} filter")
    for d in semantic.dimensions:
        if d.column not in cols:
            raise RegistryError(f"{entry.id}: dimension {d.name} uses unknown column {d.column}")
    for m in semantic.measures:
        if m.column not in cols:
            raise RegistryError(f"{entry.id}: measure {m.name} uses unknown column {m.column}")
        if contract.column(m.column).role != "measure":
            raise RegistryError(
                f"{entry.id}: measure {m.name} column {m.column} is not a measure in the contract"
            )
    loc = entry.gbd.location_column
    if loc is not None and loc not in cols:
        raise RegistryError(f"{entry.id}: gbd.location_column {loc} is not a contract column")
    cond = entry.conditions
    dims = {d.name: d for d in semantic.dimensions}
    if cond.dimension is not None and (
        cond.dimension not in dims or dims[cond.dimension].type != "categorical"
    ):
        raise RegistryError(
            f"{entry.id}: conditions.dimension {cond.dimension!r} is not a categorical dimension of the semantic model"
        )
    if semantic.generic_series and cond.dimension is None:
        raise RegistryError(
            f"{entry.id}: generic_series needs conditions.dimension, the dimension each label is pinned on"
        )
    for g in semantic.generic_series:
        if cond.dimension in g.filters:
            raise RegistryError(
                f"{entry.id}: a generic series may not fix {cond.dimension}; the label asked for pins it"
            )
    # Every series of a summed metric pins or groups each non-additive dimension, so no question routed through
    # it can add overlapping labels; the compiler refuses the same at run time for any other caller.
    templates = [g.bind("(any catalogued condition)", cond.dimension) for g in semantic.generic_series]
    for ser in [*semantic.series, *templates]:
        loose = unpinned_non_additive(semantic, semantic.metric(ser.metric), ser.held_dimensions())
        if loose:
            raise RegistryError(
                f"{entry.id}: series {ser.label} adds up {ser.metric} across {', '.join(loose)}, whose values "
                "overlap or count different things; filter each to one value"
            )
    _check_additive_needs_totals_excluded(entry.id, contract, semantic)
    for c in contract.columns:
        if c.flag_for is not None and c.flag_for not in cols:
            raise RegistryError(f"{entry.id}: {c.name} flags unknown column {c.flag_for}")
        if c.flag_codes and c.flag_for is None:
            raise RegistryError(f"{entry.id}: {c.name} lists flagCodes but flags no column (flagFor)")
        if c.not_incidence:
            label_col = dims[cond.dimension].column if cond.dimension in dims else None
            if c.name != label_col:
                raise RegistryError(
                    f"{entry.id}: {c.name} declares notIncidence, which belongs on the condition column "
                    f"({label_col or 'none: the catalog names no conditions.dimension'})"
                )
            unknown = [label for label, _ in c.not_incidence if label not in cond.labels]
            if unknown:
                raise RegistryError(
                    f"{entry.id}: notIncidence names {unknown[0]!r}, which is not in the catalog's conditions.labels"
                )


def load_registry(root: Path | None = None) -> Registry:
    root = (root or default_root()).resolve()
    catalog_dir = root / "catalog"
    if not catalog_dir.is_dir():
        raise RegistryError(f"no catalog/ directory under {root}")
    datasets: dict[str, Dataset] = {}
    for path in sorted(catalog_dir.glob("*.yaml")):
        ds = load_dataset(root, path)
        datasets[ds.id] = ds
    _check_hosts_agree(datasets.values())
    return Registry(root=root, datasets=datasets)


def _check_hosts_agree(datasets: Iterable[Dataset]) -> None:
    """The access policy is per host (robots.txt, the pace, a block), so every entry on one host must state it alike.

    crawl.py decides browser and portal per dataset (by its URLs), but whether robots.txt binds is a property of
    the host: two entries on one host that disagree on `government_host` would leave it to file order.
    """
    seen: dict[str, tuple[str, bool]] = {}
    for ds in datasets:
        s = ds.entry.source
        first = seen.setdefault(s.host, (ds.id, s.government_host))
        if first[1] != s.government_host:
            raise RegistryError(
                f"{ds.id} and {first[0]} are both on {s.host} but disagree on government_host; the access policy "
                "is per host, so they must agree"
            )
