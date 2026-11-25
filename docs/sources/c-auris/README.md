# C. auris: one condition, four counts

CDC counts *Candida auris* in several ways, and none of them adds up with another. This folder holds draft
ODCS contracts for the two sources not yet in the catalog. The NNDSS weekly counts are already catalogued, and
their C. auris facts live in [contracts/cdc_nndss_weekly.odcs.yaml](../../../contracts/cdc_nndss_weekly.odcs.yaml).
Review these drafts before they move into `contracts/`. They're written against ODCS v3.1.0.

| Source | What one count is | Dated and placed by | Contract |
|---|---|---|---|
| [CDC Tracking C. auris page](https://www.cdc.gov/candida-auris/tracking-c-auris/index.html) | A patient's clinical case, counted once | Specimen collection date and place | [cdc_cauris_tracking_clinical.yaml](cdc_cauris_tracking_clinical.yaml) |
| [NNDSS weekly](https://data.cdc.gov/d/x9gk-5huc) `x9gk-5huc`, clinical | A confirmed case notified by a jurisdiction (CSTE 22-ID-05, from 2023) | The jurisdiction's MMWR week; the reporting jurisdiction, by usual residence | `contracts/cdc_nndss_weekly.odcs.yaml` |
| NNDSS weekly, screening | A confirmed colonisation case found by screening (notifiable from 2023) | Same | Same |
| [AR Lab Network](https://arpsp.cdc.gov/psp/listing/arln/profile/c-auris) `mdwz-ar4b` | An isolate tested for antifungal resistance (a convenience sample) | Specimen collection year | [cdc_arpsp_arln_cauris.yaml](cdc_arpsp_arln_cauris.yaml) |

The four counts stay separate and labelled. They are never added together:

- **The tracking page and NNDSS both count clinical cases, by different rules.** NNDSS is provisional and is
  revised. In earlier research, Michigan 2024 matched (184 on the tracking page and in NNDSS's restated count),
  but 2025 diverged (NNDSS 178, tracking page 248). Those figures weren't re-checked for this change.
- **Screening is colonisation, not infection.** The tracking page leaves it out.
- **AR Lab Network numbers are isolates.** They're only meaningful as the denominator for resistance
  percentages.

## Settled from primary sources (read 24 Nov 2026)

| Fact | Source |
|---|---|
| NNDSS publishes only confirmed C. auris clinical cases from 2023 (CSTE 22-ID-05). Earlier years published confirmed and probable. | [Notice to Data Users](https://www.cdc.gov/nndss/infectious-disease/notice-to-data-users.html), 2023 annual data |
| C. auris screening became nationally notifiable in 2023, and confirmed cases are published. | Same |
| The 2023 case definition has one classification, Confirmed, for clinical and screening. A patient is counted once as each. | [CSTE 22-ID-05 case definition](https://ndc.services.cdc.gov/case-definitions/candida-auris-2023/) |
| NNDSS assigns a case to the reporting jurisdiction by the country of usual residence. Cases from outside the US and territories go to Non-U.S. Residents. | Notice to Data Users, 2020 annual data; [Readers' Guide](https://www.cdc.gov/nndss/docs/Readers-Guide-WONDER-Tables-20210421-508.pdf) (rev. 12/10/2024), pp. 8 and 10 |
| Licence: public-domain-us-federal. CDC says most of its website material "is in the public domain, and may be freely used or reproduced without obtaining copyright permission". | [CDC Use of Agency Materials](https://www.cdc.gov/other/agencymaterials.html) |

The NNDSS contract keeps the rest as open questions: whether the 2022 weekly clinical rows include probable
cases, how a case with an unknown country of usual residence is assigned, and whether next year's prior-year
count restates this year's. Each draft here lists its own open questions under `openQuestions`.
