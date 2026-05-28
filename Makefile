.PHONY: help install format lint typecheck test check clean

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies and dev tools using uv
	uv sync --extra dev

format: ## Auto-format Python code using ruff
	uv run ruff check --fix .
	uv run ruff format .

lint: ## Run ruff to check for linting errors
	uv run ruff check .

typecheck: ## Run mypy for strict type checking
	uv run mypy src/ tests/

test: ## Run the pytest suite
	uv run pytest -q

perf: ## Run slow performance/memory tests
	uv run pytest -q -m slow tests/slow

format-check: ## Verify formatting without modifying files
	uv run ruff format --check .

check: lint typecheck test ## Run linting, typechecking, and tests (no auto-format)

ci: format-check lint typecheck test ## CI gate: format check + lint + types + tests

clean: ## Remove python caches, virtual envs, and build artifacts
	rm -rf .venv/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	find . -type d -name "__pycache__" -exec rm -rf {} +
