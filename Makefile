# Developer and CI entry points. `make help` lists the targets.
.DEFAULT_GOAL := help
.PHONY: help install lint fmt typecheck test test-network smoke mutate docs kit fuzz-requirements check precheck refresh serve check-contracts eval hooks serve-http contracts transform

UV ?= uv
COV_FLOOR ?= 85
MUTATION_FLOOR ?= 70
BASE ?= origin/main
KIT_DIR ?= dist/projects-kit

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install the locked environment, dev tools included (uv sync)
	$(UV) sync

hooks: ## Install the git hooks (pre-commit and pre-push) from .pre-commit-config.yaml
	$(UV) run pre-commit install --hook-type pre-commit --hook-type pre-push

lint: ## Lint and check formatting (ruff check, ruff format --check)
	$(UV) run ruff check src tests eval tools
	$(UV) run ruff format --check src tests eval tools

fmt: ## Fix lint findings and format the code
	$(UV) run ruff check --fix src tests eval tools
	$(UV) run ruff format src tests eval tools

typecheck: ## Type check with mypy (strict, settings in pyproject.toml)
	$(UV) run mypy

test: ## Offline tests with coverage (fails under COV_FLOOR, default 85%)
	$(UV) run pytest -m "not network" --cov --cov-report=term-missing --cov-fail-under=$(COV_FLOOR)

test-network: ## Tests that hit live publishers (network marker)
	$(UV) run pytest -m network

smoke: ## The C. auris in Michigan journey on a synthetic lake, scored with eval/scorer.py (smoke marker; offline)
	$(UV) run pytest -m smoke tests/smoke -rxX

mutate: ## Mutation tests on the modules changed since BASE (default origin/main); fails under MUTATION_FLOOR
	$(UV) run python scripts/mutate_changed.py --base $(BASE) --floor $(MUTATION_FLOOR)

docs: ## Regenerate docs/helpers.md, the template's skills, the plugin and the tool lists (README, docs/tools.md, CLAUDE.md)
	$(UV) run python scripts/write_template.py

kit: ## Build the Claude Projects kit into KIT_DIR (default dist/projects-kit, not committed)
	$(UV) run python scripts/write_template.py --kit $(KIT_DIR)

fuzz-requirements: ## Regenerate fuzz/requirements.txt from uv.lock (hash-pinned, for the fuzz image's Python 3.11); needs PyPI
	$(UV) run python scripts/fuzz_requirements.py write

check: lint typecheck test mutate ## The quick local loop (not all of CI): lint, typecheck, test, mutation tests on changed modules

precheck: ## The pre-PR gate's local half: the CI checks plus docs drift (hooks, lint, types, tests, smoke, kit, docs drift, mutation shards); stops at the first failure
	UV="$(UV)" MAKE="$(MAKE)" BASE="$(BASE)" COV_FLOOR="$(COV_FLOOR)" MUTATION_FLOOR="$(MUTATION_FLOOR)" bash scripts/precheck.sh

refresh: ## Fetch every catalogued source into the local lake
	$(UV) run health-data-registry refresh --all

serve: ## Run the MCP server locally
	$(UV) run health-data-registry serve

serve-http: ## Run the hosted read-only server over Streamable HTTP on port 7860 (docs/dev/hosting.md)
	$(UV) run health-data-registry serve --http --host 127.0.0.1 --port 7860

contracts: ## Regenerate transform/ raw sources, staging models and model schemas from contracts/
	$(UV) run python -m health_data_registry.transform_gen

transform: ## dbt build (models and tests) on the synthetic fixtures in tests/fixtures/transform, as CI runs it
	$(UV) run python -m health_data_registry.transform_gen --check
	HDR_LAKE=../tests/fixtures/transform HDR_TRANSFORM_DB=target/ci.duckdb DBT_SEND_ANONYMOUS_USAGE_STATS=false \
		$(UV) run --directory transform dbt build --profiles-dir . --project-dir .

check-contracts: ## Check every source against its ODCS contract
	$(UV) run health-data-registry check-contracts

eval: ## Run the evaluation over the blind question sets
	$(UV) run health-data-registry eval
