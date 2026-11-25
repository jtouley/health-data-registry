# Wayne County communicable disease reports (PDF)

Wayne County Department of Health, Human & Veterans Services publishes case counts of reportable diseases as
PDF reports on its [Statistics & Reports page](https://www.waynecountymi.gov/Government/Departments/Health-Human-Veterans-Services/Public-Health/Statistics-Reports).
The old domain, www.waynecounty.com, is retired. There's no API and no CSV, so
`src/health_data_registry/adapters/wayne_county.py` reads the PDFs, using the geometry parser in
`adapters/pdf_tables.py`.

## What the reports are

| Report | Columns | Years seen |
|---|---|---|
| Annual (Q1–Q4) | `Q1 (1/1-3/31)` … `Q4`, `Totals`; the year is in the title only | 2021–2023 |
| Annual (Q1–Q4), newer layout | `Q1 2024 (1/1-3/31)` … `Q1-Q4 2024 Total` | 2024 |
| Quarterly, rolling 5 quarters | `Q2_2025` … `Q2_2026`, `YTD 2025`, `YTD 2026` | 2025– |
| 5-year | `2021` … `2025`, `2021- 2025 Total`, `5-Year Average` | 2021–2025 |

Every table has a disease group, a disease and one count per column. Some years add subtotal rows per group
and a grand-total line under the table.

## What the reports say about their numbers (contract caveats)

- **Area:** quarterly and annual reports say "Wayne County excluding Detroit" ("Out-Wayne"). The 5-year report
  doesn't say. `excludes_detroit` is `None` there, an open question for the contract, never assumed.
- **Case status:** confirmed plus probable, from the Michigan Disease Surveillance System (MDSS).
- **Provisional:** newer reports print the date the data were pulled ("Retrieved on 07/21/2026"), kept as
  `data_retrieved_on`. The same quarter differs between reports pulled on different dates, so each record
  keeps its report.
- **Suppression:** reports from 2025 on print counts of 1–5 as `*`; the 2021–2024 reports print every count.
  A `*` is kept as suppressed and never filled in.
- **Rows come and go:** a condition with 0 or suppressed counts in each of the previous five quarters is left
  out of the table, so a missing row is not a zero.
- **"YTD" means through the report's quarter:** "YTD 2025" in the Q1 2026 report is Q1 2025 alone.
- **Terms of use:** none stated, and local-government works aren't US federal public domain. Fetch at run
  time and never mirror (ADR 0009).

## How a parse proves itself

The parser builds the table from word positions and cell borders, not from text lines. It refuses rather
than guesses:

- A row of values outside the table's borders or below the grand total, a labelled cell with no values, a blank cell, two values in
  one column, an unknown column header or a header without a year all raise `LayoutError`.
- Every row's quarters must add up to its year-to-date or annual total, and every year to the multi-year
  total and average (rounded half up, as Word and Excel print it). Up to `MAX_FAILED_ROWS` (2) disease rows
  may fail, counted as rows rather than checks; they keep a `failed_checks` note. More than that means a
  misread, and the report is refused (`ReconciliationError`).
- Subtotals are checked against their group's rows, and the grand total against all rows, every time a
  report is read. A disagreement is kept in that total row's `failed_checks`, not raised. Example: the 2022
  report prints Influenza Q3 as 103 in the disease row and 113 in the subtotal, and the grand total agrees
  with 113.
- A disease printed twice in one group is refused: the grain is one row per disease per group.
- The PDF itself is untrusted: over 10 MB or 30 pages, or still parsing after 30 s, it is refused unread, and
  anything the PDF library raises becomes a `LayoutError`.

Results on the nine reports available on 24 Nov 2026 (the files are not in the repo):

| Report | Rows | Result |
|---|---|---|
| 2021, 2022, 2023 annual | 70, 71, 70 | Parsed; 2022 has the publisher discrepancy above |
| 2024 annual | 57 | Parsed |
| Q3 2025, Q4 2025, Q1 2026, Q2 2026 | 70, 68, 69, 39 | Parsed |
| 2021–2025 5-year | 56 | Parsed |

All nine are development files now. An early run silently dropped rows whose label contains a bracket,
such as "Chlamydia (Genital)", and only a subtotal check caught it. The guard against labelled cells
without values is what closes that gap for every layout.

The tests (`tests/test_pdf_reports.py`) never use a real report. `tests/pdf_factory.py` draws random reports
in each layout: repeated or merged group cells, wrapped names, one- or two-line headers, page breaks,
suppression, and a grand-total line. The tests parse each one back and compare. Real reports are checked
only by `make test-network`, once the source is catalogued.

## How it's wired in

- **Catalog:** [`catalog/wayne_cd_reports.yaml`](../../catalog/wayne_cd_reports.yaml), with a draft contract
  ([`contracts/wayne_cd_reports.odcs.yaml`](../../contracts/wayne_cd_reports.odcs.yaml)) and semantic model
  ([`semantic/wayne_cd_reports.yaml`](../../semantic/wayne_cd_reports.yaml)). Portal `pdf_report` dispatches
  to `wayne_county.pages()`.
- **Read at run time, never stored:** `redistribution: false`, so the server reads the reports live, holds
  them in memory and never puts them on the hub.
- **Area as the report states it:** a record from a report that says it excludes Detroit carries `area`
  "Wayne County, Michigan, excluding the City of Detroit (Out-Wayne)". A report that doesn't say (the 5-year
  report) carries an area that says so instead, until a maintainer confirms it (hard rule 1).
- **Each row cites its report:** `report_url`, `report_sha256` (the PDF's bytes) and `report_retrieved_at`.
- **Never added across reports or columns:** `reported_cases` must be read at one report and one column, and
  the compiler refuses anything else. A `*` leaves its group with no total.
- **Scope:** the index page, then only the reports in the layouts checked here: the 2021–2024 annual
  reports, everything under `/files/content/` (2025-on quarterly reports and the 2021–2025 5-year report).
  A PDF with no disease table at all is skipped with a note; if a report in scope doesn't parse, the whole
  source is refused, and the clinician is told the county's report layout couldn't be read and to open the
  reports page instead.
- **Live reads are bounded:** one question reads at most the three reports with the latest year in their
  file names (`live.LIVE_MAX_REPORTS`), within 75 s in all (`live.LIVE_READ_BUDGET_S`); the answer's notes
  name the reports left unread.
- **Access:** the county's site is a government host (`source.government_host`), so under the maintainer's
  access policy it is paced at least 10 s between requests, robots.txt included, by one access policy per
  server process (`crawl.shared_robots`), with an honest user agent. Index and PDFs are requested as HTML and
  PDF, not JSON. On 24 Nov 2026 the county's CDN answered 403 from the build environment; a 401, 403 or 429
  is a block, recorded and never worked around, so check access before review. The government-host
  fallback ladder is issue #36.

## Still open

1. **The 5-year report's area and case status** aren't stated in it. See the contract's `openQuestions`.
2. **Michigan MDHHS reports:** michigan.gov refused automated fetches during research, so their format is
   unverified. `pdf_tables.parse_tables` takes a `TableSpec`, so a second publisher needs its own header
   words and period rules, not a new parser.
