# Firebreak developer commands. See SPEC.md Section 13.1.
.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup verify lint format types test test-cov hygiene leakage clean unhide \
        live live-config live-down live-logs lab-flags lab-library lab-bundles lab-package lab-verify lab-smoke lab-webhook \
        graph-up graph-down graph-logs graph-load graph-check knowledge measure-ranking baseline-b0 compare-log-templates eval-b0

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

# Invoking the CLI through PYTHONPATH rather than the installed console
# script, because uv rebuilds the editable install on every run and some
# macOS tooling re-applies the hidden flag to the .pth file each time, which
# CPython's site module then skips. See the unhide target.
FIREBREAK := PYTHONPATH=src uv run firebreak

unhide:
	@# Some tooling on macOS writes .pth files with the hidden flag set, and
	@# CPython's site module skips hidden .pth files, so the editable install
	@# silently stops resolving. Clearing it is cheap, and a no-op elsewhere.
	@if [ "$$(uname)" = "Darwin" ]; then \
		chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null || true; \
	fi

verify: unhide lint types test hygiene leakage  ## Run every check the review gate expects

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

leakage:  ## Prove the agent has no route to ground truth
	uv run python scripts/check_leakage.py

clean:  ## Remove build and test artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} +

# The demo's own Compose files first, then Firebreak's overlay, which takes
# the place of the demo's empty compose.extras.yaml. compose.full.yaml is
# not optional: it carries Kafka, fraud-detection, and accounting, and the
# observability profile's collector config scrapes Kafka whether or not the
# broker is there. The project directory is fixed so relative paths inside
# every file resolve the same way no matter where make is run from. See
# docs/target-system.md.
#
# OTEL_COLLECTOR_CONFIG_EXTRAS is the seam upstream documents for forks. The
# demo's .env points it at an empty vendored file; this points it at
# Firebreak's service graph configuration instead. The path is relative to the
# project directory, which is vendor/otel-demo. A shell variable wins over the
# .env file during Compose interpolation.
#
# DEMO_VERSION pins the container images to the same release as the
# submodule. The demo's own .env sets it to "latest", which would mean the
# code is pinned and the images are not, and two recordings made weeks
# apart would not be comparable.
DEMO_TAG := 3.1.0
COMPOSE_ENV := OTEL_COLLECTOR_CONFIG_EXTRAS=../../ops/otelcol-config-extras.yml \
               DEMO_VERSION=$(DEMO_TAG)
COMPOSE_CORE := docker compose -f ops/compose.core.yml
COMPOSE_LIVE := $(COMPOSE_ENV) docker compose --project-directory vendor/otel-demo \
	-f vendor/otel-demo/compose.yaml \
	-f vendor/otel-demo/compose.full.yaml \
	-f vendor/otel-demo/compose.observability.yaml \
	-f ops/compose.live.yml

live-config:  ## Render the Prometheus config the live stack mounts
	@test -f vendor/otel-demo/compose.yaml || \
		(echo "submodule missing; run: git submodule update --init --recursive"; exit 1)
	$(FIREBREAK) lab render-config

live: live-config  ## Start the pinned OpenTelemetry Demo with Firebreak's overlay
	@docker info >/dev/null 2>&1 || (echo "Docker is not running"; exit 1)
	$(COMPOSE_LIVE) up -d
	@echo "shop      http://localhost:8080"
	@echo "flags     http://localhost:8080/feature"
	@echo "load      http://localhost:8080/loadgen/"
	@echo "prom      http://localhost:9090"
	@echo "jaeger    http://localhost:16686"
	@echo "alerts    http://localhost:9093"

live-down:  ## Stop the live stack and remove its volumes
	$(COMPOSE_LIVE) down -v

live-logs:  ## Follow the live stack logs
	$(COMPOSE_LIVE) logs -f --tail=100

lab-library:  ## Validate the scenario library and write its summary
	$(FIREBREAK) lab library

lab-package:  ## Archive the recorded library with checksums for release
	$(FIREBREAK) lab package

lab-bundles:  ## Check every recorded bundle still matches its manifest
	$(FIREBREAK) lab verify-bundles

lab-verify:  ## Check the running stack is fit to record incidents from
	$(FIREBREAK) lab verify

lab-flags:  ## Write the pinned demo's flag inventory to a report
	$(FIREBREAK) lab flags inventory

lab-smoke:  ## Turn each feature flag on in turn and record the effect
	$(FIREBREAK) lab smoke

lab-webhook:  ## Receive Alertmanager deliveries on port 8000
	$(FIREBREAK) lab webhook

# --- Knowledge graph -------------------------------------------------
#
# Neo4j is separate from the live demo on purpose. Bundle replay never
# touches it, so the ordinary test run needs no container, and tearing the
# demo down does not take the graph with it.

graph-up:  ## Start Neo4j and wait until it answers queries
	@docker info >/dev/null 2>&1 || (echo "Docker is not running"; exit 1)
	$(COMPOSE_CORE) up -d
	@echo "waiting for Neo4j to accept queries..."
	@for i in $$(seq 1 40); do \
		status=$$(docker inspect -f '{{.State.Health.Status}}' firebreak-neo4j 2>/dev/null); \
		if [ "$$status" = "healthy" ]; then echo "Neo4j ready on bolt://localhost:7687"; exit 0; fi; \
		sleep 3; \
	done; \
	echo "Neo4j did not become healthy; check: make graph-logs"; exit 1

graph-down:  ## Stop Neo4j and remove its volume
	$(COMPOSE_CORE) down -v

graph-logs:  ## Follow the Neo4j logs
	$(COMPOSE_CORE) logs -f --tail=100

knowledge:  ## Validate the hand written knowledge files
	$(FIREBREAK) graph knowledge

graph-load:  ## Apply the schema and load the knowledge files into Neo4j
	$(FIREBREAK) graph load

graph-check:  ## Prove a second load changes nothing
	$(FIREBREAK) graph check

# --- Measurement -----------------------------------------------------

measure-ranking:  ## Re-measure candidate ranking and rewrite its reports
	PYTHONPATH=src uv run python scripts/measure_ranking.py

# Three trials per task, as SPEC.md Section 9.3 specifies for pass^3. B0 has
# no sampling in it, so its pass^3 equals its pass@1; that is a true statement
# about a deterministic system rather than a shortcut, and it is worth having
# in the report as the reference every later configuration is compared to.
eval-b0:  ## Run baseline B0 on every split and write the reports
	$(FIREBREAK) eval run --config b0 --split validation --trials 3
	$(FIREBREAK) eval run --config b0 --split test_id --trials 3
	$(FIREBREAK) eval run --config b0 --split test_ood --trials 3

compare-log-templates:  ## Re-decide ADR-0006 by comparing the masker against Drain
	PYTHONPATH=src uv run python scripts/compare_log_templates.py

baseline-b0:  ## Run deterministic triage over every bundle and score it
	PYTHONPATH=src uv run python scripts/run_baseline_b0.py
