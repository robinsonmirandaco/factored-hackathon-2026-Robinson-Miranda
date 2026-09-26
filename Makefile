.PHONY: install dev test lint up down ingest eval density extract data

install:
	uv sync --frozen

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

ingest:
	uv run --frozen python -m app.cli.ingest $(SOURCE) $(ARGS)

eval:
	LLM_ENABLED=false uv run --frozen python -m app.cli.eval eval/cases --out eval/reports

# Reads the full dataset under $(DATA_DIR)/raw (default ./data) and writes docs/reports/densidad.md.
density:
	uv run --frozen python -m pipeline.density

# Downloads the in-scope tables from S3 (read-only, AWS profile from .env) into $(DATA_DIR)/raw.
# Incremental: a partition already on disk with the same size and hash is skipped.
extract:
	uv run --frozen --group pipeline python -m pipeline.extract

# Bronze manifest, silver with quarantine, and gold under $(DATA_DIR). Starts from an empty folder.
data: extract
	uv run --frozen python -m pipeline.run
