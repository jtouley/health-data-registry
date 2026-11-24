# 0010. Apache-2.0 licence for the code

- **Status:** Proposed (the maintainer is still choosing between MIT and Apache-2.0)
- **Date:** 2026-11-24
- **Spec:** "OpenSSF from day one" (Licence row); "Open questions for Jason" (Licence)

## Context

The author's earlier projects, from which some code is adapted, use MIT. ODCS, the contract standard this repo builds
on, uses Apache-2.0. Apache-2.0 adds an explicit patent grant. OpenSSF Scorecard and Best Practices need a
recognised open-source licence at the root either way.

## Decision

The code is licensed Apache-2.0, with `LICENSE` and `NOTICE` at the root and SPDX headers in source files. Data
keeps its publishers' terms, recorded in `licenses/` and in each catalog entry.

## Consequences

- Matches ODCS and gives contributors and users a patent grant.
- Differs from that earlier work; code adapted from it (MIT) keeps its notice (NOTICE).
- If the maintainer chooses MIT instead, this ADR is superseded and `LICENSE`, `NOTICE`, `pyproject.toml` and the
  README badge change together, before the first release.
