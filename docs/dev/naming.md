# Hugging Face repository names

Every Hugging Face repository of this project starts `<owner>/health-data-registry`, so one prefix finds all
of them, and a token's repository list can be checked at a glance. `<owner>` is `jtouley`, or
`HDR_PRIVATE_HF_OWNER` where a workflow or the hosted server sets it.

| Repository | Name | Visibility | Read by | Written by |
|---|---|---|---|---|
| Public dataset (open data releases) | `health-data-registry` | Public | Anyone; the hosted server | `refresh.yml` publish |
| Space (hosted MCP server) | `health-data-registry-mcp` | Public | Clinicians' MCP clients | `deploy-space.yml` |
| Private tier, one per source | `health-data-registry-private-<source-id>` | Private | The hosted server (`private.py`) | The source's crawl workflow |
| Operational state, one per job | `health-data-registry-ops-<job>` | Private | The job that owns it | The job that owns it |

Rules:

- Lowercase letters, digits and hyphens only. No abbreviations: never `hdr-`.
- `<source-id>` is the catalog id with `_` changed to `-`: `croi_abstracts_index` becomes
  `croi-abstracts-index`. One private dataset per source, so one source can be deleted on its own.
- `<job>` names the workflow job: `croi-crawl`, `raw-archive`.
- `ops` datasets hold checkpoints, fetch logs and raw bytes. They are never a data product and never public.
- A private or ops dataset is never made public. Its dataset card says so and links here.

## Where the names are built

[`src/health_data_registry/naming.py`](../../src/health_data_registry/naming.py) builds every name. Code calls
`naming.private_dataset(source_id)` or `naming.ops_dataset(job)`; workflows call

```bash
uv run python -m health_data_registry.naming --owner "$OWNER" private croi_abstracts_index
uv run python -m health_data_registry.naming --owner "$OWNER" ops croi-crawl
```

No other file spells a name out. `refresh.yml` accepts `HDR_RAW_DATASET` only when it equals the `raw-archive`
name, so a variable can't point the archive at another repository. A new private source or job gets its name
from `naming.py`; it is created by the maintainer, and its token entry added, before its first run.

## The repositories today

| Repository | Holds |
|---|---|
| `jtouley/health-data-registry` | Public releases of the public-domain sources |
| `jtouley/health-data-registry-mcp` | The Space |
| `jtouley/health-data-registry-private-croi-abstracts-index` | CROI abstract metadata rows (no text) |
| `jtouley/health-data-registry-private-croi-abstracts-text` | CROI abstract text. CROI Foundation's, never redistributed |
| `jtouley/health-data-registry-ops-croi-crawl` | CROI crawl checkpoint, fetch log and rows so far |
| `jtouley/health-data-registry-ops-raw-archive` | Raw publisher bytes and fetch logs; created only when the raw archive is turned on |

The three CROI datasets were created private on 24 Nov 2026, each holding only a dataset card.

## Tokens list exactly these repositories

Each Hugging Face token is fine-grained and names its repositories one by one; none has access to every
repository of the account.

| Token | Repositories | Access |
|---|---|---|
| `HF_PUBLISH_TOKEN` | `health-data-registry` | Write |
| `HF_SPACE_TOKEN` | `health-data-registry-mcp` | Write |
| `HF_CRAWL_TOKEN` | `health-data-registry-private-croi-abstracts-index`, `health-data-registry-private-croi-abstracts-text`, `health-data-registry-ops-croi-crawl`, and `health-data-registry-ops-raw-archive` if the archive is on | Read and write |
| `HDR_PRIVATE_HF_TOKEN` (Space secret) | `health-data-registry-private-croi-abstracts-index`, `health-data-registry-private-croi-abstracts-text` | Read |

When a repository is added or deleted, change its token's list in the same pull request that changes this page.
Secrets and variables for the workflows are in [automation.md](automation.md).
