# Developer and CI entry points. `make help` lists the targets.
.DEFAULT_GOAL := help
.PHONY: help install lint fmt typecheck test test-network mutate docs kit check precheck refresh serve check-contracts eval hooks

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

precheck: ## The pre-PR gate: every CI step locally (hooks, lint, types, tests, kit, docs drift, mutation shards); stops at the first failure
	UV="$(UV)" MAKE="$(MAKE)" BASE="$(BASE)" COV_FLOOR="$(COV_FLOOR)" MUTATION_FLOOR="$(MUTATION_FLOOR)" bash scripts/precheck.sh

refresh: ## Fetch every catalogued source into the local lake
	$(UV) run health-data-registry refresh --all

serve: ## Run the MCP server locally
	$(UV) run health-data-registry serve

check-contracts: ## Check every source against its ODCS contract
	$(UV) run health-data-registry check-contracts

eval: ## Run the evaluation over the blind question sets
	$(UV) run health-data-registry eval
