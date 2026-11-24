# 0003. Provenance on every result

- **Status:** Accepted
- **Date:** 2026-11-24
- **Spec:** "MCP tools"; "Correctness and evaluation" ("Every answer is checkable"); README tab, proposed ADR 003

## Context

A clinician-researcher must be able to cite a number and a reviewer must be able to re-derive it. A number without
its source and date can't be checked or cited.

## Decision

Every tool result carries:

- the source URL at the publisher;
- `retrieved_at`, when the raw bytes were fetched;
- the sha256 of those raw bytes ([ADR 0005](0005-raw-fetches-hash-addressed.md));
- the dataset version (snapshot id) and the contract and semantic model versions;
- the SQL that ran, a plain-language method line, the licence terms and how to cite the data.

No row enters a snapshot without its source URL and `retrieved_at`.

## Consequences

- Answers are longer, but every number is checkable.
- The guardrail prompt can require Claude to cite tool output, never memory.
- Snapshot manifests must store provenance per raw fetch, not per dataset.
