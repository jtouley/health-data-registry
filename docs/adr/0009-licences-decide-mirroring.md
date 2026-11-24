# 0009. Licences decide what is mirrored

- **Status:** Accepted
- **Date:** 2026-11-24
- **Spec:** "Hosting and storage"; "Privacy and licences"

## Context

US federal data is public domain. WHO data is under non-commercial terms. GBD has its own non-commercial terms.
State and county reports vary. Mirroring data whose terms limit redistribution would breach them.

## Decision

- Each catalog entry records the source's licence and terms. They come back with every result.
- Sources whose terms allow redistribution, such as US federal data, are snapshotted, and may be published to a
  public Hugging Face dataset.
- Sources whose terms limit redistribution, such as WHO, are not mirrored. They are fetched from the publisher
  onto the user's own machine (with the local server, by `refresh`), never published, and their terms show with
  each result.
- GBD is never fetched at all ([ADR 0004](0004-aligned-data-no-gbd-verdicts.md)).
- Data is never committed to the repo, whatever its licence, except tiny test fixtures.

## Consequences

- Restricted sources are slower and depend on the publisher being up.
- Adding a source means reading its terms first; unclear terms mean run-time fetch, not a mirror.
- Data terms texts go in `licenses/`.
