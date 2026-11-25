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

The README's images (`assets/readme/*.svg`, light and dark) come from one token set: edit and run
`python3 -I tools/readme_svg/gen.py`, never the SVGs by hand.

| Command | What it does |
|---|---|
| `make install` | `uv sync` |
| `make check` | `lint`, `typecheck`, `test` and `mutate`: the quick local loop, not all of CI |
| `make precheck` | The pre-PR gate's local half: the CI checks plus a docs-drift check, stopping at the first failure ([below](#the-pre-pr-gate)) |
| `make lint` / `make fmt` | Ruff lint and format check / fix |
| `make typecheck` | mypy, strict |
| `make test` | Offline tests with coverage; fails under 85% (`COV_FLOOR`) |
| `make test-network` | Live publisher tests in `tests/integration` (marker `network`) |
| `make smoke` | The C. auris in Michigan reference journey on a synthetic lake (marker `smoke`; see [Smoke journey](#smoke-journey)) |
| `make mutate` | Mutation tests on modules changed since `BASE` (default `origin/main`); fails under 70% killed (`MUTATION_FLOOR`) |
| `make docs` | Regenerate `docs/helpers.md` from `src/health_data_registry/helpers.py` |
| `make refresh` | `health-data-registry refresh --all` into the local lake |
| `make serve` | Run the MCP server on stdio |
| `make serve-http` | The hosted read-only server over Streamable HTTP on port 7860 ([hosting.md](hosting.md)) |
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

0. **Merge `origin/main` into the branch** (`git fetch origin main && git merge origin/main`). A PR's CI tests
   the branch merged with current main (`refs/pull/<n>/merge`), while a pre-pr run tests the branch tip alone, so
   the two agree only when the branch already contains main. If main moves after a green pre-pr run, merge it
   again and repeat the steps below.
1. **`make precheck`** runs the CI checks locally plus a docs-drift check, and stops at the first failure: all
   pre-commit hooks (actionlint with shellcheck included), lint, typecheck, tests with the coverage floor, the
   smoke journey, the Projects kit build, the docs drift check (`make docs`, then nothing may change), and
   mutation tests on the modules changed since `BASE` (default `origin/main`), one step per shard as CI shards
   them, then the 70% floor on the total. Each step prints a PASS or FAIL line, and a summary follows. It checks
   the working tree, so commit first: CI checks the commit you push. It runs on macOS's bash 3.2 as well.
2. **Push to `pre-pr/<branch>`** (`git push origin HEAD:pre-pr/<branch>`). CI and CodeQL run on every push to
   `pre-pr/**`, mutation shards included, against `origin/main`. A newer push to the same pre-pr branch cancels
   the older run. `make precheck` does not run CodeQL or the Python 3.13 test leg; CI runs them here, and again
   on the PR.
3. **When every check is green**, open the PR from that exact commit, or fast-forward the PR branch to it
   (`git push origin <green sha>:<pr-branch>`).

**The PR reuses the pre-pr mutation verdict.** The mutation shards are by far the dearest part of a CI run, and
the account runs only a few jobs at once, so a PR does not mutate a commit the gate already mutated. On a
`pull_request` event, the list job runs `scripts/mutation_reuse.py` (the copy on the base branch, so a PR can't
change its own lookup). What it reuses is the pre-pr verdict for the PR's head SHA, and only when that head
already contains the PR's base commit. A PR's CI tests the merge of its head into main; when the head already
contains main's commit (`github.event.pull_request.base.sha` is an ancestor of the head SHA), that merge has the
head's own tree, so the code, tests, `ci.yml` and `mutate_changed.py` are exactly what the pre-pr run mutated.
The script then asks the Actions API, with the run's own read-only `GITHUB_TOKEN` (`actions: read` on that job
only), for a CI run that:

- was triggered by a `push` to a `pre-pr/**` branch of this repository (not a fork), from `.github/workflows/ci.yml`
  (matched by workflow id and path);
- is for exactly the PR's head SHA, taken from the event payload (`github.event.pull_request.head.sha`);
- has a "Mutation tests (changed modules)" job, in its latest attempt, that completed with conclusion `success`;

and only when the PR's base is `main`, the branch a pre-pr run compares with. If one exists, the list job lists no
modules, the shards are skipped, and "Mutation tests (changed modules)" passes on the PR with a log line naming the
reused run. A branch behind main gets no reuse, nor does a failed, cancelled, skipped or still-running verdict, or
one for another SHA. Any lookup error (the API, git, or the command itself failing) means no reuse, so the shards
run as before; it never fails the check. Lint, types, tests, smoke, the kit, pre-commit and CodeQL still run on the
PR, and every required check still reports. The pre-PR gate merges main before the pre-pr push, so a branch that
is still up to date when the PR runs reuses the verdict. Push the green pre-pr commit unchanged: any new commit on
the PR branch has no pre-pr verdict and runs the shards, and so does a PR whose main moved on in the meantime.

Red on the pre-pr branch? Fix it there, run `make precheck` again, and push again. Fix-round commits and merges
of main take the same path.

The actionlint and shellcheck hooks need `uv sync` first: both tools come from the locked dev environment
(`actionlint-py` and `shellcheck-py`, pinned in `uv.lock`), and the `shellcheck-present` hook fails if shellcheck
is missing, because actionlint would otherwise skip its shell checks silently.

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
  check on every PR in shards: `--list` names the shards of the changed modules, one job per shard runs mutmut,
  and the aggregate job applies the 70% floor to the summed counts and lists every mutant not killed. A module
  is one shard, or, when its estimated mutants exceed a shard's size (`SHARD_TARGET`, smaller for
  `registry.py`), several named `<module>#<k>of<n>@<split>`: split by function, and a big function by the last
  digit of its mutant numbers, so each shard takes about 10 to 20 minutes (the job's limit is 40). The split
  comes from the module's source alone, read with the same Python 3.12 in `--list` and in every shard; `<split>`
  is a hash of it, and a shard refuses an id whose hash differs from its own split, so an edit made after
  `--list` (in CI or during precheck) fails the shard instead of running the wrong mutants. Each shard also
  checks that every mutant of its module falls in exactly one shard. Functions mutmut never mutates (under
  `# pragma: no mutate`, for instance) carry no weight, and `@overload` stubs count with their function. It
  fails if any shard failed its baseline, left mutants not checked, interrupted or crashed, found a mutant in no
  shard, or left no report, or if only some shards of a module were expected. A module with no mutable
  function, or a shard none of whose names matches a mutant, has nothing to mutate and passes, locally and in
  CI.
- **New features and fixes add tests.** A bug fix includes a test that fails without it.

## Smoke journey

`make smoke` runs the project spec's reference journey: a clinician asks about C. auris in Michigan in 2024.
The spec itself is not in this repo; [docs/architecture.md](../architecture.md) describes the design it set
out, and the journey is defined by `tests/smoke/test_c_auris_journey.py`. CI runs it on every pull request as
the job "Smoke: C. auris journey". It is part of `make test` too.

- **Offline, enforced.** `tests/smoke/conftest.py` makes any connection or DNS lookup off this machine fail the
  test, so a stray fetch can't quietly read live data.
- **What it drives.** The real MCP server (`server.build_server`) over the real `catalog/`, `contracts/` and
  `semantic/`, reading a lake built at test time from hand-written rows in `tests/smoke/fixtures.py`. The
  numbers are made up and unlike the published ones, and nothing is written to the repo.

The steps, one test each (`test_stepN_...` in `tests/smoke/test_c_auris_journey.py`):

1. `suggest_datasets` offers `cdc_nndss_weekly` and nothing else.
2. `describe_dataset` names both C. auris series and the flag codes.
3. `get_series` returns clinical and screening as separate series. Each has its year-end running total and
   next year's unverified restatement. They are never added, and total rows are left out.
4. A flagged place stays empty with its flag, never a zero.
5. `get_rate` divides by the Census population, returns the 95% interval with each rate, and never adds the
   two rates.
6. `list_caveats` keeps NNDSS, CDC's tracking page and the AR Lab Network apart.
7. An uncatalogued source shows no number, and only NNDSS is offered for it.
8. `get_provenance` names the exact fetch.

Every table's provenance line must be complete and carry the sha256 of the raw bytes.

- **The DEV trial questions.** The eval has two splits: DEV (`eval/dev/dev.jsonl`), which anyone may read and
  tune against, and TEST, which is held out and sealed ([eval/README.md](../../eval/README.md)). The smoke test
  scores the four DEV questions tagged `source: trial` with `eval/scorer.py`. A scripted client
  (`tests/smoke/client.py`) writes a reply from the tool responses only, and the reply must pass Class, Target,
  Provenance and the fabrication gate. Three pass today: C. auris, measles and vaccination. HIV care waits on
  #37 (see below). Replies that add clinical to screening, add two rates, or give a number no tool returned
  must fail. The smoke test never reads the TEST split or `eval/sealed/`.
- **Only NNDSS is catalogued for C. auris.** CDC's tracking page and the AR Lab Network have no catalog entry
  yet, so their steps check that the server shows no number for them. When one is catalogued, add its series
  to the journey.
- **Behaviour still to come** is `xfail(strict=True, raises=AssertionError)`, naming its issue: HIV care
  declined rather than offered RESP-NET (#37 item 9), the AR Lab Network question offered only NNDSS (#48), and
  decline fields that a machine can read in csv (#37 item 3). When the fix lands the test fails as XPASS;
  remove the mark in the same pull request.

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
