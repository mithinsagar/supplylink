# SupplyLink — common tasks.
#
# `make help` lists everything. Targets are grouped: setup, quality, database,
# pipeline, and Docker.

.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON      ?= python3
VENV        ?= .venv
BIN         := $(VENV)/bin
COMPOSE     ?= docker compose
TEST_DB_URL ?= postgresql+psycopg://supplylink:supplylink_local_dev@localhost:5432/supplylink_test

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- setup -------------------------------------------------------------------

.PHONY: install
install: ## Create a virtualenv and install the project with dev extras
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"
	@echo "Done. Activate with: source $(BIN)/activate"

.PHONY: env
env: ## Create .env from the template if it does not exist
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")

# --- quality -----------------------------------------------------------------

.PHONY: lint
lint: ## Run ruff
	$(BIN)/ruff check src tests tools scripts migrations

.PHONY: format
format: ## Fix what ruff can fix automatically
	$(BIN)/ruff check --fix src tests tools scripts migrations
	$(BIN)/ruff format src tests tools scripts migrations

.PHONY: typecheck
typecheck: ## Run mypy
	$(BIN)/mypy

.PHONY: test
test: ## Run the full test suite
	SUPPLYLINK_DATABASE_URL=$(TEST_DB_URL) $(BIN)/pytest

.PHONY: test-unit
test-unit: ## Run only the unit tests (no database needed)
	SUPPLYLINK_DATABASE_URL=$(TEST_DB_URL) $(BIN)/pytest tests/unit

.PHONY: coverage
coverage: ## Run tests with a coverage report
	SUPPLYLINK_DATABASE_URL=$(TEST_DB_URL) $(BIN)/pytest --cov --cov-report=term-missing

.PHONY: check
check: lint typecheck test ## Everything CI runs

# --- database ----------------------------------------------------------------

.PHONY: db-up
db-up: ## Start only PostgreSQL
	$(COMPOSE) up -d postgres

.PHONY: migrate
migrate: ## Apply migrations to head
	$(BIN)/alembic upgrade head

.PHONY: migrate-down
migrate-down: ## Roll back the last migration
	$(BIN)/alembic downgrade -1

.PHONY: seed
seed: ## Load supplier reference data
	$(BIN)/supplylink db seed

.PHONY: test-db
test-db: ## Create the test database and its extension
	createdb supplylink_test 2>/dev/null || true
	psql -d supplylink_test -c "CREATE EXTENSION IF NOT EXISTS btree_gist;"

.PHONY: reset
reset: ## Drop and rebuild the schema, then reseed (destroys local data)
	$(BIN)/alembic downgrade base
	$(BIN)/alembic upgrade head
	$(BIN)/supplylink db seed

# --- pipeline ----------------------------------------------------------------

.PHONY: sample-data
sample-data: ## Regenerate the synthetic dataset
	$(BIN)/python scripts/generate_sample_data.py

.PHONY: demo
demo: ## Run the full scripted demo end to end
	$(BIN)/python scripts/demo.py

.PHONY: run-erp run-acme run-api
run-erp: ## Run the ERP pipeline
	$(BIN)/supplylink run --source erp_core

run-acme: ## Run the ACME supplier pipeline
	$(BIN)/supplylink run --source acme_supplier

run-api: ## Run the GlobalParts vendor API pipeline
	$(BIN)/supplylink run --source globalparts_api

.PHONY: serve
serve: ## Serve the API on :8000 with reload
	$(BIN)/uvicorn supplylink.api:app --reload --port 8000

.PHONY: vendor-sim
vendor-sim: ## Serve the vendor API simulator on :8081
	PYTHONPATH=. $(BIN)/uvicorn tools.vendor_sim.app:app --reload --port 8081

# --- docker ------------------------------------------------------------------

.PHONY: up
up: env ## Start the whole stack
	$(COMPOSE) up -d --build
	@echo "API      http://localhost:8000/docs"
	@echo "Vendor   http://localhost:8081/health"

.PHONY: down
down: ## Stop the stack
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stack and delete its data volume
	$(COMPOSE) down -v

.PHONY: logs
logs: ## Follow application logs
	$(COMPOSE) logs -f app

.PHONY: shell
shell: ## Open psql against the running database
	$(COMPOSE) exec postgres psql -U supplylink -d supplylink
