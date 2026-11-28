---
pretty_name: Toy notifiable condition cases by state and year
license: other
license_name: public-domain-us-federal
license_link: https://www.cdc.gov/other/agencymaterials.html
tags:
- infectious-disease
- respiratory-infections
- sexually-transmitted-infections
- legionellosis
- syphilis
- lower-respiratory-infections
- geography-nation
- geography-state
- country-us
- publisher-cdc
configs:
- config_name: toy_cases
  data_files:
  - split: train
    path: toy_cases/data/toy_cases.parquet
---

# Toy notifiable condition cases by state and year

Centers for Disease Control and Prevention. Hosted copy of the publisher's table, release `2026-11-24` (run 2026-11-24), fetched 2026-11-24 from [https://data.cdc.gov/d/toy0-toy0](https://data.cdc.gov/d/toy0-toy0).

## What one row is

One row per reporting area, condition and year.

## Case definition

- **Legionellosis** (cases): CSTE 2005 (09-ID-04); case status: confirmed and probable
- **Legionellosis** (rate): not yet recorded in the contract; see the publisher's codebook
- **Syphilis** (cases): not yet recorded in the contract; see the publisher's codebook

## Years and geography

Years 2022 to 2024. Geography: nation, state (US).

## Suppression and missing values

Cells the publisher suppressed or marked missing are empty (null) here, exactly as published. They are never filled in, back-calculated or set to zero, so an empty cell is not a zero.

- `cases`: the publisher's codes '-', 'N' read as empty

## Caveats

- Counts for the latest year are provisional.
- This dataset's contract is a draft: no maintainer has reviewed it against the codebook yet.
- Not yet confirmed: Is 'N' a true zero or not notifiable?

## Columns

| Column | What it means | Unit |
|---|---|---|
| `reporting_area` | The state where the case lived, or US RESIDENTS for the national total. |  |
| `year` | MMWR year of report. |  |
| `condition` | Notifiable condition. |  |
| `cases` | Confirmed and probable cases reported that year. |  |
| `rate_per_100k` | Cases per 100,000 residents, as published. |  |

## Licence and how to cite

Licence: public-domain-us-federal ([terms](https://www.cdc.gov/other/agencymaterials.html)); the text is in `LICENSE.md` beside the data.

Cite as: CDC. Toy table. https://data.cdc.gov/d/toy0-toy0

## Files

| File | Rows | sha256 |
|---|---|---|
| `toy_cases/data/toy_cases.parquet` | 12 | `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` |

## Use it

```sql
-- DuckDB, no download step
select * from 'hf://datasets/org/hub/toy_cases/data/toy_cases.parquet' limit 10;
```

Pin this release with `@release-2026-11-24` after the dataset id. Contract `toy_cases` version 0.1.0; the codebook is at https://data.cdc.gov/d/toy0-toy0.

## Add it to a Claude project

- Plugin (coming): the clinician template will ship as a Claude Code plugin from a marketplace; installing it adds this server and its helpers in one step.
- MCP server: add health-data-registry to Claude. It reads this dataset from hf://datasets/org/hub@release-2026-11-24, so nothing needs downloading by you, and every answer names the release it came from.
- By hand: download `toy_cases/data/toy_cases.parquet` from that release and attach it to your Claude project as a file. Claude reads CSV more easily than Parquet; to make one, run in DuckDB: `COPY (SELECT * FROM 'toy_cases.parquet') TO 'toy_cases.csv';`. The file is 0.0 MB. Claude limits the size of each file you attach (about 30 MB per file when this was written; unverified, so check Claude's help pages for the current limit). A file too large to attach is a sign to use the MCP server instead.
