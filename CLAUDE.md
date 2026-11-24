# Working in this repo

health-data-registry (a working name; never put "GBD" in the project's name) is a read-only MCP server that answers
clinician-researchers' plain questions about infectious disease with public, population-level data from CDC, WHO
and other publishers. It works next to the IHME GBD and PubMed connectors and returns observed numbers keyed to GBD
names, each with its source, version and method, from reviewed per-dataset contracts.

The users are doctors, not developers. Developer docs are in [docs/dev/](docs/dev/README.md) and
[CONTRIBUTING.md](CONTRIBUTING.md); the README and [docs/setup.md](docs/setup.md) are for clinicians.

## Commands

Run from the repo root. Python 3.12+, managed with [uv](https://docs.astral.sh/uv/). `make help` lists them all.

| Command | What it does |
|---|---|
| `make install` | `uv sync` |
| `make check` | lint, typecheck, test, mutate: what CI runs; run before every push |
| `make test` | Offline tests with coverage (floor 85%) |
| `make test-network` | Live publisher checks in `tests/integration` (marker `network`) |
| `make mutate` | Mutation tests on modules changed since `BASE` (`scripts/mutate_changed.py`, floor 70%) |
| `make docs` | Regenerate `docs/helpers.md` from `helpers.py` |
| `make refresh` / `make serve` | Fetch every source into the lake / run the MCP server on stdio |
| `make check-contracts` | Check each publisher still serves the fields its contract reads |
| `make eval` | Verify the sealed sets; never run or read the held-out pool while developing |
| `uv run health-data-registry refresh <id> [--max-rows N]` | One dataset; `--max-rows` marks the snapshot partial |
| `uv run health-data-registry validate` | Cross-check every catalog entry, contract and semantic model |

## Architecture map

```text
src/health_data_registry/
  cli.py          serve, refresh <dataset>|--all, check-contracts, validate, eval
  server.py       the MCP server (stdio): registers the tools and the helper prompts; wraps every failure
                  as a plain-words refusal
  tools.py        list_sources, search_datasets, describe_dataset, suggest_datasets, get_series,
                  get_aligned_with_gbd, get_rate, get_provenance, list_caveats, find_prior_work
                  ("index not built yet"), query_sql (guarded escape hatch); CSV by default, fmt="json"
  helpers.py      the clinician helpers (MCP prompts); docs/helpers.md is generated from it
  registry.py     loads and cross-checks catalog/, contracts/ (ODCS v3.2.0) and semantic/; strict on purpose
  query.py        the read path: attaches snapshots read-only, compiles semantic-model metrics to SQL,
                  runs guarded SQL, wraps every answer with its source, version, SQL and method
  sql_guard.py    governed read-only SQL: one SELECT, allowlisted functions, declared joins only
  fetch.py        HTTPS-only fetcher to catalogued, publicly routable publisher hosts
  store.py        the lake ($HDR_LAKE, default ./lake): raw/<sha256>, datasets/<id>/<snapshot>/ with
                  data.parquet, data.duckdb and manifest.json, and LATEST
  geo.py          one place whatever the publisher calls it (name, postal code, FIPS; GBD location name)
  evaluation.py   sealed blind sets: seal check, scorer, release gate
  adapters/       socrata.py (CDC portals), who_gho.py (WHO GHO OData)
catalog/          one reviewed YAML per dataset (5 infectious-disease sources; contracts all draft)
contracts/        one ODCS v3.2.0 contract per dataset
semantic/         one dbt-style semantic model per dataset
eval/sealed/      held-out blind sets, hash-sealed; don't read them while developing
tests/            synthetic data only; tests/integration holds the live `network` checks
scripts/          mutate_changed.py, write_helpers_doc.py
docs/             setup.md and helpers.md (clinicians), dev/ (developers), architecture.md, adr/
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
   maintainer reviews them. Until then `list_caveats` reports the contract as a draft.
4. **No patient data.** No upload or local-file tool, ever. Tools read only catalogued public sources. Never join
   record-level public-use files to anything that could re-identify a person; keep publishers' suppression flags.
5. **No data in git.** Snapshots, raw fetches, Parquet and DuckDB files stay out of the repo (`lake/` is ignored).
   Tests use synthetic data and tiny hand-written fixtures only; live checks fetch at test time, write nothing, and
   are marked `network`.
6. **Fetches.** Only to catalogued publisher hosts, only over HTTPS. Primary sources only; news and aggregator
   sites are never a source ([ADR 0002](docs/adr/0002-primary-sources-only.md)). No telemetry.
7. **GBD is never fetched or mirrored.** Fellows reach GBD through the IHME connector. This server returns aligned
   observed data and never a "GBD vs local" verdict ([ADR 0004](docs/adr/0004-aligned-data-no-gbd-verdicts.md)).
8. **Provenance on every result:** source URL, `retrieved_at`, sha256 of the raw bytes, dataset version
   ([ADR 0003](docs/adr/0003-provenance-on-every-result.md)).
9. **No LLM extraction into stored data.** Model output is a suggestion for a reviewer, never a stored fact
   ([ADR 0006](docs/adr/0006-no-llm-extraction.md)).
10. **Licences decide what is mirrored.** Public-domain US federal data may be snapshotted and published;
    restricted terms (e.g. WHO, non-commercial) are never published and their terms show with every result
    ([ADR 0009](docs/adr/0009-licences-decide-mirroring.md)).
11. **Infectious disease only** for now. Don't add datasets from other fields until ID passes its evaluation bar.
12. **Built for clinicians** ([ADR 0012](docs/adr/0012-built-for-clinicians.md)). Tool descriptions, helpers and
    every refusal are in plain clinical words with a next step: never a stack trace, an HTTP code or a bare
    exception. Every step a clinician takes has a helper; change helper text only in `helpers.py`, then
    `make docs` (a test fails if `docs/helpers.md` drifts). The main path in the README never needs a terminal.
13. **Mutation tests on what you change.** `make mutate` is part of `make check` and runs in CI on every PR. Kill
    survivors with a test, or say in the PR why a mutant is equivalent.
14. **Sealed sets stay sealed.** Don't read `eval/sealed/` while developing, and never change prompts, contracts or
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
