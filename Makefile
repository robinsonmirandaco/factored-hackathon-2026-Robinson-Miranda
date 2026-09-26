.PHONY: install dev test lint up down ingest eval

install:
	uv pip install -e ".[dev]" || pip install -e ".[dev]"

dev:
	uvicorn app.main:create_app --factory --reload --port 8000

test:
	LLM_ENABLED=false pytest -m "not integration" --cov=app --cov-report=term-missing

lint:
	ruff check src tests && ruff format --check src tests

up:
	docker compose up --build

# Keeps the Postgres volume. Wiping data is a deliberate, separate command.
down:
	docker compose down

ingest:
	python -m app.cli.ingest $(SOURCE) $(ARGS)

eval:
	LLM_ENABLED=false python -m app.cli.eval eval/cases --out eval/reports
