# 0019. Covariates that explain infectious-disease rates

- **Status:** Accepted
- **Date:** 2026-11-27
- **Decision:** the maintainer's, 27 Nov 2026, on issue
  [#227](https://github.com/jtouley/health-data-registry/issues/227)
- **Widens:** hard rule 11 in [CLAUDE.md](../../CLAUDE.md#hard-rules) ("Infectious disease only for now ...
  Population denominators (Census) are allowed")
- **Related:** [ADR 0002](0002-primary-sources-only.md) (primary sources only),
  [ADR 0004](0004-aligned-data-no-gbd-verdicts.md) (no GBD verdicts),
  [ADR 0007](0007-odcs-contracts-and-semantic-models.md) (contracts and the contract facts rule),
  [ADR 0009](0009-licences-decide-mirroring.md) (licences decide what is mirrored),
  [ADR 0012](0012-built-for-clinicians.md) (built for clinicians),
  [ADR 0016](0016-crawler-factory.md) (sources are config, not code),
  [ADR 0018](0018-show-everything-qualify-dont-decide.md) (show everything; qualify, don't decide)

## Context

Hard rule 11 allowed two kinds of source: infectious-disease data, and population counts from the Census Bureau
that serve only as the denominators of infectious-disease rates.

Clinician-researchers also ask why a rate differs between places: are Lyme or syphilis rates higher in more
socially vulnerable counties? Answering that needs an area-level covariate beside the rate. The CDC/ATSDR Social
Vulnerability Index (SVI) is the common one: percentile ranks (0 to 1) of US counties on 16 social factors from the
American Community Survey.

Issue #227 asked to add SVI and stopped before building it, because SVI is neither infectious-disease data nor a
denominator. Its one population column is an ACS five-year estimate, which must not compete with the Census July 1
estimates that `get_rate` divides by. The issue records the file, the documentation, the grain and the open licence
question, so the build can follow this decision.

## Decision

Hard rule 11 now allows three kinds of source:

1. **Infectious-disease data,** as before.
2. **Population denominators** (Census), as before: they serve only as the denominators of infectious-disease rates.
3. **Covariates that explain infectious-disease rates,** such as SVI: area-level measures a researcher sets beside
   a rate to stratify or compare it (for example, county Lyme rates by SVI quartile).

The guard on covariates:

- **Only alongside an infectious-disease answer.** A covariate is never a topic of its own. It is never offered as
  a condition, a series or a rate, and `suggest_datasets` offers it only beside an infectious-disease source that
  fits the same question. A question about a covariate alone ("What is this county's SVI?") is told that it
  needs an infectious disease to sit beside. A covariate is read by place, next to an infectious-disease figure:
  through a mart keyed to the same places, or `query_sql`, which can read any loaded table.
- **Never a denominator.** A covariate's own population column (SVI's `E_TOTPOP`) is never used to compute a rate.
  Rates keep the Census denominators.
- **No new disease areas.** Covariates don't open the door to other fields' outcome data (cancer, cardiology,
  mental health). Those stay out until infectious disease passes its evaluation bar.
- **Area-level only.** Hard rule 4 still applies: a covariate describes a place (a county, a state), never a
  person. Record-level files are never joined to anything.

Every other rule applies to a covariate exactly as to any other source: a catalog entry, an ODCS contract that a
maintainer reviews, provenance on every result, the publisher's own primary source on a catalogued HTTPS host, and
its licence checked before anything is mirrored.

## Consequences

- A covariate's catalog entry names its research area `covariate`, not `infectious_disease` or `denominator`, so
  `search_datasets` and `list_sources` can tell the three apart.
- A covariate has no conditions and no named series, so `get_series`, `get_rate`, `compare_rates`, `get_trend`,
  `age_adjust` and `check_signal` never return its values as an answer. The registry refuses a covariate that
  declares a series, a condition, a GBD cause or a disease area.
- A covariate's measures follow the publisher's own rules. SVI's percentile ranks are estimates: they are never
  summed, and an average of them is qualified rather than refused (hard rule 2,
  [ADR 0018](0018-show-everything-qualify-dont-decide.md)), with the publisher's warning that ranks from different
  SVI years are not comparable.
- Each new covariate is reviewed against this guard before it merges: what infectious-disease question does it
  serve, and at what area level?
