# Developer docs

For people working on the code, contracts or evaluation. Clinicians start at the [README](../../README.md), the
[setup guide](../setup.md), the [tools reference](../tools.md) and the [data conventions](../data-conventions.md). Contribution rules are in [CONTRIBUTING.md](../../CONTRIBUTING.md); design decisions
are in [docs/adr/](../adr/README.md).

## Setup

You need [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 if needed) and Git. Run everything from the repo
root.

```bash
git clone https://github.com/jtouley/health-data-registry.git
cd health-data-registry
make install            # uv sync, dev tools included
make check              # what CI runs
make hooks              # once: uv run pre-commit install (pre-commit and pre-push hooks)
```

Every commit, by people, subagents and the lead, goes through these hooks; `--no-verify` is never used. Unfinished
work is not committed to the PR branch: it stays uncommitted or goes on a separate WIP branch until it passes the hooks.

## Commands

`make help` lists every target.

| Command | What it does |
|---|---|
| `make install` | `uv sync` |
| `make check` | `lint`, `typecheck`, `test` and `mutate`: everything CI runs |
| `make precheck` | The pre-PR gate: every CI step locally, stopping at the first failure ([below](#the-pre-pr-gate)) |
| `make lint` / `make fmt` | Ruff lint and format check / fix |
| `make typecheck` | mypy, strict |
| `make test` | Offline tests with coverage; fails under 85% (`COV_FLOOR`) |
| `make test-network` | Live publisher tests in `tests/integration` (marker `network`) |
| `make mutate` | Mutation tests on modules changed since `BASE` (default `origin/main`); fails under 70% killed (`MUTATION_FLOOR`) |
| `make docs` | Regenerate `docs/helpers.md` from `src/health_data_registry/helpers.py` |
| `make refresh` | `health-data-registry refresh --all` into the local lake |
| `make serve` | Run the MCP server on stdio |
| `make check-contracts` | Check each publisher still serves the fields its contract reads |
| `make eval` | Verify the sealed blind sets; with `--run`, score a client run against them |

The CLI behind them: `health-data-registry serve | refresh <dataset>|--all [--max-rows N] | fetch <source> |
export-public --to hf://datasets/<org>/<name>|DIR [--run-date YYYY-MM-DD] | check-contracts | validate |
eval [--sealed DIR] [--run FILE]`. `fetch <source>` refreshes one source. `export-public` publishes the sources
whose licence allows redistribution to the open data hub (cards, licence text, Parquet, `manifest.json`, tag
`release-<run date>`), lists the rest with a card and no data, and stops if a pre-publish check fails; it needs
`HF_TOKEN` only for an `hf://` target. `validate` loads and cross-checks every catalog entry, contract and
semantic model.

The lake lives at `$HDR_LAKE` (default `./lake`, ignored by git), partitioned by source and run date
(`datasets/<id>/run_date=<date>/<snapshot>/`), with raw bytes under `raw/<sha256>` and one fetch-log row per
attempt under `fetch_log/source=<id>/`. `refresh --max-rows N` takes a development sample and marks the snapshot
partial. The lake is for maintainers and CI: the server reads a hosted source from the hub release in `$HDR_HUB`
(checked against its sha256), cached under `$HDR_CACHE` (default `~/.cache/health-data-registry`, bounded by `$HDR_CACHE_MAX_GB`, default 5, least
recently used first out; publishers' raw bytes are never kept there); `hub.py` has the order. Reading the hub needs no sign-in or
token. A restricted source (WHO GHO, under WHO's Terms and Conditions for datasets) is never stored anywhere, not even in the
maintainers' lake: `live.py` reads it from the publisher per question, keeps the parsed records in memory for at
most 10 minutes, and every answer carries the attribution, licence and terms URL. `refresh --all` skips it. The server reads
only an immutable release: `HDR_HUB=hf://datasets/<org>/<name>@release-<run date>` pins one, and an unpinned
hub resolves to its newest `release-*` tag, recorded in every result; a branch such as `main` is refused. A
release built with a different contract version than the installed one is refused unless a maintainer adds the
pair to `hub.COMPATIBLE_CONTRACTS`. Published tags never move: a correction for the same run date is
`release-<run date>.2` and the hub's `CHANGELOG.md` names the release it supersedes.

What the hashes prove: a file's sha256 matching `manifest.json` shows the download is the file the manifest
names. It does not show that the manifest, the Hugging Face account or the publisher is trustworthy. Release
integrity and publisher provenance are separate controls; signed manifests (for example Sigstore) and build
attestations (SLSA provenance) for each release would add them, and are not in place yet.

Each source's entry in a release's `manifest.json` has a `quality` section for the weekly build's data tests
(dbt: staging, then intermediate, then public), so the MCP can show them:
`{"tool": "dbt" | null, "results": [{"test", "checks" (what it checks, in plain words), "status" (pass, warn or
fail), "rows_affected" (or null)}]}`. It is `{"tool": null, "results": []}` until the dbt build lands.
`export_public(..., quality={source_id: [results]})` fills it. Every hosted card, `describe_dataset` and
`get_provenance` also say how to add the source to a Claude project: the plugin (coming), the MCP server
reading the pinned release, or attaching the downloaded file by hand.

The scheduled refresh, crawls, sitemap snapshots, publishing and Space deploy run on GitHub Actions;
[automation.md](automation.md) lists each workflow, its schedule and limits, and the secrets and variables it
needs. Every Hugging Face repository is named by one convention, built in `naming.py`: see [naming.md](naming.md).

To run the server in Claude Code from a checkout:

```bash
claude mcp add health-data-registry -- uv run --directory "$PWD" health-data-registry serve
```

## The pre-PR gate

A pull request's CI should pass the first time. So a commit is checked twice before it reaches a PR branch:

1. **`make precheck`** runs every step CI runs, in order, and stops at the first failure: all pre-commit hooks
   (actionlint with shellcheck included), lint, typecheck, tests with the coverage floor, the `smoke` target if
   one exists, the Projects kit build, the docs drift check (`make docs`, then nothing may change), and mutation
   tests on the modules changed since `BASE` (default `origin/main`), one step per module as CI shards them, then
   the 70% floor on the total. Each step prints a PASS or FAIL line, and a summary follows. It checks the working
   tree, so commit first: CI checks the commit you push.
2. **Push to `pre-pr/<branch>`** (`git push origin HEAD:pre-pr/<branch>`). CI and CodeQL run on every push to
   `pre-pr/**`, mutation shards included, against `origin/main`. A newer push to the same pre-pr branch cancels
   the older run. CodeQL and the Python 3.13 test leg run only here.
3. **When every check is green**, open the PR from that exact commit, or fast-forward the PR branch to it
   (`git push origin <green sha>:<pr-branch>`). The PR's CI re-runs on the same commit.

Red on the pre-pr branch? Fix it there, run `make precheck` again, and push again. Fix-round commits and merges
of main take the same path. A red commit never goes to a PR branch.

The actionlint hook needs `uv sync` first: shellcheck comes from the locked dev environment, and the
`shellcheck-present` hook fails if it is missing, because actionlint would otherwise skip its shell checks
silently.

## Architecture

[docs/architecture.md](../architecture.md) has the diagram, the module map and the data flow: adapter fetch → raw
bytes, hash-addressed → contract-driven normalise → snapshot + manifest → DuckDB read-only views → tools.

The MCP surface (`server.py`):

- **Tools** (all read-only): `list_sources`, `search_datasets`, `describe_dataset`, `suggest_datasets`,
  `get_series`, `get_aligned_with_gbd`, `get_rate`, `get_provenance`, `list_caveats`, `find_prior_work` (returns
  "index not built yet") and `query_sql`, the guarded escape hatch. Results are CSV by default; `fmt="json"` gives
  structured output.
- **Helpers** (MCP prompts): `start_here`, `research_story`, `scope_an_area`, `find_opportunities`,
  `refine_hypothesis`, `build_story`, `help_me_fix_it`, `walk_me_through_install`, `set_up_my_project`; the same
  workflows ship as skills with hyphenated names (`/start-here`, ...). Their text lives in `helpers.py`;
  [docs/helpers.md](../helpers.md), `template/.claude/skills/` and `plugin/skills/` are generated from it by
  `make docs`, and a test fails if they differ. Never edit the generated files by hand.

When a tool's name, parameters, columns or refusals change in `server.py` or `tools.py`, update
[docs/tools.md](../tools.md) in the same PR; when a contract changes how a number is read (missing codes, total rows,
provisional status, uncertainty, places), update [docs/data-conventions.md](../data-conventions.md). The tool table in
README.md, docs/tools.md and CLAUDE.md is generated from the server's registry by `make docs`, and a test fails if it
drifts; the rest of docs/tools.md, and all of docs/data-conventions.md, is written by hand.

Everything a clinician reads (tool descriptions, helpers, refusals) follows the writing rules in
[ADR 0012](../adr/0012-built-for-clinicians.md): plain words, a next step, never a stack trace or an HTTP code.

## Tests

- **Synthetic data only.** Unit, property (Hypothesis) and metamorphic tests run on small hand-written fixtures and
  synthetic lakes. No public data is committed.
- **Network tests.** `tests/integration/test_live_<dataset>.py` fetch a small slice from the publisher at test time,
  write nothing to disk and are marked `network`. `make test` and CI's test job leave them out; run
  `make test-network` when you change an adapter, a contract or the fetcher.
- **Mutation tests.** `scripts/mutate_changed.py` runs mutmut on the modules changed since the merge base and
  prints each mutant it did not kill. A survivor means the tests would not notice that change: add or sharpen a
  test, or say in the PR why the mutant is equivalent. Every status but `killed` (and `caught by type check`)
  lowers the score, `no tests` and `skipped` included. `make mutate` is part of `make check`. CI runs the same
  check on every PR in shards: `--list` names the changed modules, one job per module runs mutmut (60 minutes
  each; the largest module needs about 17), and the aggregate job applies the 70% floor to the summed counts and
  lists every mutant not killed. It fails if any shard failed its baseline, left mutants not checked, interrupted
  or crashed, or left no report. A module with no mutable function has nothing to mutate and passes, locally and
  in CI.
- **New features and fixes add tests.** A bug fix includes a test that fails without it.

## Adding a dataset

One pull request, four parts. Scope is infectious disease only for now.

1. `catalog/<id>.yaml`: agency, publisher, level, years, geography, shape, grain, refresh cadence, licence and
   terms, citation, source host and dataset, codebook URL, caveats, and the GBD cause and location crosswalk.
2. `contracts/<id>.odcs.yaml` (ODCS v3.2.0): every column with its type and unit, codes, missing and suppression
   codes, total rows, and quality rules that assert only what the codebook states. A fact that changes a number
   comes only from the publisher's codebook, an exact structural fact, or a maintainer's reviewed declaration;
   anything else is an open question in the contract ([ADR 0007](../adr/0007-odcs-contracts-and-semantic-models.md)).
3. `semantic/<id>.yaml`: dimensions, measures and metrics, each with its aggregation rule. Frequency counts are
   summed with total rows excluded; rates and estimates are never summed.
4. Blind questions: 8–10 clinician questions by an author who has not seen the connector or written the contract,
   each answer computed twice, sealed with a hash under `eval/sealed/`.

Then:

```bash
uv run health-data-registry validate
make check-contracts
uv run health-data-registry refresh <id> --max-rows 500
make check
```

Add `tests/integration/test_live_<id>.py` for the live check. A contract merges only after a maintainer reviews it
against the publisher's documentation. Sources whose terms forbid redistribution are "listed, not hosted": they get a
catalog entry and a contract, are fetched from the publisher at run time, and are never published as snapshots
([ADR 0009](../adr/0009-licences-decide-mirroring.md)).
