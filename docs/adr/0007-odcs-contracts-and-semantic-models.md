# 0007. ODCS v3.2.0 contracts and dbt-style semantic models; the contract facts rule

- **Status:** Accepted; rule 2 below (aggregation) superseded by
  [ADR 0018](0018-show-everything-qualify-dont-decide.md) on 27 Nov 2026
- **Date:** 2026-11-24
- **Spec:** "Semantic layer"

## Context

The user must never be asked which file, which column, which code means missing, which row is the total, or how to
join two tables. Those facts need a home that is reviewable, versioned and standard.

## Decision

Each dataset gets two YAML files, reviewed in pull requests:

- `contracts/<id>.odcs.yaml`, an [ODCS v3.2.0](https://bitol-io.github.io/open-data-contract-standard/latest/)
  contract: schema objects and properties with `logicalType` / `physicalType`, `semanticType`, `enum`, `quality`
  rules (missing codes, ranges, row counts), `relationships`, `authoritativeDefinitions` (the publisher's
  codebook), and `customProperties` for licence, citation and refresh cadence.
- `semantic/<id>.yaml`, a dbt-style semantic model (MetricFlow concepts): entities, dimensions, measures and metrics,
  each metric with its aggregation rule.

Rules carried over from the earlier contract work (ADR017):

1. A fact that changes a number comes only from the publisher's own codebook, an exact structural fact, or a
   maintainer's reviewed declaration. Anything else is an open question in the contract, never a guess.
2. Frequency tables sum their count column and exclude total rows. Estimate and rate tables are never summed.
3. Contracts may be drafted by inference and a model, but merge only after a maintainer reviews them.
4. SNOMED CT is licensed per country; it stays optional and is never bundled.

## Consequences

- Every fact the server relies on is visible in a diff.
- An unresolved open question makes the affected metric decline rather than guess.
- Shared dimensions (ICD, GBD causes, ISO 3166 and FIPS, UCUM) make "Michigan", "MI" and FIPS 26 one place.
- Maintainer review is a bottleneck by design.
