"""Parity goldens for wayne_cd_reports: what clinicians see must not change when the source moves onto the generic
pdf_table type (ADR 0016).

The goldens were written from the per-source adapter (PR #27 at 172472a) on synthetic reports in every layout the
source reads. They pin three things:

- served.json: the typed table the server answers from (the live in-memory table), row for row;
- answers.json: the reported_cases and multi_year_average_cases answers, and the refusals, for a set of questions;
- printed.json: each cell exactly as the publisher printed it (report, page, group, disease, column header, cell
  text), which the raw records of the generic type must carry unchanged.

A report's sha256 depends on the PDF library's output, so the goldens hold a placeholder per report and the test
checks it against the hash of the bytes actually served. Regenerate (only on purpose, and say why in the PR) with
HDR_WRITE_GOLDEN=1.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pytest

from health_data_registry.adapters import pages
from health_data_registry.live import load_live
from health_data_registry.query import QueryRefused, compile_metric
from health_data_registry.registry import Dataset, load_registry
from health_data_registry.store import RawFetch
from tests.pdf_factory import SynthGroup, SynthReport, SynthRow, render

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden" / "wayne_cd_reports"
WRITE = os.environ.get("HDR_WRITE_GOLDEN") == "1"
BASE = "https://www.waynecountymi.gov/files/content/mainsite/v/10/government/statistics-reports"
INDEX = "https://www.waynecountymi.gov/Government/Departments/Health-Human-Veterans-Services/Public-Health/Statistics-Reports"
WHEN = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _quarterly() -> SynthReport:
    """A 2025-on quarterly report: rolling quarters, year to date, '*' for 1 to 5 cases, a data date."""
    sti = (
        SynthRow(("Chlamydia (Genital)",), ("812", "790", "771", "805", "1,602", "1,576")),
        SynthRow(("Gonorrhea",), ("240", "251", "236", "244", "491", "480")),
    )
    other = (
        SynthRow(("Candida auris",), ("*", "7", "*", "9", "*", "*")),
        SynthRow(("Legionellosis",), ("6", "11", "8", "9", "17", "17")),
    )
    return SynthReport(
        title="Out-Wayne County Quarter 2 2026 Select Communicable Diseases. Data Retrieved on 07/21/2026",
        headers=(("Q1_2025",), ("Q2_2025",), ("Q1_2026",), ("Q2_2026",), ("YTD 2025",), ("YTD 2026",)),
        groups=(SynthGroup("STI", sti), SynthGroup("Other", other)),
        group_style="merged",
        repeat_header=True,
    )


def _annual() -> SynthReport:
    """An annual report with subtotals and one publisher slip (a subtotal that disagrees with its rows)."""
    vpd = (
        SynthRow(("Pertussis",), ("3", "6", "0", "4", "13")),
        SynthRow(("Varicella",), ("2", "3", "1", "4", "10")),
        SynthRow(("Subtotal",), ("5", "9", "11", "8", "33")),
    )
    other = (SynthRow(("Legionellosis",), ("7", "8", "9", "10", "34")),)
    return SynthReport(
        title="Out-Wayne County Case Counts January-December (Q1-Q4) 2024",
        headers=(("Q1 2024",), ("Q2 2024",), ("Q3 2024",), ("Q4 2024",), ("Q1-Q4 2024", "Total")),
        groups=(SynthGroup("VPD", vpd), SynthGroup("Other", other)),
        group_style="repeat",
        repeat_header=True,
    )


def _grand_total() -> SynthReport:
    """An annual report with a grand-total line below the table and one row whose own sums don't add up."""
    rows = (
        SynthRow(("Candida auris",), ("1", "2", "3", "4", "10")),
        SynthRow(("Legionellosis",), ("5", "6", "7", "8", "27")),
    )
    return SynthReport(
        title="Excluding Detroit: Select Reportable Disease Case Counts for 2023",
        headers=(("Q1",), ("Q2",), ("Q3",), ("Q4",), ("Totals",)),
        groups=(SynthGroup("Other", rows),),
        group_style="repeat",
        repeat_header=True,
        trailing_total=("6", "8", "10", "12", "37"),
    )


def _five_year() -> SynthReport:
    """A multi-year report that doesn't say whether Detroit is included, with a total and an average."""
    rows = (
        SynthRow(("Pertussis",), ("2", "4", "6", "3")),
        SynthRow(("Varicella",), ("*", "7", "9", "4.5")),
        SynthRow(("Mumps",), ("1", "2", "3", "1.5")),
    )
    return SynthReport(
        title="Select Communicable Disease Case Counts 2021-2022",
        headers=(("2021",), ("2022",), ("2021- 2022", "Total"), ("2-Year", "Average")),
        groups=(SynthGroup("VPD", rows),),
        group_style="repeat",
        repeat_header=True,
    )


REPORTS = {
    f"{BASE}/2026-q2-public.pdf": _quarterly,
    "https://www.waynecountymi.gov/files/assets/mainsite/v/1/documents/publichealth/quarterly-report-2024.pdf": _annual,
    "https://www.waynecountymi.gov/files/assets/mainsite/v/1/documents/publichealth/quarterly-report-2023.pdf": (
        _grand_total
    ),
    f"{BASE}/2021-2022-5-year-report.pdf": _five_year,
}

