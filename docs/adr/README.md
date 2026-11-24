# Architecture decision records

Each file records one decision: its status, context, the decision and its consequences, with the spec section it
comes from. A new decision gets the next number. A changed decision gets a new record that supersedes the old one;
records are not rewritten.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-crawl-outside-claude.md) | The crawl runs on a schedule, outside the chat; the server reads snapshots or fetches from the publisher at run time | Accepted |
| [0002](0002-primary-sources-only.md) | Primary sources only | Accepted |
| [0003](0003-provenance-on-every-result.md) | Provenance on every result: source URL, `retrieved_at`, sha256, dataset version | Accepted |
| [0004](0004-aligned-data-no-gbd-verdicts.md) | Return GBD-aligned data; never a GBD-vs-local verdict; never call or mirror GBD | Accepted |
| [0005](0005-raw-fetches-hash-addressed.md) | Raw fetches are kept, hash-addressed | Accepted |
| [0006](0006-no-llm-extraction.md) | No LLM extraction into stored data | Accepted |
| [0007](0007-odcs-contracts-and-semantic-models.md) | ODCS v3.2.0 contracts and dbt-style semantic models; the contract facts rule | Accepted |
| [0008](0008-governed-sql-and-metrics.md) | Metric-first queries, with governed read-only SQL as the escape hatch | Accepted |
| [0009](0009-licences-decide-mirroring.md) | Licences decide what is mirrored | Accepted |
| [0010](0010-apache-2-licence.md) | Apache-2.0 licence for the code | Proposed |
| [0011](0011-local-stdio-first.md) | Local stdio server first; hosted Hugging Face Space later | Proposed |
