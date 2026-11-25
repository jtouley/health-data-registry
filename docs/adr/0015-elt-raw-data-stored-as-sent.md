# 0015. ELT, not ETL: raw data is stored as the publisher sent it

- **Status:** Proposed
- **Date:** 2026-11-25
- **Spec:** none; the maintainer's decision of 25 Nov 2026
- **Related:** [ADR 0005](0005-raw-fetches-hash-addressed.md) (raw fetches are kept, hash-addressed),
  [ADR 0007](0007-odcs-contracts-and-semantic-models.md) (contracts and the contract facts rule), Hard rule 15 in
  `CLAUDE.md`

## Context

ADR 0005 keeps the raw bytes of every fetch, so any number can be re-derived. But a load step that rewrites values
on the way in (recodes a missing-value marker, trims a label, casts a count) still leaves a stored dataset that the
publisher never sent. A reader can then no longer tell the publisher's value from our change to it, and a wrong
change is hard to find. The dbt project in `transform/` gives every change a reviewed, tested home: staging types
the load, and intermediate models hold the business rules.

## Decision

We load first and transform later (ELT, not ETL).

- Raw data from a publisher is stored exactly as the publisher sent it.
- We may add metadata to it. We never change a value the publisher sent.
- Every change to a value happens later, in staging and the dbt models.
- Metadata we add to a dataset has its type declared up front in the dataset's contract, and is written in that
  type. Otherwise the ingestion service records it in the snapshot manifest, and the dataset is left unchanged.

## Consequences

- Each stored value can be checked against the publisher's own file, and each change to it is a reviewed dbt
  model with its tests.
- A column we add without a declared type in the contract is a contract error, not a guess at its type.
- Load code that changes a publisher's value today moves into staging or a later dbt model. This record changes no
  code by itself; that work gets its own pull requests.
