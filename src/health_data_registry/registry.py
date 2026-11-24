"""Load and cross-check the reviewed registry: catalog entries, ODCS contracts and semantic models.

Everything that decides what a number means lives in these three YAML files per dataset. The loader is
strict on purpose: an unknown key, a semantic model that names a column the contract lacks, or a rate
that a metric would sum is a load error, not a warning, so a bad file can't reach a clinician.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_ID = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
_SQL_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

DUCKDB_TYPES = frozenset({"VARCHAR", "BIGINT", "INTEGER", "DOUBLE", "DATE", "BOOLEAN", "TIMESTAMP"})


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


# Licence ids a maintainer has reviewed as allowing a public mirror (ADR 0009). Any other id with
# redistribution: true is refused, so one boolean can never put restricted data on the public hub.
REDISTRIBUTABLE_LICENCES = frozenset({"public-domain-us-federal"})


class Licence(_Strict):
    id: str
    redistribution: bool
    terms_url: str

    @model_validator(mode="after")
    def _reviewed(self) -> Licence:
        if self.redistribution and self.id not in REDISTRIBUTABLE_LICENCES:
            raise ValueError(
                f"licence {self.id} is not on the reviewed list of licences that allow a public copy "
                f"({', '.join(sorted(REDISTRIBUTABLE_LICENCES))}); set redistribution: false"
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


class Source(_Strict):
    portal: Literal["socrata", "who_gho", "https_csv", "croi"]
    host: str
    dataset: str
    landing_url: str
    api_url: str
    encoding: str = "utf-8"  # https_csv only: the file's text encoding, from the publisher's documentation
    reshape: Reshape | None = None  # https_csv only

    @field_validator("host")
    @classmethod
    def _lower_host(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9.-]+", v):
            raise ValueError("host must be a bare lower-case DNS name")
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


# Where a source's rows may live (ADR 0009, licences decide mirroring):
#   public   redistribution allowed: snapshotted in the lake and published on the open data hub
#   live     redistribution forbidden: read from the publisher per question, in memory only, never stored
#   private  redistribution forbidden: kept only in that source's own private store (lake/private/<id>/ locally,
#            one private Hugging Face dataset per source for the hosted server), never in a public release
Hosting = Literal["public", "live", "private"]


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
    contract: str
    semantic_model: str
    status: Literal["draft", "reviewed"]
    # Unset: public when the licence allows redistribution, else live. Set "private" to keep a restricted
    # source in its own private store (store.private) instead of reading it live.
    hosting: Hosting | None = None

    @property
    def tier(self) -> Hosting:
        if self.hosting is not None:
            return self.hosting
        return "public" if self.licence.redistribution else "live"

    @model_validator(mode="after")
    def _hosting_matches_licence(self) -> CatalogEntry:
        if self.hosting == "public" and not self.licence.redistribution:
            raise ValueError("hosting: public needs licence.redistribution: true")
        if self.hosting in ("live", "private") and self.licence.redistribution:
            raise ValueError(f"hosting: {self.hosting} is for sources whose licence forbids redistribution")
        return self

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID.fullmatch(v):
            raise ValueError("id must be snake_case, 2-63 chars")
        return v


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
    # The flag code the publisher's codebook defines as "no reported cases" (NNDSS: '-'): an empty count with it
    # is read as zero, labelled so; it is never stored as a number (hard rule 1).
    no_cases_code: str | None = None

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


def label_key(text: str) -> str:
    """A publisher label compared ignoring case, edge spaces and dots (U.S. RESIDENTS = US RESIDENTS)."""
    return str(text).strip().upper().replace(".", "")


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


_PAIR_PROPS = ("flagCodes", "partialPlaces", "renamedLabels")


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
                logical_type=str(p.get("logicalType", "")),
                description=desc,
                required=bool(p.get("required", False)),
                role=role,
                source_field=str(cp.get("sourceField") or name),
                unit=None if cp.get("unit") is None else str(cp.get("unit")),
                missing_codes=tuple(str(m) for m in missing),
                flag_for=None if cp.get("flagFor") is None else str(cp.get("flagFor")),
                flag_codes=pairs["flagCodes"],
                partial_places=pairs["partialPlaces"],
                renamed_labels=pairs["renamedLabels"],
                no_cases_code=None if zero is None else str(zero),
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
    )


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


class Measure(_Strict):
    name: str
    column: str
    kind: MeasureKind
    unit: str = ""
    description: str = ""


class Uncertainty(_Strict):
    lower: str
    upper: str


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
    uncertainty: Uncertainty | None = None
    order_by: str | None = None
    restated_by: Restatement | None = None
    # Dimensions a summed running total must be filtered to one value of (NNDSS year to date: the week), so
    # every week's running total is never added up. The compiler refuses the metric otherwise.
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


class SemanticModel(_Strict):
    dataset: str
    version: str
    dimensions: list[Dimension]
    measures: list[Measure]
    metrics: list[Metric]
    series: list[Series] = Field(default_factory=list)

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
            if metric.uncertainty:
                for b in (metric.uncertainty.lower, metric.uncertainty.upper):
                    if b not in measures:
                        raise ValueError(f"metric {metric.name} uncertainty names unknown measure {b}")
        dims = {d.name: d for d in self.dimensions}
        metrics = {m.name: m for m in self.metrics}
        for ser in self.series:
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


# --------------------------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class Dataset:
    entry: CatalogEntry
    contract: Contract
    semantic: SemanticModel

    @property
    def id(self) -> str:
        return self.entry.id


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
    if "hosting" in top and top["hosting"] != entry.tier:
        raise RegistryError(
            f"{entry.id}: the contract says hosting {top['hosting']!r} but the catalog says {entry.tier!r}"
        )
    if entry.tier == "private" and top.get("hosting") != "private":
        raise RegistryError(f"{entry.id}: a private-tier source's contract must say hosting: private too")
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
    for c in contract.columns:
        if c.flag_for is not None and c.flag_for not in cols:
            raise RegistryError(f"{entry.id}: {c.name} flags unknown column {c.flag_for}")
        if c.flag_codes and c.flag_for is None:
            raise RegistryError(f"{entry.id}: {c.name} lists flagCodes but flags no column (flagFor)")


def load_registry(root: Path | None = None) -> Registry:
    root = (root or default_root()).resolve()
    catalog_dir = root / "catalog"
    if not catalog_dir.is_dir():
        raise RegistryError(f"no catalog/ directory under {root}")
    datasets: dict[str, Dataset] = {}
    for path in sorted(catalog_dir.glob("*.yaml")):
        ds = load_dataset(root, path)
        datasets[ds.id] = ds
    return Registry(root=root, datasets=datasets)
