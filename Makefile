# One command per step. `make check` is what CI runs, so green here means green there.

.DEFAULT_GOAL := help
.PHONY: help setup format lint typecheck arch test diagram index eval-retrieval eval-agent eval-agent-live eval-agent-heldout dataset db chat check

help: ## List the available commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-16s %s\n", $$1, $$2}'

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

index: ## Embed the corpus, the kit and the calibration queries (uses the cache)
	uv run python -m evaluation.retrieval index

eval-retrieval: ## Retrieval evaluation (recall@3, MRR) and the calibration of the no-data gate
	uv run python -m evaluation.retrieval run
	uv run python -m evaluation.retrieval calibrate

# The scripted report validates the evaluator with the null model and the naive agent, so the three
# modes always come from the same code; each writes evaluation/results/conversations/<mode>/.
eval-agent: ## Conversation evaluation with the scripted models (offline, free, reproducible)
	uv run python -m evaluation.conversations run --mode null
	uv run python -m evaluation.conversations run --mode naive
	uv run python -m evaluation.conversations run --mode scripted

# The dev split is what the prompt is tuned against. The held-out split is the unbiased
# measurement: run it once per accepted prompt, never to iterate (ADR-0008).
eval-agent-live: ## Real-model evaluation on the dev split (needs a key; spends credit; capped)
	uv run python -m evaluation.conversations run --mode live --split dev

eval-agent-heldout: ## Real-model evaluation on the held-out split (needs a key; run once, never iterate)
	uv run python -m evaluation.conversations run --mode live --split heldout

dataset: ## Regenerate data/ventas.json
	uv run python scripts/generate_ventas.py

db: ## Recreate the initial claims database
	uv run python scripts/init_reclamos_db.py

chat: ## Open the console (customer, operator and supervisor views)
	uv run python -m merchant_assistant.cli

check: lint typecheck arch test ## Everything CI runs
