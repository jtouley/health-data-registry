# 0005. Raw fetches are kept, hash-addressed

- **Status:** Accepted
- **Date:** 2026-11-24
- **Spec:** "Research landscape index" (crawling rules); "Starting point: what we reuse"; README tab, proposed ADR 005

## Context

Publishers revise and replace files in place. If only parsed tables are kept, a parser bug or a codebook
correction can't be fixed for past snapshots, and a number can't be re-derived.

## Decision

`fetch.py` stores every fetched response as raw bytes, addressed by their sha256, before any parsing. Snapshots and
manifests point at those hashes. Re-normalising a dataset reads the stored bytes, not the publisher.

For sources whose terms forbid redistribution ([ADR 0009](0009-licences-decide-mirroring.md)), the raw bytes stay in
the local store of the machine that fetched them and are never published.

## Consequences

- Any number can be re-derived from the exact bytes it came from.
- Identical fetches are stored once; a changed file is visible as a new hash.
- Raw bytes are data and are never committed to the repo, except tiny test fixtures.
