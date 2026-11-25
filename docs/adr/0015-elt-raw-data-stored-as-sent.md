# 0015. ELT, not ETL: raw data is stored as the publisher sent it

- **Status:** Proposed
- **Date:** 2026-11-25
- **Spec:** none; the maintainer's decision of 25 Nov 2026
- **Related:** [ADR 0005](0005-raw-fetches-hash-addressed.md) (raw fetches are kept, hash-addressed),
  [ADR 0007](0007-odcs-contracts-and-semantic-models.md) (contracts and the contract facts rule), Hard rule 15 in
  `CLAUDE.md`, issue [#104](https://github.com/jtouley/health-data-registry/issues/104) (audit of raw values changed
  before storage)

## Context

ADR 0005 keeps the raw bytes of every fetch, so any number can be re-derived. But a load step that rewrites values
on the way in (recodes a missing-value marker, trims a label, casts a count) still leaves a stored dataset that the
publisher never sent. A reader can then no longer tell the publisher's value from our change to it, and a wrong
change is hard to find. The dbt project in `transform/` gives every change a reviewed, tested home: staging types
the load, and intermediate models hold the business rules.

## Decision

We load first and transform later (ELT, not ETL).

- The raw layer (each load's `raw_records/<id>/<snapshot_id>.ndjson`, which dbt reads as its raw source) holds
  publisher data exactly as the publisher sent it. This is a different thing from the raw fetched bytes of
  ADR 0005, which are already kept as sent.
- We may add metadata to it. We never change a value the publisher sent.
- Every change to a value happens later, in staging and the dbt models.
- Metadata we add to a dataset has its type declared up front in the dataset's contract, and is written in that
  type. Otherwise the load step (`crawl.py` and `store.py`) records it in the snapshot's `manifest.json`, and the
  dataset is left unchanged.
- Existing snapshots are never rewritten or migrated.
- A change to what the raw layer holds is a contract change: we bump the contract version and re-pull to a new
  snapshot.
- The raw source contract (`transform/models/staging/_raw_sources.yml`, generated from the ODCS contract by
  `make contracts`) is loose: it holds every field the publisher sends, kept as sent, as text, with no projection
  and no casts.
- Types, casts and key checks happen in staging. Staging takes each column's type from the ODCS contract, as it
  does today, so typing facts keep their reviewed home under hard rule 1.

In the maintainer's words: "Don't break the data, just version and repull. This is a contract change and our raw
schemas should be loose anyways."

## Consequences

- Each stored value can be checked against the publisher's own file, and each change to it is a reviewed dbt
  model with its tests.
- A column we add without a declared type in the contract is a contract error, not a guess at its type.
- Old snapshots stay readable as they were stored; a fix shows up as a new contract version and a new snapshot.
- Load code that changes a publisher's value today moves into staging or a later dbt model. This record changes no
  code by itself; that work is tracked in issue #104 and gets its own pull requests. Until then the rule binds new
  load code and any value a pull request's own change touches; a pull request is not wrong for leaving the code
  listed in #104 untouched.
