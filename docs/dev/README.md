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
pre-commit install      # once, to enable the hooks in .pre-commit-config.yaml
```

## Commands

`make help` lists every target.

| Command | What it does |
|---|---|
| `make install` | `uv sync` |
| `make check` | `lint`, `typecheck`, `test` and `mutate`: everything CI runs |
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

The CLI behind them: `health-data-registry serve | refresh <dataset>|--all [--max-rows N] | check-contracts |
validate | eval [--sealed DIR] [--run FILE]`. `validate` loads and cross-checks every catalog entry, contract and
semantic model.

The lake lives at `$HDR_LAKE` (default `./lake`, ignored by git). `refresh --max-rows N` takes a development
sample and marks the snapshot partial.

To run the server in Claude Code from a checkout:

```bash
claude mcp add health-data-registry -- uv run --directory "$PWD" health-data-registry serve
```

## Architecture

[docs/architecture.md](../architecture.md) has the diagram, the module map and the data flow: adapter fetch → raw
bytes, hash-addressed → contract-driven normalise → snapshot + manifest → DuckDB read-only views → tools.

The MCP surface (`server.py`):

- **Tools** (all read-only): `list_sources`, `search_datasets`, `describe_dataset`, `suggest_datasets`,
  `get_series`, `get_aligned_with_gbd`, `get_rate`, `get_provenance`, `list_caveats`, `find_prior_work` (returns
  "index not built yet") and `query_sql`, the guarded escape hatch. Results are CSV by default; `fmt="json"` gives
  structured output.
- **Helpers** (MCP prompts): `start_here`, `set_up_my_project`, `spot_a_pattern`, `check_its_new`,
  `find_data_for_my_question`, `draft_my_pitch`, `help_me_fix_it`, `walk_me_through_install`. Their text lives in
  `helpers.py`; [docs/helpers.md](../helpers.md) is generated from it by `make docs`, and a test fails if the two
  differ. Never edit `docs/helpers.md` by hand.

When a tool's name, parameters, columns or refusals change in `server.py` or `tools.py`, update
[docs/tools.md](../tools.md) in the same PR; when a contract changes how a number is read (missing codes, total rows,
provisional status, uncertainty, places), update [docs/data-conventions.md](../data-conventions.md). Both are written
by hand, unlike `docs/helpers.md`.

Everything a clinician reads (tool descriptions, helpers, refusals) follows the writing rules in
[ADR 0012](../adr/0012-built-for-clinicians.md): plain words, a next step, never a stack trace or an HTTP code.

## Tests

- **Synthetic data only.** Unit, property (Hypothesis) and metamorphic tests run on small hand-written fixtures and
  synthetic lakes. No public data is committed.
- **Network tests.** `tests/integration/test_live_<dataset>.py` fetch a small slice from the publisher at test time,
  write nothing to disk and are marked `network`. `make test` and CI's test job leave them out; run
  `make test-network` when you change an adapter, a contract or the fetcher.
- **Mutation tests.** `scripts/mutate_changed.py` runs mutmut on the modules changed since the merge base and
  prints each surviving mutant. A survivor means the tests would not notice that change: add or sharpen a test, or
  say in the PR why the mutant is equivalent. `make mutate` is part of `make check`, and CI runs it on every PR.
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
