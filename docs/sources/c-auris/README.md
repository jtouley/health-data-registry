# C. auris: one condition, three counts

The case study for the spec's standard "one condition, several counts". CDC counts *Candida auris* three
ways. The catalog pulls in all three as separate sources and never merges them, and each draft ODCS contract
here records what its count is. These are drafts: review them before they move into `contracts/`. They are
written against ODCS v3.1.0 and validate against its official schema.

| Source | What one count is | Dated and placed by | Michigan 2024 | US 2024 |
|---|---|---|---|---|
| [CDC Tracking C. auris page](https://www.cdc.gov/candida-auris/tracking-c-auris/index.html) | A patient's clinical case, counted once | Specimen collection date and place | 184 | 6,370 |
| [NNDSS weekly](https://data.cdc.gov/d/x9gk-5huc) `x9gk-5huc`, clinical | A confirmed case notified by the state (CSTE 22-ID-05) | MMWR week the state assigns; reporting jurisdiction | 174 at year end, revised to 184 | 6,382, revised to 7,702 |
| NNDSS weekly, screening | A confirmed colonisation case found by screening | Same | 643, revised to 689 | 3,624, revised to 5,343 (looks wrong) |
| [AR Lab Network](https://arpsp.cdc.gov/psp/listing/arln/profile/c-auris) `mdwz-ar4b` | An isolate tested for antifungal resistance (convenience sample) | Specimen collection year | 160 isolates | 7,779 isolates |

- **The tracking page and NNDSS both count clinical cases, by different rules.** NNDSS is provisional and is
  revised upward. Michigan matching in 2024 is a coincidence; 2025 diverges (NNDSS 178 vs tracking page 248).
- **Screening is colonisation, not infection,** and the tracking page leaves it out.
- **AR Lab Network numbers are isolates,** only meaningful as the denominator for resistance percentages (Michigan
  2024: 97.5% fluconazole-resistant, 6.9% echinocandin-resistant).

Shared keys in every contract's `customProperties`: `countedUnit`, `caseSubtype`, `caseStatus`, `dateBasis`,
`geographyBasis`, `dedupRule`, `sourceSystem`, `revisionPolicy`, `suppressionRule`, and `comparableWith`
(each other source, with `comparable` and a reason).

Unverified as of 24 Nov 2026:

- which system feeds the tracking page;
- whether the AR Lab Network keeps one isolate per patient, and which state an isolate is assigned to (its
  methods documents return 404);
- the meaning of AR Lab Network row `geographyid` 354, and portal totals that disagree;
- final 2024 NNDSS annual values;
- the 2025 NNDSS screening previous-year total.
