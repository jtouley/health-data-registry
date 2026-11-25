"""A synthetic lake for the C. auris in Michigan smoke journey: hand-written rows, no real data (hard rule 5).

The rows follow the publisher's layout (NNDSS weekly on data.cdc.gov, Census county and state estimates) so the
real catalog, contracts and semantic models read them as they would read a refresh. Every number here is made
up and deliberately unlike the published one: Michigan's real 2024 C. auris counts are in the NNDSS contract's
open questions, and a smoke answer that repeats one of them must fail as a number from memory.

The lake is built in a temporary directory at test time and deleted with it; nothing is written to the repo.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eval.scorer import ToolCall
from health_data_registry.query import Engine
from health_data_registry.registry import Registry, load_registry
from health_data_registry.server import build_server
from health_data_registry.store import build_snapshot, write_ndjson
from tests.conftest import raw_fetch

ROOT = Path(__file__).resolve().parents[2]
NNDSS = "cdc_nndss_weekly"
CENSUS = "census_county_pop"
NNDSS_URL = "https://data.cdc.gov/resource/x9gk-5huc.json"
CENSUS_URL = "https://www2.census.gov/programs-surveys/popest/datasets/2020-2025/counties/totals/co-est2025-alldata.csv"

CLINICAL = "Candida auris, clinical"
SCREENING = "Candida auris, screening"
MEASLES_INDIGENOUS = "Measles, Indigenous"
MEASLES_IMPORTED = "Measles, Imported"


def nndss(
    area: str,
    label: str,
    year: int,
    week: int,
    *,
    m1: str | None = None,
    m3: str | None = None,
    m4: str | None = None,
    flag: str | None = None,
    summary: bool = False,
) -> dict[str, Any]:
    """One NNDSS weekly row in the publisher's field names; `flag` explains each count left empty."""
    return {
        "states": area, "year": str(year), "week": str(week), "label": label,
        "m1": m1, "m1_flag": "-" if m1 is not None else flag, "m2": None, "m2_flag": None,
        "m3": m3, "m3_flag": "-" if m3 is not None else flag, "m4": m4, "m4_flag": None if m4 is not None else flag,
        "location1": None if summary else area, "location2": area if summary else None, "sort_order": None,
    }  # fmt: skip


# Michigan 2024: clinical and screening are separate NNDSS labels. Year end is week 52; the 2025 table restates
# 2024 in its prior-year column (m4), kept as a separate, unverified observation. Names are upper case through
# 2024 and title case from 2025, as the publisher writes them.
NNDSS_ROWS: list[dict[str, Any]] = [
    nndss("MICHIGAN", CLINICAL, 2024, 51, m1="2", m3="19"),
    nndss("MICHIGAN", CLINICAL, 2024, 52, m1="2", m3="21"),
    nndss("MICHIGAN", SCREENING, 2024, 51, m1="3", m3="54"),
    nndss("MICHIGAN", SCREENING, 2024, 52, m3="57", flag="-"),  # no screening cases reported in week 52
    nndss("Michigan", CLINICAL, 2025, 1, m4="23", flag="-"),
    nndss("Michigan", SCREENING, 2025, 1, m1="1", m3="1", m4="60"),
    # A neighbour the jurisdiction could not report for: the value stays empty with flag U, never a zero.
    nndss("OHIO", CLINICAL, 2024, 52, flag="U"),
    # Total rows: never part of a state's figure, never added to one.
    nndss("US RESIDENTS", CLINICAL, 2024, 52, m1="40", m3="400", summary=True),
    nndss("US RESIDENTS", SCREENING, 2024, 52, m1="90", m3="900", summary=True),
    nndss("TOTAL", CLINICAL, 2024, 52, m1="41", m3="401", summary=True),
    nndss("TOTAL", SCREENING, 2024, 52, m1="91", m3="901", summary=True),
    # Measles (trial question 1): indigenous and imported are separate series; '-' all year is no reported cases.
    nndss("MICHIGAN", MEASLES_INDIGENOUS, 2023, 52, m3="1"),
    nndss("MICHIGAN", MEASLES_IMPORTED, 2023, 52, m3="3"),
    nndss("MICHIGAN", MEASLES_INDIGENOUS, 2024, 52, m3="6"),
    nndss("MICHIGAN", MEASLES_IMPORTED, 2024, 52, flag="-"),
]

# Census July 1 estimates: the denominator get_rate divides by (state rows, SUMLEV 040). Made-up population.
CENSUS_ROWS: list[dict[str, Any]] = [
    {"SUMLEV": "040", "REGION": "2", "DIVISION": "3", "STATE": "26", "COUNTY": "000", "STNAME": "Michigan",
     "CTYNAME": "Michigan", "YEAR": "2024", "POPESTIMATE": "1000000"},
    {"SUMLEV": "050", "REGION": "2", "DIVISION": "3", "STATE": "26", "COUNTY": "163", "STNAME": "Michigan",
     "CTYNAME": "Wayne County", "YEAR": "2024", "POPESTIMATE": "250000"},
]  # fmt: skip


def raw_sha256(rows: list[dict[str, Any]]) -> str:
    """The sha256 of the raw bytes `raw_fetch` stores for these rows: what every provenance line must carry."""
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


@dataclass
class Journey:
    """The MCP server over the synthetic lake, and every call a scripted client made with what it returned."""

    registry: Registry
    engine: Engine
    calls: list[ToolCall] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.server = build_server(self.registry, self.engine)

    def call(self, tool: str, **args: Any) -> str:
        """Call one tool through the MCP server (as a client would) and record it for the scorer."""
        out = asyncio.run(self.server.call_tool(tool, args))
        text = str(out.content[0].text)  # type: ignore[union-attr]
        self.calls.append(ToolCall(tool, args))
        self.responses.append(text)
        return text


def build_journey(tmp_path: Path) -> Journey:
    """The real registry (catalog/, contracts/, semantic/) over a lake built from the rows above."""
    reg = load_registry(ROOT)
    lake = tmp_path / "lake"
    lake.mkdir()
    for ds_id, url, rows in ((NNDSS, NNDSS_URL, NNDSS_ROWS), (CENSUS, CENSUS_URL, CENSUS_ROWS)):
        records = tmp_path / f"{ds_id}.ndjson"
        write_ndjson(records, rows)
        build_snapshot(lake, reg.get(ds_id), [raw_fetch(url, rows)], [records])
    return Journey(reg, Engine(registry=reg, lake=lake, hub=None, fetch_at_run_time=False))
