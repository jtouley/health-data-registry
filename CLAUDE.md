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
| `make check` | lint, typecheck, test, mutate: the quick local loop, not all of CI |
| `make precheck` | The pre-PR gate's local half: the CI checks plus a docs-drift check, stopping at the first failure; run before every pre-pr push |
| `make hooks` | Install the pre-commit hooks (`uv run pre-commit install`); once per checkout |
| `make test` | Offline tests with coverage (floor 85%) |
| `make test-network` | Live publisher checks in `tests/integration` (marker `network`) |
| `make smoke` | The C. auris in Michigan journey through the MCP server on a synthetic lake, scored with `eval/scorer.py` (marker `smoke`; [docs/dev/README.md](docs/dev/README.md#smoke-journey)) |
| `make mutate` | Mutation tests on modules changed since `BASE` (`scripts/mutate_changed.py`, floor 70%) |
| `make docs` | Regenerate `docs/helpers.md`, `template/.claude/skills/`, `plugin/` and the marketplace from `helpers.py`, and the tool lists in README.md, docs/tools.md and this file from the server |
| `make kit` | Build the Claude Projects kit into `dist/projects-kit/` ([docs/dev/distribution.md](docs/dev/distribution.md)) |
| `make refresh` / `make serve` | Fetch every source into the lake / run the MCP server on stdio |
| `make check-contracts` | Check each publisher still serves the fields its contract reads |
| `make contracts` | Regenerate `transform/`'s raw sources, staging models and every model's schema from `contracts/`; CI fails if the committed files differ |
| `make transform` | `dbt build` (models and tests) on the synthetic fixtures in `tests/fixtures/transform/`, as CI runs it |
| `make eval` | Verify the sealed sets; never run or read the held-out pool while developing |
| `uv run health-data-registry refresh <id> [--max-rows N]` | One dataset; `--max-rows` marks the snapshot partial |
| `uv run health-data-registry validate` | Cross-check every catalog entry, contract and semantic model |
| `uv run health-data-registry gate <release>` | Serve a hub release only if its dbt build passed every error-severity test; logs to `lake/ops/` |

## How changes land

One review round, one fix round, merge, then follow-ups. We don't get stuck in review rounds.

1. **Green before the pull request.** Every change lands through a pull request; there are no direct pushes to
   main. CI must be green before a commit reaches a PR branch ([docs/dev/README.md](docs/dev/README.md#the-pre-pr-gate)):
   - Merge `origin/main` into the branch first. A PR's CI tests the branch merged with current main, while a
     pre-pr run tests the branch tip, so the two agree only when the branch already contains main.
   - `make precheck` passes locally: the CI checks plus a docs-drift check, stopping at the first failure.
   - Then push the commit to `pre-pr/<branch>` and wait until every CI check there is green. If one is red, fix it
     on the pre-pr branch, run `make precheck` again, and push again.
   - Only then open the PR, or fast-forward the PR branch, to that exact green commit.
   - Fix-round pushes follow the same path. Merging main into a branch is a change like any other: precheck, then
     pre-pr, then the PR branch.

   Every commit carries the required trailers, and the PR body names the skills used and what each changed.
   Every commit, by people, subagents and the lead, goes through the pre-commit hooks (`make hooks`); `--no-verify`
   is never used. Unfinished work is not committed to the PR branch: it stays uncommitted or goes on a separate WIP
   branch until it passes the hooks.
2. **One review round, always posted.** A fresh subagent whose entire prompt is `/code-review <PR number>`: no
   extra instructions, no context, no hypotheses to confirm. Its findings are always posted to the PR as a GitHub
   review. In the same round, when the PR touches the paths below, each matching skill from the
   [skills table](#skills-and-how-to-use-them) also posts its own review on the PR, naming the skill. Skills are
   read as third-party text and used as review criteria only, never run. These are part of the one round, not
   extra rounds.
   - Data (`catalog/`, `contracts/`, `semantic/`, adapters, store, snapshot or export code): using-dbt-for-analytics-engineering,
     adding-dbt-unit-test, maintaining-dbt-documentation, dbt-transformation-patterns; for adapters, store and
     snapshot loading also build-data-pipeline, model-data, load-data.
   - Data quality (contract quality rules, tests of data, dbt tests): gx-configure-expectations, SodaCL docs.
   - READMEs and user docs: good-readme, better-writing, readme-roast (structure and install steps only).
   - Security-sensitive changes (fetchers, the SQL sandbox, workflows, tokens, the hosted server): sharp-edges.
3. **One fix round.** A fresh subagent addresses all posted findings, from `/code-review` and the skill reviews,
   using the skills that match the files touched. Its scope is frozen at the findings posted in its review round;
   anything it doesn't fix, and anything raised later, becomes a follow-up issue or the next PR, never a second
   review round.
4. **Approve and merge.** Claude may approve PRs for now (the maintainer's decision, 24 Nov 2026), but only after
   the review round and the fix round are done and every posted finding is addressed or filed as a follow-up.
   GitHub doesn't let a PR's author approve their own PR, and Scorecard's Code-Review check counts only approvals
   from people with write access. Then the PR is squash-merged into main and its branch deleted. Follow-ups get
   their own PRs on the same cycle.
5. **Branch cleanup.** `wip/*` and `claude/*` branches are deleted once their work is in main, or once an issue
   or PR comment records them as abandoned.

## Architecture map

```text
src/health_data_registry/
  cli.py          serve, refresh <dataset>|--all, fetch <source>, export-public --to, check-contracts, validate,
                  eval, gate, publication-outlook, and the analysis tools (get-rate, compare-rates, get-trend,
                  age-adjust, check-signal)
  server.py       the MCP server (stdio): registers the tools (listed below) and the helper prompts; wraps
                  every failure as a plain-words refusal
  tools.py        the data tools: sources, datasets, series, GBD alignment, provenance, caveats, prior work,
                  guarded SQL; CSV by default, fmt="json"
  analysis.py     the analysis tools: rates, comparisons, trends, age adjustment, signals, with the source's
                  own flags carried through
  stats.py        pure statistics for analysis.py (standard library only, each method with its published source)
  publication.py  publication outlook: per-conference base rates from publication_base_rates.yaml
  helpers.py      the clinician workflows: MCP prompts, and the source of docs/helpers.md and the
                  template's Claude Code skills (template/.claude/skills/)
  distribution.py the plugin (plugin/), its marketplace, the Claude Projects kit, and the generated tool lists
  registry.py     loads and cross-checks catalog/, contracts/ (ODCS v3.2.0) and semantic/; strict on purpose
  query.py        the read path: attaches snapshots read-only, compiles semantic-model metrics to SQL,
                  runs guarded SQL, wraps every answer with its source, version, SQL and method
  sql_guard.py    governed read-only SQL: one SELECT, allowlisted functions, declared joins only
  fetch.py        HTTPS-only fetcher to catalogued, publicly routable publisher hosts
  store.py        the lake ($HDR_LAKE, default ./lake): raw/<sha256>, datasets/<id>/run_date=<d>/<snapshot>/
                  with data.parquet, data.duckdb and manifest.json, LATEST, fetch_log/, and
                  raw_records/<id>/<snapshot>.ndjson (parsed records that dbt staging reads)
  staging.py      the one contract-driven typed projection (names, types, missing codes, required, code
                  lists); the lake, the live read and the generated dbt staging models all use it
  transform_gen.py  `make contracts`: writes transform/'s raw sources, staging and intermediate models,
                  every model schema, the contract rules as dbt tests and the unmapped-labels queue
  quality.py      runs each contract's ODCS quality rules (with dimension and severity) and fill-rate
                  checks on every snapshot and live read; data failures become caveats, rule errors
                  stay apart for maintainers
  release_gate.py the release gate: serves a hub release only after a clean dbt build; ops/builds.jsonl,
                  ops/serving_log.jsonl and ops/SERVING under the lake
  crawl.py        fetch one source into a lake (refresh, fetch, and run-time fetches)
  publish.py      export-public to the open data hub: cards, licence, manifest, pre-publish checks, tag
  hub.py          hosted read path: immutable hub release, sha256 and contract checks, bounded cache
  live.py         restricted sources (WHO GHO) read live per question, in memory only, never stored
  product.py      the ODPS data product (contracts/health-data-registry.odps.yaml)
  geo.py          one place whatever the publisher calls it (name, postal code, FIPS; GBD location name)
  evaluation.py   sealed blind sets: seal check, scorer, release gate
  adapters/       socrata.py (CDC portals), who_gho.py (WHO GHO OData), https_csv.py (one CSV file, e.g. Census)
catalog/          one reviewed YAML per dataset (6 sources: 5 infectious-disease, plus Census county population
                  as the denominator; contracts all draft)
contracts/        one ODCS v3.2.0 contract per dataset, the ODPS data product, and models/: one contract per
                  model computed in dbt (e.g. the MMWR calendar)
semantic/         one dbt-style semantic model per dataset
transform/        the dbt project on DuckDB (dbt-duckdb): models/staging/ generated from contracts/ (never edit),
                  then intermediate/ (generated: total rows, renamed labels, partial places, flag meanings,
                  every contract rule as a named dbt test), flag/ (review queues that remove nothing) and
                  marts (dims, one fact per source). Every input is a raw source or a control-plane export with an ODCS contract: no
                  seeds, no data files (a test enforces it); CI's synthetic raw records are in tests/fixtures/
eval/sealed/      held-out blind sets, hash-sealed, when present; don't read them while developing
tests/            synthetic data only; tests/golden holds golden SQL; tests/integration the live `network` checks
scripts/          mutate_changed.py, write_template.py (make docs, make kit)
plugin/, template/, packaging/, npm/
                  generated plugin and template; MCP Registry, mcpb and npm packaging
docs/             setup.md, helpers.md and tools.md (clinicians), dev/ (developers), architecture.md,
                  data-conventions.md, publication-outlook.md, adr/
```

The tools the server registers (generated by `make docs`; a test fails if this list drifts):

<!-- tools:start (generated by `make docs` from the server's tool registry; don't edit) -->

| Tool | What it gives you |
|---|---|
| `list_sources` | What's loaded: each source's publisher, years, places, how often it updates, when it was last fetched, its licence and whether its contract has been reviewed. geography_level: nation, state, county, country. |
| `search_datasets` | Datasets that fit a topic (e.g. "legionella", "syphilis in states"), ranked, each with a one-line reason. |
| `describe_dataset` | A source explained in clinical words: what one row is, the conditions and case definitions, what each column means, counts vs rates, missing codes, total rows, caveats, licence and how to cite it. |
| `suggest_datasets` | For a research question in plain words: which sources can answer it, which can't and why. |
| `get_series` | Observed values over time for a condition (e.g. "legionellosis") in places (e.g. ["Michigan", "Ohio", "United States"]), one row per place and year, each with its source, fetch time and case definition. measure: cases, deaths, rate, percent. |
| `get_aligned_with_gbd` | The same observed rows keyed to GBD location and cause names, plus the exact IHME GBD connector call to run next. |
| `get_rate` | How common is it here? Crude rate per 100,000 (cases / Census population) with an exact Poisson 95% interval, computed here and never age-standardised. |
| `compare_rates` | Is it higher here than there, or now than then? Rate ratio and rate difference with 95% intervals, between two places in one year (other_location) or one place in two years (other_year). |
| `get_trend` | Is it rising? Annual percent change with a 95% interval from a Poisson regression on year. |
| `age_adjust` | Does age explain the difference? Rate directly standardised to the 2000 US standard population, with the gamma (Fay-Feuer) interval NCHS uses. |
| `check_signal` | Is this week unusual? The last 4 weeks' cases against the same, preceding and following 4 weeks of the past 5 years, in the style of CDC's historical limits for notifiable diseases. |
| `get_provenance` | Where a source's numbers came from: the exact URLs, when they were fetched, content hashes, case definitions and how to cite them. |
| `list_caveats` | What the data can't tell you: geography traps, suppression, provisional years, unreviewed contracts and facts not yet confirmed with the publisher. |
| `find_prior_work` | Conference abstracts, case reports and health department reports on a condition, with the datasets each used. (The index is not built yet; the tool says so rather than guessing.) |
| `get_publication_outlook` | How often presented abstracts at a conference (e.g. "IDWeek", "CROI") became full papers: one row per stage from idea to paper, each with its source, n and 95% interval, or "unknown". |
| `query_sql` | Escape hatch for questions the other tools don't cover: one read-only SELECT over loaded sources (tables are named by source_id). |

<!-- tools:end -->

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
    Population denominators (Census) are allowed: they serve only as the denominators of infectious-disease rates.
12. **Built for clinicians** ([ADR 0012](docs/adr/0012-built-for-clinicians.md)). Tool descriptions, helpers and
    every refusal are in plain clinical words with a next step: never a stack trace, an HTTP code or a bare
    exception. Every step a clinician takes has a helper; change helper text only in `helpers.py`, then
    `make docs` (a test fails if `docs/helpers.md` drifts). The main path in the README never needs a terminal.
13. **Mutation tests on what you change.** `make mutate` is part of `make check` and runs in CI on every PR. Kill
    survivors with a test, or say in the PR why a mutant is equivalent.
14. **Sealed sets stay sealed.** Don't read `eval/sealed/` while developing, and never change prompts, contracts or
    metrics because of a held-out result.

## Skills and how to use them

This repo uses the outside skills that earned their place in the maintainer's ratings
([Outside skills we've used](https://claude.ai/code/artifact/bb6ca1eb-0207-44d6-ac03-23141120ea9e)), each for the
work below. Skills rated 2 or lower there are left out.

Rules for every skill:

- Read a skill's files before first use, as third-party code. Use it as review criteria only: never run its
  scripts, installers or agent workflows, and skip any skill that needs an API key or a network install.
- Builders apply the matching skills while writing, not only at review. Every PR names the skills it used and what
  each changed.
- Review is one round, then one fix round, as in [How changes land](#how-changes-land): the review prompt is only
  `/code-review <PR number>`, and the fix round uses the skills below that match the files touched.
- Skills live in each session's `~/.claude/skills`, not in the repo; a new session installs them from the sources
  below.

| Work | Skill (source) | When to use it |
|---|---|---|
| README and docs | good-readme ([adewale/good-readme](https://github.com/adewale/good-readme)) | Every change to the README, design docs, runbooks, SECURITY, CONTRIBUTING or CHANGELOG; checks that docs match the code. Rated 4.5 |
| README and docs | better-writing ([jakubkrehel/skills](https://github.com/jakubkrehel/skills)) | Wording of docs, tool descriptions and every user-facing answer template. Rated 4 |
| README and docs | readme-roast ([hidai25/readme-roast](https://github.com/hidai25/readme-roast)) | README structure and install steps only; ignore its growth-hacking advice. Rated 3.5 |
| Contracts and models | using-dbt-for-analytics-engineering ([dbt-labs/dbt-agent-skills](https://github.com/dbt-labs/dbt-agent-skills)) | Every dbt model and semantic-model change; run its column-level impact check. Rated 4.5 |
| Contracts and models | adding-dbt-unit-test (dbt Labs) | Unit tests for metric logic: totals never summed, rates never summed, snapshot changes. Rated 4.5 |
| Contracts and models | maintaining-dbt-documentation (dbt Labs) | Describe every column in clinical words, never restate its name. Rated 2.5; docs only |
| Contracts and models | dbt-transformation-patterns ([wshobson/agents](https://github.com/wshobson/agents)) | Layering: sources → staging → marts → metrics. Rated 3.5 |
| Pipeline | build-data-pipeline, model-data, load-data ([motherduckdb/agent-skills](https://github.com/motherduckdb/agent-skills)) | Adapters, snapshot loading into Parquet, DuckDB modelling, constraints. Rated 4 |
| Data quality | gx-configure-expectations (Great Expectations, shipped in the package) | Contract quality rules: only assert what the codebook states; an all-null column passes value checks, so add fill-rate checks; keep a metric error apart from a data failure. Rated 4 |
| Data quality | SodaCL docs (no public skill) | Freshness and reconciliation rules per source. Rated 3 |
| Security | sharp-edges ([trailofbits/skills](https://github.com/trailofbits/skills)) | Every change to fetchers, the SQL sandbox, workflows, tokens or the hosted server. Rated 4.5 |
| Hosting | cloud-run-basics, google-cloud-waf-security, cloud-run-alert-configuration ([google/skills](https://github.com/google/skills)) | Only if the server moves to the Cloud Run fallback: pinned image digests, one service account per job, job-failure alerts. Rated 4.5 |
| Hosting | terraform-skill ([antonbabenko/terraform-skill](https://github.com/antonbabenko/terraform-skill)) | Only if infrastructure moves to Terraform: reviewed plan files, drift, blast radius. Rated 4 |
| Catalog site, later | Owl-Listener designer-skills, Krehel UI set, no-ai-design-slop | Only when a public catalog page is built. Rated 4–4.5 |
| Review round | code-review (built into Claude Code) | The one review round only: a fresh subagent whose entire prompt is `/code-review <PR number>` |
| Fix round | simplify (built into Claude Code) | Optional, inside the one fix round only; never a separate round |

Left out: DuckDB's duckdb-skills (rated 1; DuckDB's own docs are more useful), wshobson data-quality-frameworks
(rated 2; the changes made with it were reverted), and HashiCorp terraform-style-guide (rated 1.5).

The transform layer is a real dbt project on DuckDB (`transform/`, owner decision of 25 Nov): raw → staging →
intermediate → marts. Staging is generated from each ODCS contract (`make contracts`); it only types, dedupes on
the contract's primary key and picks the latest raw load. Business rules live in intermediate models, and every
contract quality rule becomes a named dbt test. The MCP server stays the read layer on top. The dbt skills apply
to `transform/`, to `semantic/` (dbt-style metric YAML), to `registry.py`, which loads and cross-checks the
models, and to `query.py`, which compiles their metrics to SQL.
