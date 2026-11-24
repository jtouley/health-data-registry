# Working in this repo

health-data-registry is a read-only MCP server that answers clinician-researchers' plain questions with public,
population-level health data (CDC, WHO and other publishers). It works next to the IHME GBD connector: GBD gives
modelled burden, this server gives observed and local numbers keyed to GBD names. Every number it returns comes
from a reviewed contract and carries its source, version and method.

## Commands

Run from the repo root. Python 3.12+, managed with [uv](https://docs.astral.sh/uv/).

| Command | What it does |
|---|---|
| `make install` | `uv sync` with the dev group |
| `make check` | Lint, type checks and tests; run before every push |
| `make test` | Tests only |
| `make serve` | Start the MCP server over stdio |
| `make refresh` | Fetch and snapshot catalogued datasets |
| `make eval` | Run the development question pool (never the sealed held-out pool by hand) |
| `uv run health-data-registry serve` | The server, as `uvx` runs it |
| `uv run health-data-registry refresh <dataset>` / `--all` | Fetch one or all datasets into a new snapshot |
| `uv run health-data-registry check-contracts` | Check every contract against ODCS v3.2.0 and the repo's rules |
| `uv run health-data-registry validate` | Check catalog, contracts and semantic models agree with each other |

## Architecture map

```text
src/health_data_registry/
  cli.py          commands: serve, refresh <dataset>|--all, check-contracts, validate
  server.py       the MCP server (stdio); registers the tools, nothing else
  tools.py        find_datasets, describe_dataset, suggest_datasets, query_metric, query_sql (Phase 2);
                  get_aligned_with_gbd and find_prior_work come later
  catalog.py      loads catalog/<id>.yaml; ranks datasets by research area, geography, years, shape
  contracts.py    loads contracts/<id>.odcs.yaml (ODCS v3.2.0): types, codes, missing values, total rows
  semantic.py     loads semantic/<id>.yaml; compiles a metric + filters + group-by into SQL with its
                  aggregation rule
  sql_guard.py    governed read-only SQL: refuses writes, unbounded scans and undeclared joins
  fetch.py        HTTPS-only fetcher to catalogued publisher hosts; keeps raw bytes hash-addressed
  store.py        Parquet snapshots + manifest per dataset version; DuckDB read-only views
  adapters/
    socrata.py    CDC and other Socrata portals, with column metadata
    who_gho.py    WHO Global Health Observatory (OData); fetched at run time, never mirrored
catalog/          one reviewed YAML per dataset
contracts/        one ODCS v3.2.0 contract per dataset
semantic/         one dbt-style semantic model per dataset
eval/questions    development question pool
eval/sealed       held-out blind sets, hash-sealed; don't read them while developing
docs/adr/         decisions; docs/architecture.md: the data flow
```

Data flow: adapter fetch → raw bytes, hash-addressed → contract-driven normalise → Parquet snapshot + manifest →
DuckDB read-only views → tools. See [docs/architecture.md](docs/architecture.md).

## Hard rules

These come from the spec and [docs/adr/](docs/adr/README.md). A PR that breaks one is wrong, whatever its evidence.

1. **Facts that change a number** ([ADR 0007](docs/adr/0007-odcs-contracts-and-semantic-models.md), carried over
   from ADR017). A fact that changes a number comes only from the publisher's own codebook, an exact structural
   fact, or a maintainer's reviewed declaration. Anything else is an open question in the contract, never a guess.
2. **Aggregation.** Frequency tables sum their count column and exclude total rows. Rates and estimates are never
   summed. Published uncertainty ranges are returned, never dropped.
3. **Review.** Contracts and semantic models may be drafted by inference or a model, but merge only after a
   maintainer reviews them.
4. **No patient data.** No upload or local-file tool, ever. Tools read only catalogued public sources. Never join
   record-level public-use files to anything that could re-identify a person; keep publishers' suppression flags.
5. **No data in git.** Snapshots, raw fetches, Parquet and DuckDB files stay out of the repo (`lake/` is ignored).
   The only exception is tiny test fixtures.
6. **Fetches.** Only to catalogued publisher hosts, only over HTTPS. Primary sources only; news and aggregator
   sites are never a source ([ADR 0002](docs/adr/0002-primary-sources-only.md)). No telemetry.
7. **GBD is never fetched or mirrored.** Fellows reach GBD through the IHME connector. This server returns aligned
   observed data and never a "GBD vs local" verdict ([ADR 0004](docs/adr/0004-aligned-data-no-gbd-verdicts.md)).
8. **Provenance on every result:** source URL, `retrieved_at`, sha256 of the raw bytes, dataset version
   ([ADR 0003](docs/adr/0003-provenance-on-every-result.md)).
9. **No LLM extraction into stored data.** Model output is a suggestion for a reviewer, never a stored fact
   ([ADR 0006](docs/adr/0006-no-llm-extraction.md)).
10. **Licences decide what is mirrored.** Public-domain US federal data may be snapshotted; restricted terms
    (e.g. WHO, non-commercial) are fetched at run time and their terms shown with every result
    ([ADR 0009](docs/adr/0009-licences-decide-mirroring.md)).
11. **Sealed sets stay sealed.** Don't read `eval/sealed/` while developing, and never change prompts, contracts or
    metrics because of a held-out result.

## Skills and how to use them

This repo uses the outside skills that earned their place on registry.coffee
([Outside skills we've used](https://claude.ai/code/artifact/bb6ca1eb-0207-44d6-ac03-23141120ea9e)), each for the
work below. Skills rated 2 or lower there are left out.

Rules for every skill:

- Read a skill's files before first use, as third-party code. Use it as review criteria only: never run its
  scripts, installers or agent workflows, and skip any skill that needs an API key or a network install.
- Builders apply the matching skills while writing, not only at review. Every PR names the skills it used and what
  each changed.
- Review stays one round: an opus subagent whose prompt is only `/code-review PR #<n>`, posted to the PR, then one
  fix round by a fresh subagent with the relevant skills.
- Skills live in each session's `~/.claude/skills`, not in the repo; a new session installs them from the sources
  below.

| Work | Skill (source) | When to use it |
|---|---|---|
| README and docs | good-readme ([adewale/good-readme](https://github.com/adewale/good-readme)) | Every change to the README, design docs, runbooks, SECURITY, CONTRIBUTING or CHANGELOG; checks that docs match the code. Rated 4.5 |
| README and docs | better-writing ([jakubkrehel/skills](https://github.com/jakubkrehel/skills)) | Wording of docs, tool descriptions and every user-facing answer template. Rated 4 |
| README and docs | readme-roast ([hidai25/readme-roast](https://github.com/hidai25/readme-roast)) | README structure and install steps only; ignore its growth-hacking advice. Rated 3.5 |
| Contracts and models | using-dbt-for-analytics-engineering ([dbt-labs/dbt-agent-skills](https://github.com/dbt-labs/dbt-agent-skills)) | Every dbt model and semantic-model change; run its column-level impact check. Rated 4.5 |
| Contracts and models | adding-dbt-unit-test (dbt Labs, [dbt-labs/dbt-agent-skills](https://github.com/dbt-labs/dbt-agent-skills)) | Unit tests for metric logic: totals never summed, rates never summed, snapshot changes. Rated 4.5 |
| Contracts and models | maintaining-dbt-documentation (dbt Labs, [dbt-labs/dbt-agent-skills](https://github.com/dbt-labs/dbt-agent-skills)) | Describe every column in clinical words, never restate its name. Rated 2.5; docs only |
| Contracts and models | dbt-transformation-patterns ([wshobson/agents](https://github.com/wshobson/agents)) | Layering: sources → staging → marts → metrics. Rated 3.5 |
| Pipeline | build-data-pipeline, model-data, load-data ([motherduckdb/agent-skills](https://github.com/motherduckdb/agent-skills)) | Adapters, snapshot loading into Parquet, DuckDB modelling, constraints. Rated 4 |
| Data quality | gx-configure-expectations (Great Expectations, shipped in the package) | Contract quality rules: only assert what the codebook states; an all-null column passes value checks, so add fill-rate checks; keep a metric error apart from a data failure. Rated 4 |
| Data quality | SodaCL docs (no public skill) | Freshness and reconciliation rules per source. Rated 3 |
| Security | sharp-edges ([trailofbits/skills](https://github.com/trailofbits/skills)) | Every change to fetchers, the SQL sandbox, workflows, tokens or the hosted server. Rated 4.5 |
| Hosting | cloud-run-basics, google-cloud-waf-security, cloud-run-alert-configuration ([google/skills](https://github.com/google/skills)) | Only if the server moves to the Cloud Run fallback: pinned image digests, one service account per job, job-failure alerts. Rated 4.5 |
| Hosting | terraform-skill ([antonbabenko/terraform-skill](https://github.com/antonbabenko/terraform-skill)) | Only if infrastructure moves to Terraform: reviewed plan files, drift, blast radius. Rated 4 |
| Catalog site, later | Owl-Listener designer-skills, Krehel UI set, no-ai-design-slop | Only when a public catalog page is built. Rated 4–4.5 |

Left out: DuckDB's duckdb-skills (rated 1; DuckDB's own docs are more useful), wshobson data-quality-frameworks
(rated 2; reverted on registry.coffee), and HashiCorp terraform-style-guide (rated 1.5).

Semantic models here are dbt-style YAML (MetricFlow concepts) without a dbt project, so the dbt skills apply to
`semantic/` and the metric compiler in `semantic.py`.
