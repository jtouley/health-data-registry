"""wayne_cd_reports on main's core (ADR 0016 row 9): pdf_table config, printed_facts, period_header_parser.

The catalog entry is the real one; every report is a tiny synthetic PDF drawn by tests/adapters/pdf_fixture.py, in
the layouts the county prints (hard rule 5: no publisher file). Each case is read through the type, then staged
live through the real contract, as the server does for a source that is never stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import duckdb
import pytest

from health_data_registry.adapters import LayoutError, pages
from health_data_registry.adapters.extensions import ExtensionRefused
from health_data_registry.adapters.extensions import period_header_parser as php
from health_data_registry.adapters.extensions import printed_facts as pf
from health_data_registry.live import load_live
from health_data_registry.registry import Dataset, load_registry
from tests.adapters.helpers import Site
from tests.adapters.pdf_fixture import Table, render

pytestmark = pytest.mark.usefixtures("no_sockets")

HOST = "https://www.waynecountymi.gov"
INDEX = f"{HOST}/Government/Departments/Health-Human-Veterans-Services/Public-Health/Statistics-Reports"
STORE = "/files/content/mainsite/government/departments/hhvs/public-health"


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return load_registry().get("wayne_cd_reports")


def index(*hrefs: str) -> bytes:
    return ("<html><body>" + "".join(f'<a href="{h}">report</a>' for h in hrefs) + "</body></html>").encode()


ANNUAL_2023 = Table(
    title="Select Reportable Communicable Disease Counts for 2023",
    notes=("Wayne County excluding Detroit",),
    headers=("Q1 (1/1-3/31)", "Q2 (4/1-6/30)", "Q3 (7/1-9/30)", "Q4 (10/1-12/31)", "Totals"),
    rows=(
        ("STI", "Chlamydia (Genital)", ("1,001", "999", "1,010", "990", "4,000")),
        ("STI", "Gonorrhea", ("300", "310", "290", "300", "1,200")),
        ("STI", "Subtotal", ("1,301", "1,309", "1,300", "1,290", "5,200")),
    ),
    value_w=64.0,
)
QUARTERLY_2026 = Table(
    title="Quarter 1 2026 Communicable Disease Report",
    notes=("Out-Wayne", "Retrieved on 04/21/2026"),
    headers=("Q1_2025", "Q1_2026", "YTD 2025", "YTD 2026"),
    rows=(
        ("Foodborne", "Salmonellosis", ("12", "*", "12", "*")),
        ("Foodborne", "Campylobacter", ("20", "18", "20", "18")),
    ),
    value_w=56.0,
)
FIVE_YEAR = Table(
    title="Five-year report",
    headers=("2024", "2021-2025 Total", "5-Year Average"),
    rows=(("Vaccine Preventable", "Pertussis", ("14", "60", "12.0")),),
    value_w=90.0,
)


@dataclass
class Case:
    name: str
    reports: dict[str, Table]
    want: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)  # (disease, header) -> staged
    refused: type[Exception] | None = None
    match: str | None = None
    extra_links: tuple[str, ...] = ()


CASES = [
    Case(
        "annual report: quarters take the title year; the area and thousands are read as printed",
        {f"{STORE}/quarterly-report-2023.pdf": ANNUAL_2023},
        {
            ("Chlamydia (Genital)", "Q1 (1/1-3/31)"): {
                "cell_text": "1,001",
                "printed_number": 1001.0,
                "period_kind": "quarter",
                "period_year": 2023,
                "period_quarter": 1,
                "area_statement": "excluding Detroit",
                "report_year": 2023,
            },
            ("Gonorrhea", "Totals"): {"printed_number": 1200.0, "period_kind": "year", "period_year": 2023},
            ("Subtotal", "Totals"): {"printed_number": 5200.0, "failed_checks": "[]"},
        },
    ),  # fmt: skip
    Case(
        "quarterly report: '*' kept as printed with no number; YTD and its retrieval date",
        {f"{STORE}/2026-q1-public.pdf": QUARTERLY_2026},
        {
            ("Salmonellosis", "Q1_2026"): {
                "cell_text": "*",
                "printed_number": None,
                "period_kind": "quarter",
                "period_year": 2026,
                "period_quarter": 1,
                "area_statement": "Out-Wayne",
                "data_retrieved_on": "04/21/2026",
            },
            ("Campylobacter", "YTD 2025"): {
                "printed_number": 20.0,
                "period_kind": "year_to_date",
                "period_year": 2025,
            },
        },
    ),  # fmt: skip
    Case(
        "5-year report: silent on Detroit, so the area is empty, never assumed; the average as printed",
        {f"{STORE}/2021-2025-5-year-report.pdf": FIVE_YEAR},
        {
            ("Pertussis", "2021-2025 Total"): {
                "printed_number": 60.0,
                "period_kind": "multi_year_total",
                "period_year": 2021,
                "period_year_end": 2025,
                "area_statement": None,
            },
            ("Pertussis", "5-Year Average"): {
                "printed_number": 12.0,
                "period_kind": "multi_year_average",
                "period_year": None,
            },
            ("Pertussis", "2024"): {"period_kind": "year", "period_year": 2024},
        },
    ),  # fmt: skip
    Case(
        "reports outside the checked layouts are never fetched (2014-2020, other paths)",
        {f"{STORE}/2026-q1-public.pdf": QUARTERLY_2026},
        {("Campylobacter", "Q1_2025"): {"printed_number": 20.0}},
        extra_links=("/files/old/quarterly-report-2019.pdf", "/Statistics/quarterly-report-2018.pdf"),
    ),
    Case(
        "a printed total that disagrees with its parts keeps a note; the value stays as printed",
        {
            f"{STORE}/quarterly-report-2023.pdf": Table(
                **{
                    **ANNUAL_2023.__dict__,
                    "rows": (
                        *ANNUAL_2023.rows[:2],
                        ("STI", "Subtotal", ("1,301", "1,309", "1,300", "1,290", "5,300")),
                    ),
                }
            )
        },
        {
            ("Subtotal", "Totals"): {
                "printed_number": 5300.0,
                "failed_checks": '["STI Subtotal: Totals is 5300, but the 2 rows it totals sum to 5200"]',
            }
        },
    ),  # fmt: skip
    Case(
        "a report with no Group/Disease table is refused, never read as empty",
        {f"{STORE}/2026-q1-public.pdf": Table(**{**QUARTERLY_2026.__dict__, "group_header": "Region"})},
        refused=LayoutError,
        match="no table",
    ),
]


def _read(ds: Dataset, case: Case) -> tuple[Site, list[dict[str, Any]]]:
    site = Site(
        {
            INDEX: index(*case.reports, *case.extra_links),
            **{HOST + p: render(t) for p, t in case.reports.items()},
        }
    )
    fields = sorted({c.source_field for c in ds.contract.columns})
    _index, *docs = list(pages(ds.entry, site, fields=fields))
    return site, [r for p in docs for r in p.records]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_wayne_reports(ds: Dataset, case: Case) -> None:
    if case.refused is not None:
        with pytest.raises(case.refused, match=case.match):
            _read(ds, case)
        return
    site, records = _read(ds, case)
    assert site.calls == [INDEX, *(HOST + p for p in case.reports)]  # nothing out of scope was fetched
    con = duckdb.connect()
    staged = load_live(con, ds, records)
    assert staged.staging.row_count == len(records)  # staging keeps every printed cell, one row each
    assert staged.quality.rule_errors == ()  # every contract rule ran on the staged table
    cols = [c.name for c in ds.contract.columns]
    rows = {(r["disease"], r["column_header"]): r for r in (dict(zip(cols, t, strict=True)) for t in con.execute(f"SELECT {', '.join(cols)} FROM wayne_cd_reports").fetchall())}  # fmt: skip
    for key, want in case.want.items():
        got = rows[key]
        assert {k: got[k] for k in want} == want, key
    assert all(r["period_kind"] is not None for r in rows.values())  # every printed header was read
    assert all(r["document_sha256"] and r["document_url"].startswith(HOST) for r in rows.values())


def test_the_entry_is_live_only_and_private(ds: Dataset) -> None:
    assert ds.entry.licence.redistribution is False
    assert ds.entry.source.government_host is True
    assert ds.contract.version and ds.entry.status == "draft"


# ------------------------------------------------------------------------------------------- period_header_parser

RULES = load_registry().get("wayne_cd_reports").entry.source.extensions[1].args


@pytest.mark.parametrize(
    ("header", "year", "want"),
    [
        ("Q1 (1/1-3/31)", "2023", ("quarter", "2023", "1", None)),
        ("Q1", "2023", ("quarter", "2023", "1", None)),
        ("Q1 2024 (1/1-3/31)", None, ("quarter", "2024", "1", None)),
        ("Q2_2025", "2026", ("quarter", "2025", "2", None)),
        ("YTD 2026", None, ("year_to_date", "2026", None, None)),
        ("Q1-Q4 2024 Total", None, ("year", "2024", None, None)),
        ("Totals", "2022", ("year", "2022", None, None)),
        ("2021", None, ("year", "2021", None, None)),
        ("2021- 2025 Total", None, ("multi_year_total", "2021", None, "2025")),
        ("2021-2025 Total", None, ("multi_year_total", "2021", None, "2025")),
        ("5-Year Average", None, ("multi_year_average", None, None, None)),
        (
            "Q5_2025",
            None,
            (None, None, None, None),
        ),  # no rule reads it: left empty, the contract rule says so
        ("Rate per 100,000", None, (None, None, None, None)),
        ("Totals", None, ("year", None, None, None)),  # a title with no year: the year stays empty
    ],
)
def test_period_header_parser_reads_the_catalogs_rules(
    header: str, year: str | None, want: tuple[Any, ...]
) -> None:
    args = php.Args.model_validate(RULES)
    step = php.staging(args, "raw", ["column_header", "report_year", "cell_text"])
    assert step.rows_per_row == 1 and step.fields[-4:] == php.OUTPUTS
    con = duckdb.connect()
    con.execute("CREATE TABLE raw (column_header VARCHAR, report_year VARCHAR, cell_text VARCHAR)")
    con.execute("INSERT INTO raw VALUES (?, ?, '7')", [header, year])
    got = con.execute(f"SELECT {', '.join(php.OUTPUTS)}, cell_text FROM {step.sql}").fetchall()
    assert got == [(*want, "7")]  # every raw field is kept unchanged


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"field": "h", "rules": []}, "at least 1"),
        ({"field": "h", "rules": [{"pattern": "Q(\\d)", "kind": "quarter", "quarter": 2}]}, "names group 2"),
        ({"field": "h", "rules": [{"pattern": "Q", "kind": "quarter", "year_from_field": True}]}, "year_field is named"),
        ({"field": "h", "year_field": "y", "rules": [{"pattern": "(\\d)", "kind": "year", "year": 1, "year_from_field": True}]}, "not both"),
        ({"field": "period_kind", "rules": [{"pattern": "x", "kind": "year"}]}, "can't be named"),
        ({"field": "h", "rules": [{"pattern": "(?<=x)y", "kind": "year"}]}, "linear time"),
        ({"field": "h", "rules": [{"pattern": "x", "kind": "Year Total"}]}, "pattern"),
    ],
)  # fmt: skip
def test_period_header_parser_arguments(args: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        php.Args.model_validate(args)


@pytest.mark.parametrize(
    ("fields", "match"), [(["h", "period_year"], "already have period_year"), (["x"], "no longer have h")]
)
def test_period_header_parser_refuses_a_layout_it_would_misread(fields: list[str], match: str) -> None:
    args = php.Args.model_validate({"field": "h", "rules": [{"pattern": "x", "kind": "year"}]})
    with pytest.raises(ExtensionRefused, match=match):
        php.staging(args, "raw", fields)


def test_period_header_parser_quotes_what_it_is_given() -> None:
    args = php.Args.model_validate({"field": 'h"x', "rules": [{"pattern": "it's", "kind": "year"}]})
    con = duckdb.connect()
    con.execute('CREATE TABLE raw ("h""x" VARCHAR)')
    con.execute("INSERT INTO raw VALUES ('it''s')")
    assert con.execute(f"SELECT period_kind FROM {php.staging(args, 'raw', ['h"x']).sql}").fetchall() == [
        ("year",)
    ]


# ------------------------------------------------------------------------------------------------- printed_facts

FACTS = {"year": r"(?i)Counts for (\d{4})", "area": r"(?i)(excluding (?:the city of )?Detroit|Out-Wayne)"}


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Counts for 2023\nWayne County excluding the City of Detroit", {"year": "2023", "area": "excluding the City of Detroit"}),
        ("COUNTS FOR 2021 and 2022", {"year": "2021", "area": None}),  # the first match, as printed
        ("A report that says neither", {"year": None, "area": None}),  # never guessed
        ("x" * 5000 + " Counts for 2023", {"year": None, "area": None}),  # a line past the bound never matches
    ],
)  # fmt: skip
def test_printed_facts_reads_each_fact_as_printed(text: str, want: dict[str, str | None]) -> None:
    assert pf.read(pf.Args.model_validate({"facts": FACTS}), text.encode(), "https://x.gov/a.pdf") == [want]


@pytest.mark.parametrize(
    ("facts", "match"),
    [({}, "at least 1"), ({"Year": "(\\d)"}, "lower case"), ({"year": "\\d"}, "exactly 1 group")],
)
def test_printed_facts_arguments(facts: dict[str, str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        pf.Args.model_validate({"facts": facts})


# --------------------------------------------------------------------------------------------- groupSeparator


@pytest.mark.parametrize(
    ("sep", "ptype", "ok"), [(",", "DOUBLE", True), (",", "VARCHAR", False), (".", "BIGINT", False)]
)
def test_group_separator_is_a_numeric_columns_thousands_mark(
    tmp_path: Any, sep: str, ptype: str, ok: bool
) -> None:
    import yaml

    from health_data_registry.registry import RegistryError, parse_contract
    from tests.conftest import CONTRACT

    doc = yaml.safe_load(CONTRACT)
    col = doc["schema"][0]["properties"][3]
    col["physicalType"] = ptype
    col["logicalType"] = "number" if ptype == "DOUBLE" else ("integer" if ptype == "BIGINT" else "string")
    col["customProperties"].append({"property": "groupSeparator", "value": sep})
    if ok:
        assert parse_contract(doc, expected_id="toy_cases").columns[3].group_separator == sep
    else:
        with pytest.raises(RegistryError, match="groupSeparator"):
            parse_contract(doc, expected_id="toy_cases")


def test_the_raw_layer_keeps_every_value_as_printed(ds: Dataset) -> None:
    """Hard rule 15: the raw records hold the report's text exactly as printed ("1,001", "*"); printed_facts only
    adds metadata fields beside them, and groupSeparator is applied in staging, never at load."""
    case = Case("raw", {f"{STORE}/quarterly-report-2023.pdf": ANNUAL_2023})
    _site, records = _read(ds, case)
    cells = {(r["Disease"], r["column_header"]): r["cell_text"] for r in records}
    assert cells[("Chlamydia (Genital)", "Q1 (1/1-3/31)")] == "1,001"
    assert cells[("Subtotal", "Totals")] == "5,200"
    printed = {v for _d, _g, vals in ANNUAL_2023.rows for v in vals}
    assert set(cells.values()) == printed  # nothing rewritten, nothing added to a value
    facts = {"report_year", "area_statement", "data_retrieved_on"}
    assert all(facts <= set(r) for r in records)
    assert {r["report_year"] for r in records} == {"2023"}  # the fact as printed: text, not a number
    assert {r["area_statement"] for r in records} == {"excluding Detroit"}
    # Only staging reads the number without its comma.
    con = duckdb.connect()
    load_live(con, ds, records)
    got = con.execute(
        "SELECT cell_text, printed_number FROM wayne_cd_reports WHERE disease = 'Chlamydia (Genital)' "
        "AND column_header = 'Q1 (1/1-3/31)'"
    ).fetchall()
    assert got == [("1,001", 1001.0)]
