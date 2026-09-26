.PHONY: install dev test lint up down ingest eval

install:
	uv sync --frozen

dev:
	uv run --frozen uvicorn app.main:create_app --factory --reload --port 8000

test:
	LLM_ENABLED=false uv run --frozen pytest -m "not integration" --cov=app --cov-report=term-missing

lint:
	uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests

up:
	docker compose up --build

# Keeps the Postgres volume. Wiping data is a deliberate, separate command.
down:
	docker compose down

ingest:
	uv run --frozen python -m app.cli.ingest $(SOURCE) $(ARGS)

eval:
	LLM_ENABLED=false uv run --frozen python -m app.cli.eval eval/cases --out eval/reports
