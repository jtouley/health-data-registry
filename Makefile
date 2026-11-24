# Developer and CI entry points. `make help` lists the targets.
.DEFAULT_GOAL := help
.PHONY: help install lint fmt typecheck test test-network check refresh serve check-contracts eval mutation

UV ?= uv
COV_FLOOR ?= 85

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install the locked environment, dev tools included (uv sync)
	$(UV) sync

lint: ## Lint and check formatting (ruff check, ruff format --check)
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

fmt: ## Fix lint findings and format the code
	$(UV) run ruff check --fix src tests
	$(UV) run ruff format src tests

typecheck: ## Type check with mypy (strict, settings in pyproject.toml)
	$(UV) run mypy

test: ## Offline tests with coverage (fails under COV_FLOOR, default 85%)
	$(UV) run pytest -m "not network" --cov --cov-report=term-missing --cov-fail-under=$(COV_FLOOR)

test-network: ## Tests that hit live publishers (network marker)
	$(UV) run pytest -m network

check: lint typecheck test ## Everything CI runs: lint, typecheck, test

refresh: ## Fetch every catalogued source into the local lake
	$(UV) run health-data-registry refresh --all

serve: ## Run the MCP server locally
	$(UV) run health-data-registry serve

check-contracts: ## Check every source against its ODCS contract
	$(UV) run health-data-registry check-contracts

eval: ## Run the evaluation over the blind question sets
	$(UV) run health-data-registry eval

mutation: ## Mutation tests on the PDF report parser (mutmut; scope in pyproject [tool.mutmut])
	$(UV) run mutmut run --max-children 3
