# 0004. Return aligned data; never a GBD-vs-local verdict, never call or mirror GBD

- **Status:** Accepted
- **Date:** 2026-11-24
- **Spec:** "Works alongside other connectors"; "Dataset catalog"; "Hosting and storage"; README tab, "Conclusion",
  GBD interoperability rules and proposed ADR 004

## Context

GBD 2023 gives modelled estimates with 95% uncertainty intervals, at state level at its finest in the US, up to
2023. Observed counts differ for real reasons: syphilis is flat in GBD 2010–2023 while CDC reported the most cases
since 1950; some state splits borrow national patterns. Comparing the two well needs judgement about standards,
rounds, uncertainty and case definitions. Fellows already have the IHME GBD connector, and GBD's terms are
non-commercial.

## Decision

- The server never calls the GBD API and never copies or mirrors GBD data.
- It returns observed rows keyed to GBD location and cause names (a crosswalk per catalog entry) and, through
  `get_aligned_with_gbd`, the exact GBD connector call to make next.
- It never computes or states a "GBD vs local" conclusion. Interoperability rules (no cross-standard
  age-standardised comparisons, counts are not estimates, pathogen fractions don't sum) come back as caveats.

## Consequences

- The user, with Claude, makes the comparison, using both connectors' outputs and the guardrail prompt.
- This server stays small and its licence stays clear.
- The crosswalk (ICD to GBD cause, FIPS to GBD location) is reviewed data, held to the contract facts rule.
