# Wayne County communicable disease reports (PDF)

Wayne County Department of Health, Human & Veterans Services publishes case counts of reportable diseases as
PDF reports on its [Statistics & Reports page](https://www.waynecountymi.gov/Government/Departments/Health-Human-Veterans-Services/Public-Health/Statistics-Reports).
The old domain, www.waynecounty.com, is retired. There's no API and no CSV, so the source is read by the
generic `pdf_table` type (ADR 0016, row 9): configuration in
[`catalog/wayne_cd_reports.yaml`](../../catalog/wayne_cd_reports.yaml) and two named extensions, with no code
written for this county.

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
  doesn't say. Each row keeps the report's own words in `area_statement`, empty when the report is silent; what
  each statement means is a draft declaration in the contract (`areaDeclaration`) for the maintainer to review.
- **Case status:** confirmed plus probable, from the Michigan Disease Surveillance System (MDSS).
- **Provisional:** newer reports print the date the data were pulled ("Retrieved on 07/21/2026"), kept as
  `data_retrieved_on`. The same quarter differs between reports pulled on different dates, so each record
  keeps its report.
- **Suppression:** reports from 2025 on print counts of 1–5 as `*`; the 2021–2024 reports print every count.
  A `*` is kept as printed (`cell_text`) and has no number (`printed_number` is empty); it is never filled in.
- **Rows come and go:** a condition with 0 or suppressed counts in each of the previous five quarters is left
  out of the table, so a missing row is not a zero.
- **"YTD" means through the report's quarter:** "YTD 2025" in the Q1 2026 report is Q1 2025 alone.
- **Terms of use:** none stated, and local-government works aren't US federal public domain. Read at run
  time and never mirrored (ADR 0009).

## How it's read (ELT)

| Layer | What happens | Where |
|---|---|---|
| Fetch | The Statistics & Reports page, then each linked PDF whose path is in scope: the 2021–2024 annual reports and the county's current file store (2025-on quarterly, 2021–2025 5-year). Older reports are never fetched | `pdf_table.index_link_pattern`, `scope_paths` |
| Raw | One record per printed cell (`layout: long`): `Group`, `Disease`, `column_header`, `cell_text`, all as printed, plus the document's URL, sha256 and read time | `pdf_table` type |
| Raw metadata | The facts each report prints once: its title year, its area statement, its retrieval date, as printed | `printed_facts` (hook `parse`) |
| Raw metadata | Each printed total checked against its parts (subtotals, the grand-total line, YTD against its quarters); a failed check is kept on the cell as `failed_checks`, and more than two failing rows refuse the report as misread. No value changes | `pdf_table.reconcile` |
| Staging | Each printed header becomes the period it names (`period_kind`, `period_year`, `period_quarter`, `period_year_end`) by ordered rules in the catalog; a header no rule reads stays empty and the contract's `every_header_read` rule reports it | `period_header_parser` (hook `staging`) |
| Staging | `printed_number`: the cell's number, its thousands comma removed (`thousandsSeparator`), `*` as no number | the contract |
| Contract | Total rows by their printed label, the grain, suppression only where `*` is printed, and the reconciliation as quality rules | `contracts/wayne_cd_reports.odcs.yaml` |

The source is live-only (`redistribution: false`): the server reads the newest three reports per question
(`live.max_documents`, newest by the year in the file name), within the hosted deadline, holds them in memory and
never stores or publishes them. Every row cites its report (`document_url`, `document_sha256`,
`document_retrieved_at`). Each cell is returned as printed and never added across reports or columns.

The county's site is a `.gov` host, paced at least 10 s between requests under the maintainer's government-host
policy, with an honest user agent. On 24 Nov 2026 the county's CDN answered 403 from the build environment; a 401,
403 or 429 is a block, recorded and never worked around, so check access before review.

## What the earlier parser found (24 Nov 2026)

Nine reports parsed (the files are not in the repo): the 2021, 2022 and 2023 annual reports (70, 71 and 70
rows), 2024 annual (57), Q3 2025 to Q2 2026 (70, 68, 69 and 39) and the 2021–2025 5-year report (56). The 2022
report prints Influenza Q3 as 103 in the disease row and 113 in the subtotal, and the grand total agrees with
113: a publisher discrepancy, kept as a `failed_checks` note. An early run dropped rows whose label holds a
bracket ("Chlamydia (Genital)"); the guard against labelled cells without values closes that for every layout.

The tests (`tests/adapters/test_wayne_reports.py`) use only synthetic PDFs drawn by `tests/adapters/pdf_fixture.py`
in each layout, read through the real catalog entry and staged through the real contract.

## Still open

1. **The 5-year report's area and case status** aren't stated in it. See the contract's `openQuestions`.
2. **Michigan MDHHS reports:** michigan.gov refused automated fetches during research, so their format is
   unverified. A second publisher needs its own `pdf_table` config and period rules, not a new parser.
3. **Total-row labels** (Total, Totals, Subtotal) are read from the reports, not a codebook: an open question.
4. **PDF subprocess limits** for the shared PDF reader are issue #42.
