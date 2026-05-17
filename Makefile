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

check: format lint typecheck test ## Run all formatting, linting, typechecking, and tests

clean: ## Remove python caches, virtual envs, and build artifacts
	rm -rf .venv/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	find . -type d -name "__pycache__" -exec rm -rf {} +
