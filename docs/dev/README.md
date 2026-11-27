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

Every commit, by people, agents and the lead, goes through these hooks; `--no-verify` is never used. Unfinished
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
| `make ratchet` | The Trusted gate's ratchet on `HEAD` against `BASE` (default `origin/main`), as the PR check will run it, without the required-check rule ([below](#the-trusted-gate)) |
| `make lint` / `make fmt` | Ruff lint and format check / fix |
| `make typecheck` | mypy, strict |
| `make test` | Offline tests with coverage, one worker per core (pytest-xdist); fails under 85% (`COV_FLOOR`) |
| `make test-network` | Live publisher tests in `tests/integration` (marker `network`) |
| `make smoke` | The C. auris in Michigan reference journey on a synthetic lake (marker `smoke`; see [Smoke journey](#smoke-journey)) |
| `make mutate` | Mutation tests on the functions changed since `BASE` (default `origin/main`); fails under 70% killed (`MUTATION_FLOOR`) |
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
needs. Finished and inactive branches are archived (tagged, then deleted) by `branch-hygiene.yml`;
[branch-hygiene.md](branch-hygiene.md) explains it and how to restore a branch. Every Hugging Face repository is named by one convention, built in `naming.py`: see [naming.md](naming.md).
[crawler-audit.md](crawler-audit.md) lists every ingestion path and what each hard-codes for one source;
[ADR 0016](../adr/0016-crawler-factory.md) proposes how to move that into config.

**Checking the crawl keys.** The `crawl` environment holds `CENSUS_API_KEY`, `SOCRATA_APP_TOKEN` and
`OPENALEX_API_KEY`. Its deployment branches must be set to `main` (Settings → Environments → `crawl`): that rule
is what keeps the keys off other branches, because a run on another branch uses that branch's copy of the
workflow, which can drop the workflow's own main-only check. To check the keys, open the Actions tab, choose
"Check crawl API keys", then "Run workflow" with the branch set to `main`. It makes one request each to the
Census API, data.cdc.gov and OpenAlex, and reports each key as rejected (the publisher's bad-key answer) or
not checked (no answer, a rate limit or an outage: try again later, and don't replace the key). Otherwise the
Census key and the Socrata token are reported as not rejected: both APIs also answer without a key, so a 200
doesn't show the key was applied. OpenAlex is asked for its rate-limit status, which it gives only to a valid
key, so its key is reported as accepted, or as not confirmed when the answer doesn't prove it. The run fails
unless every key is present, Census and Socrata are not rejected and OpenAlex is accepted. It never prints a
key, a request URL, response headers or a body, and writes nothing.

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
1. **`make precheck`** runs the CI checks locally plus a drift check, and stops at the first failure: all
   pre-commit hooks (actionlint with shellcheck included), lint, typecheck, tests with the coverage floor, the
   smoke journey, `make transform` (dbt on the fixtures), the Projects kit build, and the generated-files drift
   check (`make docs` and `make contracts`, then nothing may change). Mutation tests are not in it, as they are not
   in CI: they run nightly ([CI](#ci)); `make mutate` runs them locally. Each step prints a PASS or FAIL line, and a
   summary follows. It checks the working tree, so commit first: CI checks the commit you push. It runs on macOS's
   bash 3.2 as well.
2. **Push to `pre-pr/<branch>`** (`git push origin HEAD:pre-pr/<branch>`). CI and CodeQL run on every push to
   `pre-pr/**`, against `origin/main`, with only what the change can affect while main has a merge queue (see
   [CI](#ci) below). A newer push to the same pre-pr branch cancels the older run and its queued jobs.
   `make precheck` does not run CodeQL; CI runs it here, and again on the PR.
3. **When every check is green**, open the PR from that exact commit, or fast-forward the PR branch to it
   (`git push origin <green sha>:<pr-branch>`).

**The PR reuses the whole pre-pr verdict when it tests the same tree.** On a `pull_request` event into `main`, the
changes job runs main's copy of `scripts/verdict_reuse.py`, while main has a merge queue, which asks the Actions
API, with the run's own read-only `GITHUB_TOKEN` (`actions: read` only here and on "CI result", which reads this
run's job times), for a CI run that:

- was triggered by a `push` to a `pre-pr/**` branch of this repository (not a fork), from `.github/workflows/ci.yml`;
- is for exactly the PR's head SHA, taken from the event payload (`github.event.pull_request.head.sha`);
- has a "CI result" job, in its latest attempt, that completed with conclusion `success`;

and only when the commit the PR's run tests (GitHub's merge of the PR into main) has exactly the head commit's
tree, so the pre-pr run checked exactly this code (#96's rule). If one exists, every other job is skipped and "CI
result" passes with a log line naming the reused run. A failed, cancelled, skipped or still-running verdict never
counts, nor does one for another SHA or another tree, and any API or git error means no reuse, so everything runs.
CodeQL still runs on the PR. So push the green pre-pr commit unchanged, on a branch that already contains main:
any new commit on the PR branch, or a main that moved on, has no matching verdict and runs everything.

Red on the pre-pr branch? Fix it there, run `make precheck` again, and push again. Fix-round commits and merges
of main take the same path.

**Making step 3 hard to skip.** Two things are meant to enforce it; the server rule is the one that will count, and
it isn't on yet (tracked in [#138](https://github.com/jtouley/health-data-registry/issues/138)):

- **On GitHub, once the maintainer applies it:** a ruleset on every branch except `main`, `pre-pr/**`, `wip/**`
  and `dependabot/**` requires the "CI result" check. For a branch that isn't the default branch, GitHub then
  refuses any push that moves the branch to a commit whose "CI result" hasn't already passed. Checks belong to the
  commit, not the branch, so the commit that went green on `pre-pr/<branch>` pushes through and any other commit is
  refused. `do_not_enforce_on_create` is off, so creating a PR branch is a push like any other: create it at the
  green commit (`git push origin <green sha>:refs/heads/<branch>`). "Update branch" on GitHub makes a new,
  unchecked commit and would be refused too; merge main locally and go through pre-pr. It needs `CI result`
  from [#122](https://github.com/jtouley/health-data-registry/pull/122) first.
- **Locally, today (fast feedback):** the `pre-push` hook (`make hooks` installs it; `scripts/pre_push_gate.py`)
  refuses a push to a PR branch (any branch but `pre-pr/**`, `wip/**`, `dependabot/**` and `main`) unless that
  exact commit has a completed, green run of `ci.yml` from a push to `pre-pr/**` (asked with `gh api`; any error
  refuses the push), and runs ruff, mypy and the changed modules' tests before a push to `pre-pr/**`. It has gaps
  pre-commit puts there: pre-commit runs pre-push hooks only for the first pushed ref that brings new commits, so
  the hook doesn't run when you create a PR branch at a commit already on `pre-pr/`, and in a push of several refs
  only the first is checked. It reads only `ci.yml`'s run: CodeQL and the other workflows on the pre-pr commit are
  yours to check before you push.

The actionlint and shellcheck hooks need `uv sync` first: both tools come from the locked dev environment
(`actionlint-py` and `shellcheck-py`, pinned in `uv.lock`), and the `shellcheck-present` hook fails if shellcheck
is missing, because actionlint would otherwise skip its shell checks silently.

## The Trusted gate

A pull request's own CI runs the pull request's copy of `ci.yml`, the Makefile, `scripts/` and `pyproject.toml`,
so a pull request can edit the checks it is judged by. No person reviews pull requests here, so one check runs
code the pull request cannot change: **Trusted gate**
([`.github/workflows/trusted-gate.yml`](../../.github/workflows/trusted-gate.yml)). It runs on every pull request
into `main`; it becomes a *required* check once the maintainer adds it to the `main` ruleset
([#138](https://github.com/jtouley/health-data-registry/issues/138)).

**How it stays trusted.** It runs on `pull_request_target`, so GitHub takes the workflow file from `main`, and it
checks out `main`'s commit (main's tip when the run starts), never the pull request's. It fetches the pull
request's commit as git objects and reads it as text (`git cat-file`, `git diff`), parsing Python with `ast`,
workflows with `yaml.safe_load` and TOML with `tomllib`: nothing from the pull request is imported, installed or
run. The token is `contents: read`, no secret is used, the uv cache is off, and only SHAs from the event payload
reach a shell. One exception: in the merge queue (`merge_group`), GitHub runs the queued commit's copy of the
workflow, so for a pull request that changes `trusted-gate.yml` the queue's result comes from that pull request's
own YAML (the workflow's header comment says the same).

The preferred enforcement is a ruleset "required workflows" rule that pins `trusted-gate.yml` to
`refs/heads/main`: GitHub then runs main's copy on every pull request, whatever the pull request changes, and no
other job can stand in for it. `ci.yml` is not pinned, so a pull request that changes CI is tested with its own
CI; the ratchet is what stops that change from weakening the checks. GitHub offers that rule only in organisation
rulesets, and this repository belongs to a user account, so the gate runs on `pull_request_target` instead. A
status check is matched by name only, so the gate also fails a pull request that adds a second job reporting a
required check's name, or a job whose name is an expression it can't resolve.

**What it fails.** `python -m health_data_registry.ratchet` compares the pull request with its merge base and fails
when the change:

| Rule | Fails when the change | Allowlist? |
|---|---|---|
| threshold | lowers or removes a floor, fail-under or threshold in `pyproject.toml`, the Makefile, `scripts/`, the workflows or another tool config, or sets the coverage floor under 85 or the mutation floor under 70 anywhere there | no |
| escape hatch | adds a skip, skipif or xfail mark, a test deselection or a pytest `-m`/`-k` filter, a no-cover or no-mutate pragma, a type-ignore or noqa comment, an inline mypy setting, a ruff, mypy, coverage, pytest or mutmut relaxation in `pyproject.toml`, a relaxing ruff or mypy flag, or a command whose failure is ignored | yes |
| escape hatch (unreadable) | adds or changes a settings file beside `pyproject.toml` (`mypy.ini`, `setup.cfg`, `pytest.ini`, `tox.ini`, `.coveragerc`, a nested `ruff.toml` or `pyproject.toml`), puts a NUL byte in a script or config, or removes a TOML setting outright | no |
| links | adds a symbolic link or a submodule, which the gate can't read through | no |
| tests | leaves fewer collected tests (parametrized cases counted, literal lists and module-level constants) in the test modules it touches; moving or renaming a test is fine | yes, per test |
| required check | removes or renames a check the ruleset requires, or adds another job reporting its name or a job with an unresolvable name; changes the `if:` of a required job or a job it needs, old or newly needed; drops a job from what a required job waits for; adds continue-on-error to one of them or their steps; adds an `if:` to one of their steps | `if:` only |
| sealed sets | touches `eval/sealed/` | no |
| hooks | adds the no-verify flag, the n flag of `git commit`, a hooks-path override or pre-commit's skip or allow-no-config variables; removes a hook from `.pre-commit-config.yaml` | only prose in Markdown, and a removed hook |
| allowlist, gate files | changes the allowlist with anything else; changes the gate's own files with anything but files under `docs/` and `CHANGELOG.md` | no |
| budgets | loosens `ci/budgets.yml`: raises a time budget, adds a key (a new budget is a new allowance), removes one, or deletes the file | no |
| mutation | weakens a job that runs, gathers or checks mutation tests, in any workflow (the PR shards, the nightly run, the release gate's mutation check): removes it, makes it or a step conditional, adds continue-on-error, drops a mutation command from its steps, takes a schedule away from its workflow, or stops a job that waited for it (a release job, an aggregate) waiting for it | yes, except continue-on-error |

The gate's own files are `trusted-gate.yml`, `src/health_data_registry/ratchet.py`, `tests/test_ratchet.py` and
`src/health_data_registry/__init__.py`. Note that `SECURITY.md`, `CLAUDE.md` and this repository's other root
Markdown files are not under `docs/`, so they can't come with a gate change.

Each failure names the file and line and says what weakened. An allowlistable one prints the exact entry after
`Allowlist entry:`; any other one prints its `Next step:`. The required checks come from the ruleset itself
(`GET /repos/{owner}/{repo}/rules/branches/main`); if that call fails or names no check, the gate fails.
`make ratchet` runs every rule but that one locally.

**The allowlist** is [`.github/ratchet-allowlist.txt`](../../.github/ratchet-allowlist.txt): one
`path :: exact stripped line` per entry (the file's header lists every entry shape). The gate reads it from
`main`, and it changes only in a pull request that changes nothing else, so an entry takes effect only after its
own pull request went through the review round and merged. Add the entry first, then the change that needs it;
once the entry is on `main`, push or edit the pull request's title to run the gate again (re-running the old run
reuses the `main` it started with). The allowlist may hold any number of entries, but each must be well-formed,
which `tests/test_ratchet.py` checks on every run: it parses as `path :: line`; its path is a plain repo-relative
path to a file under an existing top-level directory (a file that a later pull request adds counts, so a typo below
the top level is not caught); and a comment with text stands above it in its own block, saying why. The comments
that open the file are its header and never count as an entry's reason, so leave a blank line after the header.
An entry that breaks one of these fails the tests of the allowlist's own pull request, with the line number and
the reason. The check also runs on `main` and on every later pull request, so one that removes or renames a
top-level directory an entry names fails it too, and can't fix the entry itself: first remove or update the entry
in a pull request that changes only the allowlist, merge it, then run the checks again.

**What it cannot stop.** A pull request that changes the gate's own files is judged by `main`'s gate, but once it
merges, the next pull request is judged by the changed gate; the gate only makes such a change stand alone
([#141](https://github.com/jtouley/health-data-registry/issues/141)). It reads text, so it doesn't see a check
weakened by logic (a test that asserts nothing, a step whose command is changed to `true`, a comparison flipped
inside a script), and an allowlist entry needs no person, only its own pull request; one entry allows its line
any number of times in that file ([#142](https://github.com/jtouley/health-data-registry/issues/142)). A job
counts as a mutation job when its id, name, steps, env or reusable workflow mention mutation (`mutmut`, `mutate`,
`mutation`); a release gate that checks mutation inside a script under another name is protected only by the
threshold rule on that script's floor. Its verdict on a pull request is from the `main` it ran against: it doesn't
run again when `main` moves, and the merge queue's run of a gate-changing pull request is self-reported (above).
It runs only after the pull request is open, not in `make precheck`
([#140](https://github.com/jtouley/health-data-registry/issues/140)).

### When the gate itself is wrong

The gate fails closed, and every pull request, a fix to the gate included, is judged by `main`'s copy. So a false
positive on most diffs, a change in the ruleset API's answer, a broken action pin or a `uv sync --locked` failure
on main's lock blocks every pull request. Only the maintainer can lift it:

1. Open an issue recording what broke and the failing run.
2. Stop requiring the check: Settings → Rules → Rulesets → `main`, remove `Trusted gate` from the required status
   checks (or set the ruleset's enforcement to "Evaluate" for a broader outage). With the CLI, fetch the ruleset
   (`gh api repos/jtouley/health-data-registry/rulesets/<id> > main-ruleset.json`), edit it, and write it back
   (`gh api -X PUT repos/jtouley/health-data-registry/rulesets/<id> --input main-ruleset.json`).
3. Land the fix through the normal cycle (precheck, pre-pr, one review round, one fix round).
4. Put the check back the same way, confirm the next pull request gets a `Trusted gate` result, and close the
   issue. [#130](https://github.com/jtouley/health-data-registry/issues/130) tracks committing the ruleset JSON
   with its forward and rollback states.

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
- **Mutation tests.** `scripts/mutate_changed.py` runs mutmut on the functions changed since the merge base and
  prints each mutant it did not kill. A function is changed when it holds an added, edited or deleted line, or
  reads a module-level name that a changed line defines (an allowlist entry, a limit, an import), directly or
  through names built from it; every method of a class whose attributes changed counts too, and so does every
  function of a new file. mutmut mutates function bodies only, so a change to a docstring or a comment makes no
  mutant, and the list names any changed module left with nothing to test. A survivor means the tests would not
  notice that change: add or sharpen a test, or explain why the mutant is equivalent. Every status but
  `killed` (and `caught by type check`) lowers the score, `no tests` and `skipped` included. `make mutate` is part
  of `make check`. CI runs the same check nightly on main, not on pull requests ([Nightly mutation
  tests](#nightly-mutation-tests)), in shards: `--list` names the shards of the changed functions, one job per
  shard runs mutmut (at most 6 at once), and "Mutation result" applies the 70% floor to the summed counts and lists
  every mutant not killed. The plan job resolves the base once and hands it to every shard, so they scope to the
  same functions. Each shard generates mutants for its own module only. It starts from mutmut stats (which tests
  call which function) that the run's stats job collects once, over every test, on the commit it mutates, and
  mutmut collects stats again only for the tests the map links to a changed function and for every test in a
  changed test file. Without stats (`make mutate` locally), mutmut's stats run covers the test files that reach
  the module: by import, directly or through other modules, scripts/ and tools/ included, or by loading a file by
  path. Either way a test left out can only lower the score, never count a mutant as killed. Shards are named
  `<module>#<k>of<n>@<split>`: the changed functions, split by function, and a big function by the last digit of
  its mutant numbers, to about 3 minutes of mutants each (`SHARD_TARGET`, smaller for `registry.py`; a shard's job
  limit is 60 minutes). The split comes from the module's source and the changed functions alone, read with the
  same Python 3.12 in `--list` and in every shard; `<split>` is a hash of it, and a shard refuses an id whose
  hash differs from its own split, so an edit made after `--list` fails the shard instead of running the wrong
  mutants. Each shard also checks that every mutant of its module falls in exactly one shard. While a shard runs, `pyproject.toml`
  carries its `only_mutate` and test selection; a run stopped with Ctrl-C or SIGTERM restores it, and if a killed
  run leaves it edited, the next run refuses to start until you restore the file. Functions mutmut never mutates (under
  `# pragma: no mutate`, for instance) carry no weight, and `@overload` stubs count with their function. It
  fails if any shard failed its baseline, left mutants not checked, interrupted or crashed, found a mutant in no
  shard, or left no report, or if only some shards of a module were expected. A module with no mutable
  function, or a shard none of whose names matches a mutant, has nothing to mutate and passes, locally and in
  CI.
- **New features and fixes add tests.** A bug fix includes a test that fails without it.

## CI

`.github/workflows/ci.yml` runs only what a change can affect, and everything where it matters most:

| Event | What runs |
|---|---|
| Pull request, push to `pre-pr/**`, while main has a merge queue | The changes job maps the changed files to work (`scripts/ci_impact.py`, rules in its docstring); Checks; the test files the change can reach, on Python 3.12 |
| Pull request, push to `pre-pr/**`, while main has no merge queue | Everything, as below, and no pre-pr verdict is reused: nothing else would run it before the merge |
| Merge queue (`merge_group`), push to `main`, nightly, by hand | Everything: Checks with the kit and dbt, every test on Python 3.12 and 3.13 with the coverage floor |

Mutation tests are in none of these: they run nightly on main ([Nightly mutation tests](#nightly-mutation-tests)).

- **Changes** (seconds): whether main has a merge queue (`scripts/merge_queue.py`: a `merge_queue` rule in effect
  on main; any doubt answers no), what the change can affect, and on a PR whether a green pre-pr run already
  checked this exact tree (then every other job is skipped).
- **Checks**: budgets only tighten, generated files current (`make docs`, `make contracts`), then in parallel every
  pre-commit hook (ruff, mypy, actionlint and the rest), the Claude Projects kit, and `dbt build` on the fixtures
  when dbt inputs or the Python dbt imports changed.
- **Tests**: `pytest -n auto` (one worker per core). A README-only change runs no test; a change to a module runs
  every test file that imports it, however indirectly; a change to CI config, `scripts/`, the lockfile or any path
  the map does not know runs everything.
- **CI result**, the one required CI check: it passes only when every job the changes job selected passed and
  every other job was skipped. A cancelled run fails it.

**What a pull request skips, the merge queue runs before the merge**, so main is tested as it will be. This holds
only while every merge goes through the queue: keep the ruleset that holds the `merge_queue` rule free of bypass
actors. A merge that bypasses it is tested by main's own full run right after, too late to block it.

**Job limits.** Checks: 3 minutes on a narrowed run, 10 on a full one. Tests: 3 and 15. Changes: 5. CI result: 3.

**Budgets.** `ci/budgets.yml` holds a running-time budget for every job and for the run's critical path (Changes
plus the longest job after it; queue time is printed, never counted). The pre-PR target is 3 minutes from push to
every check done; the `pr` profile's job budgets add up to that on the critical path (`tests/test_ci_probes.py`
checks the sum). "CI result"'s CI budget step reads this run's job times from the Actions API, with the `pr`
profile on a narrowed run and `full` on a full one. On a pull request or a pre-pr push it fails the run when a
job or the run went over, listing them. On the merge queue, main and nightly it only warns: runner speed is no
reason to block a merge. If the job times cannot be read, it warns and passes. Budgets only tighten: the Checks job
fails a change that gives any job a larger effective budget (a larger number, a budget taken away, a new key or a
reordered pattern), unless the change touches `ci/budgets.yml` alone (a pull request of its own, for the
maintainer to approve).

**Probes.** `tests/ci_probes/probes.yml` is a fixed set of tiny synthetic changes (docs only, a contract, a function
in sql_guard.py and in registry.py, an allowlist constant in sql_guard.py, a new file), each with the jobs it must
select, whether the nightly mutation run would mutate it, and a time budget. `tests/test_ci_probes.py` checks every
selection offline; `python scripts/ci_probe.py apply NAME` makes one in a checkout, for a live timing run on a
pre-pr branch.

**Switching the required checks, and back.** The maintainer changes the `main` ruleset in the same sitting as the
merge that brings in "CI result":

1. In `required_status_checks`, replace the old check names (Lint (ruff), Types (mypy), Test (Python 3.12), Test
   (Python 3.13), Pre-commit hooks (all files), Claude Projects kit, Mutation tests (changed modules), Smoke: C.
   auris journey) with `CI result`.
2. Turn on the `merge_queue` rule (squash, one entry at a time), with no bypass actors on that ruleset.
3. Keep `strict_required_status_checks_policy: false`: the queue tests each merge on current main.

To roll back, in this order, or the revert deadlocks (the old `ci.yml` has no `merge_group:` trigger and no "CI
result", so a queued revert waits for a check that never reports): (1) remove the `merge_queue` rule; (2) restore
the old required check names; (3) merge the revert. After turning the queue off for any reason, re-run CI on open
pull requests before merging them: their narrowed runs relied on the queue.

## Nightly mutation tests

`.github/workflows/mutation.yml` runs at 02:41 UTC on main, and by hand (Actions, Mutation, Run workflow). It
mutates every function changed on main since the last green run (`scripts/mutation_nightly.py base`; before the
first, since the commit that added the workflow). A dispatch may give an earlier base, never a later one, so no
change is skipped. A red run never moves the base: the next run covers its range again.

- **Plan** lists the shards (`scripts/mutate_changed.py --list`). **Mutation stats** collects mutmut's stats over
  every test once, on this commit, and every shard starts from them. **Mutation tests (shard)** runs one shard,
  at most 6 at once, each with a 60-minute limit. **Mutation result** applies the 70% floor to the total and fails
  closed, as above.
- **Survivors** open or update one issue per originating pull request or commit (label `mutation-survivors`,
  title "Mutation survivors from #N"): the newest commit since the base that changed a line of the survivor's
  function, or else its file. Kill each survivor with a test, or explain in the issue why it is equivalent, then
  close the issue. A closed issue that already lists the same survivors is not reopened.
- **The run's own failure** (a red run) shows on the run, and the next run covers the same range.
- **The release gate.** A release waits for `scripts/mutation_nightly.py gate`: the latest completed nightly run
  on main whose commit is the released commit or an ancestor of it must be green, no file under
  `src/health_data_registry/` or `eval/` may have changed since that commit, and no `mutation-survivors` issue may
  be open. `release.yml` (v-tags), `deploy-space.yml` (the Space) and `refresh.yml` (the hub publish) each run it
  as a `mutation-gate` job before the job that publishes. Any doubt, an API error included, blocks the release.
  Until the first nightly run, nothing can be released: start one by hand.

## Smoke journey

`make smoke` runs the project spec's reference journey: a clinician asks about C. auris in Michigan in 2024.
The spec itself is not in this repo; [docs/architecture.md](../architecture.md) describes the design it set
out, and the journey is defined by `tests/smoke/test_c_auris_journey.py`. It is part of `make test`, so CI runs it
in the test job whenever a change can reach it, and on every full run.

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

Add `tests/integration/test_live_<id>.py` for the live check. Hand-written dbt unit tests for a new source go in
their own files under `transform/models/unit_tests/`, one per model they test (`staging_typing_<id>.yml`,
`total_rows_<id>.yml`), so source pull requests running side by side don't edit one shared file;
`staging_typing.yml` keeps the tests written before this layout. A contract merges only after a maintainer reviews it
against the publisher's documentation. Sources whose terms forbid redistribution are "listed, not hosted": they get a
catalog entry and a contract, are fetched from the publisher at run time, and are never published as snapshots
([ADR 0009](../adr/0009-licences-decide-mirroring.md)).
