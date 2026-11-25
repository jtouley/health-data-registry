# 0014. Bibliographic registries as primary-source routes

- **Status:** Accepted (the maintainer's decision, 25 Nov 2026)
- **Date:** 2026-11-25
- **Spec:** "Privacy and licences"
- **Related:** [ADR 0002](0002-primary-sources-only.md) (primary sources only), [ADR 0003](0003-provenance-on-every-result.md)
  (provenance on every result), Hard rule 6 in `CLAUDE.md`, PR #29 (the DOI conference harvester)

## Context

Conference abstracts (IDWeek, SHEA Spring, APIC, ICID) are published by journals as supplements, one DOI per
abstract. The harvester reads them from Crossref, OpenAlex and Europe PMC rather than from the journals' own sites.
ADR 0002 says that aggregators are never a source, and the review of PR #29 asked whether these three services count
as aggregators.

They don't re-report anyone's data. Crossref is the DOI registration agency: the publisher deposits the record there
itself, and the DOI resolves through it. OpenAlex (CC0) and Europe PMC index the same deposited records, adding
PubMed ids and the open-access text with its licence. None of them re-rounds, re-labels or summarises the abstract
the way news and aggregator sites do. That was the failure ADR 0002 was written to prevent.

## Decision

DOI registration agencies and the open bibliographic indexes that a DOI resolves through are primary-source routes
for bibliographic records, not aggregators. Today that means Crossref, OpenAlex and Europe PMC. A catalog entry that
reads through them still names the publisher (the journal) and the DOI link of every record. Each such host is
declared on its own source (`source.host`, `source.enrich_hosts`) and is reachable only from that source's fetcher.

News sites, blogs, aggregator sites and other MCP wrappers are still never a source.

## Consequences

- Every row records which API each value came from, with its terms (`metadata_sources`, `metadata_terms`), and the
  provenance of every response it used: source URL, sha256 and `retrieved_at` for Crossref and for each enricher.
- An abstract's text is kept only under the licence of the API that gave the text. A licence is never borrowed from
  another API's answer.
- Any other index or registry needs its own maintainer decision before it is catalogued.
