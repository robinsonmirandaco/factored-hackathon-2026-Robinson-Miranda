# bankagent

AI-first banking customer service agent with graded autonomy, risk scoring and human-in-the-loop.
Built for the Factored AI & Data Hackathon 2026.

Status: scaffold. The system runs end to end on synthetic data with a placeholder random scorer.
The challenge dataset, the trained model and the measured results are added in later steps.

## Requirements

- Docker with Docker Compose, or Python 3.12 for the local mode
- About 3 GB of free disk for the Docker image
- Free ports 8000 (API), 8501 (panel), 5432 (Postgres) and 6379 (Redis) when using Docker
- Optional: an Anthropic API key. Without a working LLM provider every turn uses the
  deterministic fallback, so the whole system still runs.

## Run with Docker (recommended)

```bash
cp .env.example .env    # optional; set ANTHROPIC_API_KEY to enable the LLM
docker compose up --build
```

The API seeds synthetic data on startup (`--seed 42`), then serves:

| Service | URL |
| --- | --- |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/health |
| Panel | http://localhost:8501 |

Compose runs the system on Postgres. `docker compose down` keeps the data volume.

## Run locally without Docker

SQLite is used for local runs and unit tests only; the system runs on Postgres under Docker Compose.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,panel]"
cp .env.example .env                                # optional, see Requirements
python -m app.cli.ingest synthetic --seed 42        # seeds SQLite, writes eval/reports/ingest_synthetic.json
uvicorn app.main:create_app --factory --port 8000   # http://localhost:8000/docs
```

Try it:

```bash
curl -s -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"customer_id": "C00001", "message": "What is my balance?"}'
```

The response includes `intent`, `outcome`, `autonomy_level`, `actions_taken` and a `trace_id`;
`GET /cases/{case_id}/trace` returns the audit events of that case.

## Test and evaluate

| Command | What it does | Output |
| --- | --- | --- |
| `make test` | Unit tests with coverage (integration tests excluded) | Terminal |
| `make lint` | ruff check and format check | Terminal |
| `make eval` | Runs the golden conversation cases in `eval/cases/` | `eval/reports/golden_report.md` |
| `python -m app.cli.ingest synthetic --seed 42` | Ingestion with validation and quarantine | `eval/reports/ingest_synthetic.json` |

Integration tests are marked `@pytest.mark.integration` and need the Docker Compose stack.
CI (`.github/workflows/ci.yml`) runs lint, tests, ingestion, the golden cases and the Docker build
on every push, and publishes `eval/reports/` as an artifact.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Status of database, scorer and LLM provider |
| POST | `/chat` | One customer turn |
| GET | `/cases/{case_id}` | Case state |
| GET | `/cases/{case_id}/trace` | Audit events of a case |
| GET | `/queue` | Cases waiting for a human |
| POST | `/cases/{case_id}/decision` | Human decision on an escalated case |
| GET | `/metrics` | Operational metrics from the audit log |

## Repository layout

| Path | Contents |
| --- | --- |
| `src/app/api/` | Thin FastAPI routers |
| `src/app/schemas/` | Pydantic input and output models |
| `src/app/services/` | Application logic (agent, tools, cases, ingestion) |
| `src/app/domain/` | Pure business rules (policy router, risk, PII redaction) |
| `src/app/adapters/` | Database, LLM client, scorer, ingestion adapters |
| `src/app/core/` | Settings, logging, errors, trace_id middleware |
| `config/policy.yaml` | Autonomy policy: risk bands, amount limits, action classes |
| `eval/cases/` | Golden conversation cases, one YAML per case |
| `panel/` | Streamlit panel for the human agent |

## License

MIT, see [LICENSE](LICENSE).
