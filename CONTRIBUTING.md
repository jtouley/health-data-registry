# Contributing

Issues and pull requests are welcome. The project has one maintainer, so for anything larger than a small fix,
open an issue first to agree the change.

- **Bugs, wrong numbers and dataset requests:** use the forms in `.github/ISSUE_TEMPLATE/`.
- **Vulnerabilities:** don't open an issue. Follow [SECURITY.md](SECURITY.md).
- **Conduct:** see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

No Developer Certificate of Origin sign-off and no CLA are required. By opening a pull request you agree that your
contribution is licensed under the [Apache License 2.0](LICENSE).

## Setting up

```bash
make install   # uv sync
make hooks     # once per checkout: the pre-commit and pre-push hooks
make check     # lint, type check, tests and mutation tests on changed functions (make precheck adds the rest of CI)
make help      # every target
```

`make hooks` installs both hook types from `.pre-commit-config.yaml` (a bare `pre-commit install` would need
pre-commit on your PATH and skip the pre-push hook); every commit goes through them, never `--no-verify`. Tests marked `network` hit live
publishers and are left out of `make test`; run them with `make test-network`.

## Requirements for every pull request

- `make check` passes: ruff lint and format, mypy (strict), pytest with at least 85% coverage, and mutation tests
  (`make mutate`) on the functions you changed, with at least 70% of mutants killed. Kill survivors with a test, or
  say in the pull request why a mutant is equivalent.
- CI is green before the pull request: merge `origin/main`, `make precheck` passes, then the commit goes green
  on `pre-pr/<branch>`, and only that commit goes to the PR branch ([the pre-PR gate](docs/dev/README.md#the-pre-pr-gate)).
- **New features and bug fixes add tests.** A bug fix includes a test that fails without it.
- No data is committed. Snapshots live in the Hugging Face dataset, and only for sources whose terms allow it
  (see [licenses/README.md](licenses/README.md)). Small hand-written fixtures in `tests/` are the exception.
- No patient data, anywhere: not in issues, tests, fixtures or examples.
- The pull request template is filled in, including which skills were used and what each changed.

## Review

Every change, including the maintainer's, lands through a pull request; nothing is pushed straight to main. Each pull
request gets one review round, then one round of fixes, and then it is merged. Each review comes from a fresh agent
and is posted on the pull request in full, by that agent, unedited. If your change touches data (catalog entries,
contracts, semantic models, adapters, storage or export code), data-quality rules and tests, the README and user docs,
anything security-sensitive (fetchers, the SQL sandbox, workflows, tokens, the hosted server: a sharp-edges
review), or CI and merge gating (workflows, the ruleset: deployment-pipeline-design and github-actions-templates
reviews), you'll also see a separate posted review for each matching skill in
[CLAUDE.md](CLAUDE.md#skills-and-how-to-use-them), in the same round. The fix round is one more fresh agent, started
with `/fix-round <PR number>`, and it addresses every posted finding.
Anything the fix round doesn't cover becomes a follow-up issue or a later pull request, which goes through the same
cycle. There is no second review round, so expect a short, predictable path to merge rather than repeated
back-and-forth. Every pull request is squash-merged into `main`, and its branch is deleted afterwards.

## Adding a dataset

A dataset is added in one pull request with four parts:

1. **Catalog entry** in `catalog/`: agency, years, geography, research area, licence and terms, citation, and the
   publisher hosts it is fetched from (HTTPS only).
2. **ODCS data contract** in `contracts/`: every column with its type, unit (UCUM), codes (ICD, GBD cause, FIPS),
   missing and suppression codes, and quality rules that assert only what the publisher's codebook states.
3. **Semantic model** in `semantic/`: the metrics a question can ask for, with how each aggregates (rates and
   totals are never summed).
4. **Blind questions**: 8 to 10 clinician questions written by someone who has not seen the connector, each answer
   computed twice (Polars and independent SQL), sealed with a hash under `eval/sealed/`. The author of the contract
   does not write them.

`make check-contracts` must pass against the live source. A dataset is merged only after the maintainer has reviewed
the contract against the publisher's documentation. Sources whose terms forbid redistribution are fetched at run
time and never snapshotted.

If you want a dataset but can't do the work, open a **Dataset request** issue instead.
