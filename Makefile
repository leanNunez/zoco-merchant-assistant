# One command per step. `make check` is what CI runs, so green here means green there.

.DEFAULT_GOAL := help
.PHONY: help setup format lint typecheck arch test diagram index eval-retrieval dataset check

help: ## List the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

setup: ## Install dependencies from the lockfile and the git hooks
	uv sync --locked
	uv run pre-commit install

format: ## Apply formatting and safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Lint and check formatting (no changes)
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## mypy in strict mode
	uv run mypy

arch: ## Architecture contracts (import-linter)
	uv run lint-imports

# Run twice on purpose: a test that depends on the machine's timezone passes in
# one run and fails in the other.
test: ## Unit and integration tests, in the local timezone and in UTC
	uv run pytest
	TZ=UTC uv run pytest

diagram: ## Regenerate the state diagram
	uv run python -m merchant_assistant.control.diagram > design/state-diagram.md

index: ## Embed the corpus with every chunking (uses the cache)
	uv run python -m evaluation.retrieval index

eval-retrieval: ## Retrieval evaluation: recall@3 and MRR tables
	uv run python -m evaluation.retrieval run

dataset: ## Regenerate data/ventas.json
	uv run python scripts/generate_ventas.py

check: lint typecheck arch test ## Everything CI runs
