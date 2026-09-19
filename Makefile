# Firebreak developer commands. See SPEC.md Section 13.1.
.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup verify lint format types test test-cov hygiene clean

help:  ## Show the available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

setup:  ## Install dependencies, git hooks, and a starter .env
	uv sync
	@# macOS marks files written here with the hidden flag, and CPython's site
	@# module skips hidden .pth files, which breaks the editable install.
	@if [ "$$(uname)" = "Darwin" ]; then \
		chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null || true; \
	fi
	uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
	@test -f .env || cp .env.example .env
	@echo "setup complete; edit .env if you need non-default settings"

verify: lint types test hygiene  ## Run every check the review gate expects

lint:  ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format:  ## Apply formatting and safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

types:  ## Type check src and scripts with mypy strict
	uv run mypy src scripts

test:  ## Run the test suite with the coverage floor
	uv run pytest --cov --cov-report=term-missing

test-cov:  ## Run tests and write an HTML coverage report
	uv run pytest --cov --cov-report=html
	@echo "open htmlcov/index.html"

hygiene:  ## Run the repository hygiene checks
	uv run python scripts/check_repo_hygiene.py

clean:  ## Remove build and test artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +
