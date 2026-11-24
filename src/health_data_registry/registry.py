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


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------------------------- catalog


class Years(_Strict):
    start: int
    end: int | None = None


class Licence(_Strict):
    id: str
    redistribution: bool
    terms_url: str


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


class Reshape(_Strict):
    wide_to_long: WideToLong


class Source(_Strict):
    portal: Literal["socrata", "who_gho", "https_csv"]
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

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def column_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns)


def _custom(props: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in props or []:
        if not isinstance(item, Mapping) or "property" not in item:
            raise RegistryError(f"customProperties entries need 'property' and 'value': {item!r}")
        out[str(item["property"])] = item.get("value")
    return out


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
    )


# --------------------------------------------------------------------------------------------- semantic

MeasureKind = Literal["count", "rate", "estimate", "lower_bound", "upper_bound", "percent"]


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
    """A later table restating a whole earlier period (e.g. NNDSS's prior-year year-to-date column)."""

    measure: str  # cumulative count for the previous period, as restated in the current period's table
    complete_at: int  # the order_by value from which the restatement covers the whole previous period


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
            # The spec's core rule: sum a frequency count, never sum a rate or an estimate.
            if metric.aggregation == "sum" and m.kind != "count":
                raise ValueError(f"metric {metric.name} would sum a {m.kind}; only counts may be summed")
            if metric.aggregation == "cumulative_latest":
                if m.kind != "count":
                    raise ValueError(f"metric {metric.name}: cumulative_latest needs a running count")
                if metric.order_by is None or metric.order_by not in {d.name for d in self.dimensions}:
                    raise ValueError(f"metric {metric.name}: cumulative_latest needs order_by, a dimension")
                if metric.restated_by and measures.get(metric.restated_by.measure) is None:
                    raise ValueError(f"metric {metric.name}: restated_by names an unknown measure")
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
            if ser.measure in ("cases", "deaths", "population") and kind != "count":
                raise ValueError(f"series {ser.condition} says {ser.measure} but its metric reads a {kind}")
            if ser.measure in ("rate", "percent", "estimate") and kind == "count":
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
            raise KeyError(f"unknown dataset {dataset_id!r}; catalogued: {known}") from None

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
