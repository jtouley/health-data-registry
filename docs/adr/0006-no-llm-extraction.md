# 0006. No LLM extraction into stored data

- **Status:** Accepted
- **Date:** 2026-11-24
- **Spec:** "Semantic layer" (rules carried over from ADR017); README tab, "Testing rule" and proposed ADR 006

## Context

Some sources are PDFs or irregular tables. A language model can pull numbers from them, but its output is
plausible rather than verified, and a wrong count looks like a right one.

## Decision

Parsing is per-source code and configuration, driven by the contract, with a frozen fixture and a golden test per
parser. Model output, whether it drafts a contract, suggests a column meaning or reads a PDF, is a suggestion for a
maintainer to review. It is never written into a snapshot as a fact.

## Consequences

- A publisher layout change fails a golden test loudly instead of silently shifting numbers.
- Adding a PDF source costs more work.
- Models still help draft contracts; the review rule ([ADR 0007](0007-odcs-contracts-and-semantic-models.md)) decides
  what merges.
