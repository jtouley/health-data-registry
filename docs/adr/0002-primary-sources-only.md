# 0002. Primary sources only

- **Status:** Accepted
- **Date:** 2026-11-24
- **Spec:** "Privacy and licences"; README tab, proposed ADR 002 and "Sandbox problems seen"
- **Related:** [ADR 0014](0014-bibliographic-registries-as-primary-routes.md): DOI registration agencies and open
  bibliographic indexes a DOI resolves through (Crossref, OpenAlex, Europe PMC) are primary-source routes, not
  aggregators

## Context

The first C. auris draft leaned on news reports. Re-pulling the numbers from CDC NNDSS changed the finding:
Michigan plateaued rather than rose. Aggregators and news re-round, re-label and lag the publisher.

## Decision

Every catalogued source is the publisher of the data: a federal agency, a state or county health department, WHO or
another international agency. News sites, blogs, aggregators and other MCP wrappers are never a source. Each
catalog entry names the publisher and the publisher's own URL and codebook.

## Consequences

- Every number traces to the body that produced it.
- Some data is harder to reach (PDF-only reports, odd portals) and needs a per-source adapter.
- Third-party MCP servers over CDC or WHO are prior art for which datasets to cover, not upstreams.
