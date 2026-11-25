# Architecture decision records

Each file records one decision: its status, context, the decision and its consequences, with the spec section it
comes from. A new decision gets the next number. A changed decision gets a new record that supersedes the old one;
records are not rewritten.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-crawl-outside-claude.md) | The crawl runs on a schedule, outside the chat; the server reads only snapshots, and restricted sources are fetched onto the user's machine | Accepted |
| [0002](0002-primary-sources-only.md) | Primary sources only | Accepted |
| [0003](0003-provenance-on-every-result.md) | Provenance on every result: source URL, `retrieved_at`, sha256, dataset version | Accepted |
| [0004](0004-aligned-data-no-gbd-verdicts.md) | Return GBD-aligned data; never a GBD-vs-local verdict; never call or mirror GBD | Accepted |
| [0005](0005-raw-fetches-hash-addressed.md) | Raw fetches are kept, hash-addressed | Accepted |
| [0006](0006-no-llm-extraction.md) | No LLM extraction into stored data | Accepted |
| [0007](0007-odcs-contracts-and-semantic-models.md) | ODCS v3.2.0 contracts and dbt-style semantic models; the contract facts rule | Accepted |
| [0008](0008-governed-sql-and-metrics.md) | Named tools over reviewed metrics, with governed read-only SQL as the escape hatch | Accepted |
| [0009](0009-licences-decide-mirroring.md) | Licences decide what is mirrored | Accepted |
| [0010](0010-apache-2-licence.md) | Apache-2.0 licence for the code | Proposed |
| [0011](0011-local-stdio-first.md) | Local stdio server first; hosted Hugging Face Space later | Proposed |
| [0012](0012-built-for-clinicians.md) | Built for clinicians: helpers as MCP prompts, with copy-paste docs | Accepted |
| [0013](0013-browser-fetcher-on-government-hosts.md) | The browser fetcher may read government hosts: honest user agent, 10 s pace, no evasion; a challenge, CAPTCHA, 401, 403 or 429 stops the crawl | Accepted |
| [0014](0014-bibliographic-registries-as-primary-routes.md) | Bibliographic registries (Crossref, OpenAlex, Europe PMC) are primary-source routes for DOI records, not aggregators | Accepted |
| [0016](0016-crawler-factory.md) | Crawler factory: a closed set of generic adapter types, per-source config in the catalog, named pure extensions, one shared fetch core ([audit](../dev/crawler-audit.md)) | Proposed |

There is no 0015 row yet: ADR 0015 (ELT: raw data stored as sent) is proposed on PR #108 and gets its row when that
PR merges.

The spec's current positioning (three layers: crawler and lake, an open health data hub on Hugging Face, and the MCP
server with analysis tools; datasets hosted on the hub wherever the licence allows; restricted sources "listed, not
hosted" and fetched by the server at run time) changes where restricted sources are fetched in 0001 and how the
server is reached in 0011. Those records still describe what ships today; a superseding record is pending.