# Questions a clinician's tools ask, by metric, filters, group_by and period.
QUESTIONS: list[tuple[str, dict[str, Any], list[str], dict[str, Any] | None]] = [
    ("reported_cases", {"report": f"{BASE}/2026-q2-public.pdf", "period_column": "YTD 2026"}, [], None),
    (
        "reported_cases",
        {"report": f"{BASE}/2026-q2-public.pdf", "period_column": "Q2_2025"},
        ["disease_group"],
        None,
    ),
    (
        "reported_cases",
        {"report": f"{BASE}/2026-q2-public.pdf", "period_column": "Q1_2026"},
        ["disease"],
        None,
    ),
    ("reported_cases", {"report": list(REPORTS)[1], "period_column": "Q1-Q4 2024 Total"}, [], None),
    ("reported_cases", {"report": list(REPORTS)[1], "period_column": "Q3 2024"}, ["disease_group"], None),
    ("reported_cases", {"report": list(REPORTS)[2], "period_column": "Totals"}, [], None),
    ("reported_cases", {"report": list(REPORTS)[3], "period_column": "2022"}, [], None),
    ("reported_cases", {"report": list(REPORTS)[3], "period_column": "2021"}, [], None),
    ("reported_cases", {"report": list(REPORTS)[3], "period_column": "2-Year Average"}, [], None),
    (
        "reported_cases",
        {"report": list(REPORTS)[3], "period_column": "2021- 2022 Total"},
        [],
        {"start": 2021},
    ),
    ("reported_cases", {"period_column": "Q1_2026"}, ["report"], None),
    ("multi_year_average_cases", {"report": list(REPORTS)[3]}, ["disease"], None),
    ("multi_year_average_cases", {"report": list(REPORTS)[3]}, [], {"start": 2021, "end": 2022}),
    # refusals
    ("reported_cases", {"period_column": "Q1_2026"}, [], None),
    ("reported_cases", {"report": f"{BASE}/2026-q2-public.pdf"}, [], None),
    ("reported_cases", {"report": list(REPORTS)[:2], "period_column": "YTD 2026"}, [], None),
    ("no_such_metric", {}, [], None),
]


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return load_registry(ROOT).get("wayne_cd_reports")


@pytest.fixture(scope="module")
def site() -> dict[str, bytes]:
    index = "".join(f'<a href="{u}">report</a>' for u in REPORTS).encode()
    return {
        INDEX: index,
        **{u: build() for u, build in ((u, lambda b=b: render(b())) for u, b in REPORTS.items())},
    }


def _get(site: dict[str, bytes]) -> Any:
    def get(url: str) -> RawFetch:
        body = site[url]
        return RawFetch(url=url, content=body, sha256=hashlib.sha256(body).hexdigest(), retrieved_at=WHEN)

    return get


def _placeholders(site: dict[str, bytes]) -> dict[str, str]:
    """Each report's real sha256 -> a stable placeholder naming the report."""
    return {
        hashlib.sha256(body).hexdigest(): f"<sha256 of {u.rsplit('/', 1)[-1]}>" for u, body in site.items()
    }


def _plain(value: Any, shas: dict[str, str]) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value)
    return shas.get(text, text)


@pytest.fixture(scope="module")
def served(ds: Dataset, site: dict[str, bytes]) -> tuple[duckdb.DuckDBPyConnection, list[dict[str, Any]]]:
    recs = [
        {k: (None if v is None else str(v)) for k, v in r.items()}
        for p in pages(ds.entry, _get(site))
        for r in p.records
    ]
    con = duckdb.connect()
    load_live(con, ds, recs)
    return con, recs


def _check(name: str, got: Any) -> None:
    path = GOLDEN / name
    text = json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if WRITE:
        GOLDEN.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert path.is_file(), f"no golden {path}; run with HDR_WRITE_GOLDEN=1 once, on purpose"
    assert text == path.read_text(encoding="utf-8"), f"{name} differs from the golden"


def test_served_table_matches_golden(
    ds: Dataset, site: dict[str, bytes], served: tuple[duckdb.DuckDBPyConnection, list[dict[str, Any]]]
) -> None:
    con, _ = served
    shas = _placeholders(site)
    cols = [c.name for c in ds.contract.columns]
    rows = con.execute(f'SELECT {", ".join(cols)} FROM "{ds.id}" ORDER BY ALL').fetchall()  # noqa: S608 - contract names
    _check("served.json", {"columns": cols, "rows": [[_plain(v, shas) for v in r] for r in rows]})


def test_answers_and_refusals_match_golden(
    ds: Dataset, site: dict[str, bytes], served: tuple[duckdb.DuckDBPyConnection, list[dict[str, Any]]]
) -> None:
    con, _ = served
    shas = _placeholders(site)
    out = []
    for metric, filters, group_by, period in QUESTIONS:
        q = {"metric": metric, "filters": filters, "group_by": group_by, "period": period}
        try:
            c = compile_metric(ds, metric, filters=filters, group_by=group_by, period=period)
        except QueryRefused as e:
            out.append({**q, "refused": e.code, "message": str(e)})
            continue
        rows = con.execute(c.sql, c.params).fetchall()
        out.append(
            {
                **q,
                "columns": c.columns,
                "rows": [[_plain(v, shas) for v in r] for r in rows],
                "method": c.method,
            }
        )
    _check("answers.json", out)


def test_cells_as_printed_match_golden(
    site: dict[str, bytes], served: tuple[duckdb.DuckDBPyConnection, list[dict[str, Any]]]
) -> None:
    """What the publisher printed, cell by cell: the generic type's raw records must carry exactly these."""
    _, recs = served
    shas = _placeholders(site)
    keys = ["report_url", "report_sha256", "page", "disease_group", "disease", "column", "printed"]
    _check("printed.json", sorted([[_plain(r[k], shas) for k in keys] for r in recs], key=str))


def test_every_report_hash_is_its_own_bytes(
    site: dict[str, bytes], served: tuple[duckdb.DuckDBPyConnection, list[dict[str, Any]]]
) -> None:
    _, recs = served
    for r in recs:
        assert r["report_sha256"] == hashlib.sha256(site[r["report_url"]]).hexdigest()
