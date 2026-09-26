# TRAZO

An AI-first intake assistant for disputed card transactions, built for the Factored AI & Data Hackathon 2026. The LLM understands and drafts; code decides and acts; statistics define how much autonomy the system earns.

## Status

Scaffold. The service runs end to end on synthetic data, aligned with the target architecture. It does not yet use the challenge dataset or the dispute workflow of the design. 8 of the 13 golden cases are skipped until the dispute policy is rewritten (TRZ-17); `make eval` reports them as skipped with that reason.

## Requirements

- Docker with Docker Compose.
- [uv](https://docs.astral.sh/uv/) for local development (the lock file was produced with uv 0.11.31).
- Free ports: 8000 (API) and 5432 (Postgres).
- Optional: an Anthropic API key in `ANTHROPIC_API_KEY`. Without it, every turn falls back to deterministic rules and templates, and `/health` reports `llm_available: false`.

## Run with Docker (recommended)

```bash
cp .env.example .env
docker compose up --build
```

Copying `.env.example` is required: Compose needs `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` to start. Set `ANTHROPIC_API_KEY` in `.env` to enable the LLM.

The `db` service starts first. The `api` service then loads the synthetic fixture (`ingest synthetic --seed 42`) and serves:

| Service | URL |
| --- | --- |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/health |

`docker compose down` stops the services and keeps the database volume.

## Run the API locally

The API runs on your machine against the Postgres service from Compose. `--wait` returns only when Postgres is healthy, so the next commands can connect:

```bash
cp .env.example .env
uv sync
docker compose up -d --wait db
uv run python -m app.cli.ingest synthetic --seed 42
uv run uvicorn app.main:create_app --factory --port 8000
```

Then open http://localhost:8000/docs.

## Try it

**Warning:** the endpoints do not have authentication yet, and `/chat` still takes `customer_id` in the request body. Session-based authentication arrives with TRZ-09. Do not expose this service on a public URL before then.

```bash
curl -s -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"customer_id": "C00001", "message": "What is my balance?"}'
```

The response includes `intent`, `outcome`, `autonomy_level`, `actions_taken` and a `trace_id`. `GET /cases/{case_id}/trace` returns the audit events of that case.

## Test and evaluate

| Command | What it does | Output |
| --- | --- | --- |
| `make test` | Unit tests with coverage; they do not need a database | Terminal |
| `make lint` | ruff check and format check | Terminal |
| `uv run pytest -m integration` | Integration tests against Postgres | Terminal |
| `make eval` | Runs the golden conversation cases in `eval/cases/` | `eval/reports/golden_report.md` |
| `uv run python -m app.cli.ingest synthetic --seed 42` | Ingestion with validation and quarantine | `eval/reports/ingest_synthetic.json` |

Integration tests and golden cases need the Postgres service (`docker compose up -d --wait db`) and read `DATABASE_URL` from `.env`. Each golden case and each integration test runs in its own temporary schema, so they never touch existing tables.

CI (`.github/workflows/ci.yml`) runs lint, unit and integration tests against a Postgres service, ingestion, the golden cases and the Docker build on every push and pull request, and publishes `eval/reports/` as an artifact.

## Synthetic data

The synthetic generator creates customers, transactions and messages, including deliberately invalid rows, to exercise validation and quarantine. It is a fixture for CI and Compose, not the challenge dataset. The challenge data is never stored in this repository.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Status of the app, database and LLM provider |
| POST | `/chat` | One customer turn |
| GET | `/cases/{case_id}` | Case state |
| GET | `/cases/{case_id}/trace` | Audit events of a case |
| GET | `/queue` | Cases waiting for a human |
| POST | `/cases/{case_id}/decision` | Human decision on an escalated case |
| GET | `/metrics` | Operational metrics |

`/health` returns `status`, `app_env`, `db`, `llm_provider` and `llm_available`.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/app/api/` | Thin FastAPI routers |
| `src/app/schemas/` | Pydantic input and output models |
| `src/app/services/` | Application logic (agent, tools, cases, ingestion) |
| `src/app/domain/` | Pure business rules (policy, PII redaction) |
| `src/app/adapters/` | Database, LLM client and ingestion adapters |
| `src/app/core/` | Settings, logging, errors, trace_id middleware |
| `config/policy.yaml` | Autonomy policy: amount limits, intent ceilings, action classes and hard escalation rules |
| `eval/cases/` | Golden conversation cases, one YAML per case |
| `web/` | Reserved for the customer and back-office web app |
| `tests/unit/` | Unit tests, no database |
| `tests/integration/` | Integration tests against Postgres |

## License

MIT, see [LICENSE](LICENSE).
