# Developer and CI entry points. `make help` lists the targets.
.DEFAULT_GOAL := help
.PHONY: help install lint fmt typecheck test test-network mutate docs kit check refresh serve check-contracts eval hooks contracts transform

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
	$(UV) run ruff check src tests eval
	$(UV) run ruff format --check src tests eval

fmt: ## Fix lint findings and format the code
	$(UV) run ruff check --fix src tests eval
	$(UV) run ruff format src tests eval

typecheck: ## Type check with mypy (strict, settings in pyproject.toml)
	$(UV) run mypy

test: ## Offline tests with coverage (fails under COV_FLOOR, default 85%)
	$(UV) run pytest -m "not network" --cov --cov-report=term-missing --cov-fail-under=$(COV_FLOOR)

test-network: ## Tests that hit live publishers (network marker)
	$(UV) run pytest -m network

mutate: ## Mutation tests on the modules changed since BASE (default origin/main); fails under MUTATION_FLOOR
	$(UV) run python scripts/mutate_changed.py --base $(BASE) --floor $(MUTATION_FLOOR)

docs: ## Regenerate docs/helpers.md, the template's skills, the plugin and the tool lists (README, docs/tools.md, CLAUDE.md)
	$(UV) run python scripts/write_template.py

kit: ## Build the Claude Projects kit into KIT_DIR (default dist/projects-kit, not committed)
	$(UV) run python scripts/write_template.py --kit $(KIT_DIR)

check: lint typecheck test mutate ## Everything CI runs: lint, typecheck, test, mutation tests on changed modules

refresh: ## Fetch every catalogued source into the local lake
	$(UV) run health-data-registry refresh --all

serve: ## Run the MCP server locally
	$(UV) run health-data-registry serve

contracts: ## Regenerate transform/ raw sources, staging models and model schemas from contracts/
	$(UV) run python -m health_data_registry.transform_gen

transform: ## dbt build (models and tests) on the synthetic fixtures in tests/fixtures/transform, as CI runs it
	$(UV) run python -m health_data_registry.transform_gen --check
	HDR_RAW_RECORDS=../tests/fixtures/transform/raw_records HDR_TRANSFORM_DB=target/ci.duckdb DBT_SEND_ANONYMOUS_USAGE_STATS=false \
		$(UV) run --directory transform dbt build --profiles-dir . --project-dir .

check-contracts: ## Check every source against its ODCS contract
	$(UV) run health-data-registry check-contracts

eval: ## Run the evaluation over the blind question sets
	$(UV) run health-data-registry eval
