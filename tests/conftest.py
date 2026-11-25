"""A small synthetic registry and lake, shaped like a real surveillance table, for unit tests."""

from __future__ import annotations

import hashlib
import json
import textwrap
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from health_data_registry.registry import Registry, load_registry
from health_data_registry.store import RawFetch, build_snapshot, write_ndjson

CATALOG = """\
id: toy_cases
title: Toy notifiable condition cases by state and year
agency: CDC
publisher: Centers for Disease Control and Prevention
level: federal
country: US
research_areas: [infectious_disease]
years: {start: 2022, end: 2024}
geography: [nation, state]
shape: surveillance_series
grain: One row per reporting area, condition and year.
refresh: weekly
licence: {id: public-domain-us-federal, redistribution: true, terms_url: "https://www.cdc.gov/other/agencymaterials.html"}
hosting: public
citation: "CDC. Toy table. https://data.cdc.gov/d/toy0-toy0"
source:
  portal: socrata
  host: data.cdc.gov
  dataset: toy0-toy0
  landing_url: https://data.cdc.gov/d/toy0-toy0
  api_url: https://data.cdc.gov/resource/toy0-toy0.json
codebook_url: https://data.cdc.gov/d/toy0-toy0
caveats: [Counts for the latest year are provisional.]
gbd:
  location_column: reporting_area
  causes:
    - {condition: legionellosis, cause: Lower respiratory infections, pathogen: Legionella spp}
    - {condition: syphilis, cause: Syphilis}
contract: contracts/toy_cases.odcs.yaml
semantic_model: semantic/toy_cases.yaml
status: draft
"""

CONTRACT = """\
apiVersion: v3.0.2
kind: DataContract
id: toy_cases
version: 0.1.0
status: draft
customProperties:
  - {property: unreliableRule, value: {kind: none, description: "None stated (toy)."}}
  - {property: openQuestions, value: ["Is 'N' a true zero or not notifiable?"]}
schema:
  - name: toy_cases
    physicalName: toy_cases
    logicalType: object
    customProperties:
      - {property: totalRows, value: "reporting_area = 'US RESIDENTS'"}
      - {property: tableShape, value: surveillance_series}
    properties:
      - name: reporting_area
        physicalType: VARCHAR
        logicalType: string
        required: true
        description: The state where the case lived, or US RESIDENTS for the national total.
        customProperties: [{property: role, value: dimension}, {property: sourceField, value: states}]
      - name: year
        physicalType: INTEGER
        logicalType: integer
        required: true
        description: MMWR year of report.
        customProperties: [{property: role, value: dimension}]
      - name: condition
        physicalType: VARCHAR
        logicalType: string
        required: true
        description: Notifiable condition.
        customProperties: [{property: role, value: dimension}, {property: sourceField, value: label}]
      - name: cases
        physicalType: BIGINT
        logicalType: integer
        description: Confirmed and probable cases reported that year.
        customProperties:
          - {property: role, value: measure}
          - {property: sourceField, value: m1}
          - {property: missingCodes, value: ["-", "N"]}
      - name: rate_per_100k
        physicalType: DOUBLE
        logicalType: number
        description: Cases per 100,000 residents, as published.
        customProperties: [{property: role, value: measure}, {property: sourceField, value: m2}]
"""

SEMANTIC = """\
dataset: toy_cases
version: 0.1.0
dimensions:
  - {name: state, column: reporting_area, type: categorical, non_additive: false, description: Reporting area}
  - {name: year, column: year, type: time, non_additive: false, description: MMWR year}
  - {name: condition, column: condition, type: categorical, non_additive: true, non_additive_reason: "each is a different condition", description: Condition}
measures:
  - {name: cases, column: cases, kind: count, unit: "1", description: reported cases}
  - {name: rate, column: rate_per_100k, kind: rate, unit: /100000, description: rate per 100k}
metrics:
  - {name: case_count, description: Reported cases, measure: cases, aggregation: sum, exclude_total_rows: true}
  - {name: case_rate, description: Published rate per 100k, measure: rate, aggregation: none, exclude_total_rows: false, uncertainty: none, uncertainty_reason: "The toy table gives no interval."}
series:
  - condition: legionellosis
    label: Legionellosis
    metric: case_count
    measure: cases
    filters: {condition: Legionellosis}
    location_dimension: state
    time_dimension: year
    case_status: confirmed and probable
    case_definition: CSTE 2005 (09-ID-04)
  - condition: legionellosis
    label: Legionellosis
    metric: case_rate
    measure: rate
    filters: {condition: Legionellosis}
    location_dimension: state
    time_dimension: year
  - condition: syphilis
    label: Syphilis
    metric: case_count
    measure: cases
    filters: {condition: Syphilis}
    location_dimension: state
    time_dimension: year
"""

RECORDS = [
    {"states": "MICHIGAN", "year": "2022", "label": "Legionellosis", "m1": "300", "m2": "3.0"},
    {"states": "MICHIGAN", "year": "2023", "label": "Legionellosis", "m1": "310", "m2": "3.1"},
    {"states": "MICHIGAN", "year": "2024", "label": "Legionellosis", "m1": "N", "m2": ""},
    {"states": "OHIO", "year": "2023", "label": "Legionellosis", "m1": "400", "m2": "3.4"},
    {"states": "MICHIGAN", "year": "2023", "label": "Syphilis", "m1": "1000", "m2": "10.0"},
    {"states": "US RESIDENTS", "year": "2023", "label": "Legionellosis", "m1": "9000", "m2": "2.7"},
]


def write_registry(
    root: Path, *, catalog: str = CATALOG, contract: str = CONTRACT, semantic: str = SEMANTIC
) -> Path:
    for d in ("catalog", "contracts", "semantic"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "catalog" / "toy_cases.yaml").write_text(textwrap.dedent(catalog))
    (root / "contracts" / "toy_cases.odcs.yaml").write_text(textwrap.dedent(contract))
    (root / "semantic" / "toy_cases.yaml").write_text(textwrap.dedent(semantic))
    return root


def raw_fetch(url: str, payload: object, when: datetime | None = None) -> RawFetch:
    body = json.dumps(payload).encode()
    return RawFetch(
        url=url,
        content=body,
        sha256=hashlib.sha256(body).hexdigest(),
        retrieved_at=when or datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
    )


@pytest.fixture(autouse=True)
def _isolated_read_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads the user's cache, a real hub, or a publisher at run time unless it asks to."""
    monkeypatch.setenv("HDR_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("HDR_HUB", "off")  # the default public hub is never read by an offline test
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setenv("HDR_FETCH_AT_RUN_TIME", "0")


@pytest.fixture(autouse=True)
def _no_straggler_outlives_its_test() -> Iterator[None]:
    """A helper a test left running past its deadline (a slow fake download) is a straggler, which poisons every
    hosted guard; wait for it here so it can't fail whichever test runs next (mutmut runs tests in its own order)."""
    from health_data_registry import deadline

    yield
    end = time.monotonic() + 10
    while deadline.stragglers() and time.monotonic() < end:
        time.sleep(0.02)


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return load_registry(write_registry(tmp_path / "reg"))


@pytest.fixture
def lake(tmp_path: Path, registry: Registry) -> Path:
    lake = tmp_path / "lake"
    lake.mkdir()
    f = tmp_path / "page.ndjson"
    write_ndjson(f, RECORDS)
    build_snapshot(
        lake,
        registry.get("toy_cases"),
        [raw_fetch("https://data.cdc.gov/resource/toy0-toy0.json", RECORDS)],
        [f],
    )
    return lake
