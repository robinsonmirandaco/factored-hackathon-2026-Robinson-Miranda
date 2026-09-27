.PHONY: init install dev test lint up down migrate seed seed-synthetic eval density extract data report-data diff-backup

install:
	uv sync --frozen

# Creates .env from .env.example if missing and generates DOCUMENT_HASH_KEY and APP_DB_PASSWORD
# when they are empty or still the example; values already set are kept and none is printed.
# System python3, so it runs before uv sync and with Docker alone.
init:
	python3 src/app/cli/init_env.py

dev:
	uv run --frozen uvicorn app.main:create_app --factory --reload --port 8000

test:
	LLM_ENABLED=false uv run --frozen pytest -m "not integration" --cov=app --cov-report=term-missing

lint:
	uv run --frozen ruff check src pipeline tests && uv run --frozen ruff format --check src pipeline tests

up:
	docker compose up --build

# Keeps the Postgres volume. Wiping data is a deliberate, separate command.
down:
	docker compose down

# Applies db/migrations as the owner (ADMIN_DATABASE_URL) and sets the password of trazo_app.
migrate:
	uv run --frozen python -m app.cli.migrate

# Loads the serving cohort ($(DATA_DIR)/gold/cohort, built by make data) into the database of
# ADMIN_DATABASE_URL. A database holds one source: REPLACE=1 empties it first.
seed:
	uv run --frozen python -m app.cli.seed cohort $(if $(REPLACE),--replace)

# The synthetic fixture instead of the cohort, as in CI and a fresh compose stack.
seed-synthetic:
	uv run --frozen python -m app.cli.seed synthetic $(if $(REPLACE),--replace)

eval:
	LLM_ENABLED=false uv run --frozen python -m app.cli.eval eval/cases --out eval/reports

# Reads the full dataset under $(DATA_DIR)/raw (default ./data) and writes docs/reports/densidad.md.
density:
	uv run --frozen python -m pipeline.density

# Downloads the in-scope tables from S3 (read-only, AWS profile from .env) into $(DATA_DIR)/raw.
# Incremental: a partition already on disk with the same size and hash is skipped.
extract:
	uv run --frozen --group pipeline python -m pipeline.extract

# Bronze manifest, silver with quarantine, gold and the serving cohort under $(DATA_DIR), plus
# docs/reports/calidad.md and cohorte.md. Starts from an empty folder.
data: extract
	uv run --frozen python -m pipeline.run

# docs/reports/calidad.md and demanda.md from the silver and gold of the last make data; writes
# nothing under $(DATA_DIR).
report-data:
	uv run --frozen python -m pipeline.reports

# Downloads the in-scope tables of the backup prefix (read-only, incremental) into
# $(DATA_DIR)/backup, apart from raw/, and compares them with the current version:
# docs/reports/diferencias_versiones.md.
diff-backup:
	uv run --frozen --group pipeline python -m pipeline.diff_backup
