# TRAZO

An AI-first intake assistant for disputed card transactions, built for the Factored AI & Data Hackathon 2026. The LLM understands and drafts; code decides and acts; statistics define how much autonomy the system earns.

## Status

Prototype in progress. The service runs end to end on synthetic data. A customer turn follows the dispute workflow of the design up to the decision: the LLM reads the intent and the clues (the rules baseline answers when it fails), the charge is identified with a conformal set, and the business policy of `config/policy.yaml` decides. Registering a dispute or blocking a card runs only after the customer confirms the pending action.

Not built yet: the recognition step before deciding, dispute folios and the read-back check after acting, the analyst dossier and queue with priority, autonomy levels per cell (every cell starts at A0) and authentication. `make eval` runs 22 golden cases, none skipped.

## Requirements

- Docker with Docker Compose.
- [uv](https://docs.astral.sh/uv/) for local development (the lock file was produced with uv 0.11.31).
- Free ports: 8000 (API) and 5432 (Postgres).
- Optional: an Anthropic API key in `ANTHROPIC_API_KEY`. Without it, every turn falls back to deterministic rules and templates, and `/health` reports `llm_available: false`.

## Run with Docker (recommended)

```bash
make init
docker compose up --build
```

`make init` is required: it creates `.env` from `.env.example` and generates `DOCUMENT_HASH_KEY` and `APP_DB_PASSWORD`, which Compose needs along with `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB`. Values already set in `.env` are kept. Set `ANTHROPIC_API_KEY` in `.env` to enable the LLM.

The `db` service starts first. The one-shot `migrate` service then applies the migrations and loads the synthetic fixture (`seed synthetic --seed 42`) only if the database is empty, and the `api` service serves:

| Service | URL |
| --- | --- |
| API docs | http://localhost:8000/docs |
| Health | http://localhost:8000/health |

`docker compose down` stops the services and keeps the database volume.

## Run the API locally

The API runs on your machine against the Postgres service from Compose. `--wait` returns only when Postgres is healthy, so the next commands can connect:

```bash
make init
uv sync
docker compose up -d --wait db
make migrate
uv run python -m app.cli.seed synthetic --seed 42
uv run uvicorn app.main:create_app --factory --port 8000
```

Then open http://localhost:8000/docs.

## Try it

**Warning:** the endpoints do not have authentication yet, and `/chat` still takes `customer_id` in the request body. Session-based authentication arrives with TRZ-09. Do not expose this service on a public URL before then.

The examples use customer `C00001` of the synthetic fixture (`make seed-synthetic`). Messages are in Spanish or Portuguese.

A charge the customer does not recognize. The policy decides; nothing runs until the customer confirms:

```bash
curl -s -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"customer_id": "C00001", "message": "No reconozco un cargo de 40.92 dólares en Claro"}'
```

The response has `outcome` `awaiting_confirmation` and a `case_id`. Confirm the pending action of that case:

```bash
curl -s -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"customer_id": "C00001", "message": "sí", "case_id": "<case_id>", "confirm": true}'
```

The outcome is `registered` and `actions_taken` is `["open_dispute"]`. A request outside disputes, such as `"¿Cuál es mi saldo?"`, gets `outcome` `abstained` and no action.

Every response includes `intent`, `outcome`, `autonomy_level`, `actions_taken` and a `trace_id`. `GET /cases/{case_id}/trace` returns the audit events of that case, including the policy rule and version behind each decision.

## Test and evaluate

| Command | What it does | Output |
| --- | --- | --- |
| `make test` | Unit tests with coverage; they do not need a database | Terminal |
| `make lint` | ruff check and format check | Terminal |
| `uv run pytest -m integration` | Integration tests against Postgres | Terminal |
| `make eval` | Runs the golden conversation cases in `eval/cases/` | `eval/reports/golden_report.md` |
| `uv run python -m app.cli.seed synthetic --seed 42` | Ingestion with validation and quarantine | `eval/reports/ingest_synthetic.json` |
| `make report-data` | Rebuilds the quality and demand reports from silver and gold | `docs/reports/calidad.md`, `docs/reports/demanda.md` |
| `make diff-backup` | Compares the current data against the organizers' earlier backup | `docs/reports/diferencias_versiones.md` |

Integration tests and golden cases need the Postgres service (`docker compose up -d --wait db`) and read `DATABASE_URL` and `ADMIN_DATABASE_URL` from `.env`. Each golden case and each integration test runs in its own temporary schema, so they never touch existing tables.

CI (`.github/workflows/ci.yml`) runs lint, unit and integration tests against a Postgres service, ingestion, the golden cases and the Docker build on every push and pull request, and publishes `eval/reports/` as an artifact.

## Data pipeline

`make data` prepares the LATAM Bank data in three layers under `DATA_DIR` (outside git). It first runs `make extract`, so it can start from an empty folder.

- **Bronze**: the CSV files downloaded from S3, kept exactly as delivered. `manifest/bronze.json` records the path, partition, size, SHA-256, load time and header of every file.
- **Silver**: typed Parquet per table and partition. Every row is checked against its table's contract (`pipeline/contracts.py`); a row that breaks a rule goes to `quarantine/` with the rule, column, value, source file and partition, never silently dropped. For every table, bronze rows equal silver rows plus quarantine rows, or the run fails.
- **Gold**: demand marts, the case generator input and the serving tables.

`docs/reports/calidad.md` is regenerated on each run with counts per rule, alerts, duplicates, nulls, schema evolution and the time rule. Running `make data` twice gives the same output hashes (`manifest/outputs.json`).

`make report-data`   # quality and demand reports from silver and gold
`make diff-backup`   # partitions, row counts and shared ids against data_backup_20260831

### Time in the data

Timestamps carry no time zone. Each partition is an operational day with a fixed cut-off per table (transactions run from 06:00 to 06:00 of the next day; complaints and interactions from 08:00 to 08:00), the same in all three countries. Timestamps are read as local time of the customer's country; this is an assumption, since the data does not say. The partition date is kept as lineage.

### Tables left out

Three of the thirteen tables are never downloaded or processed: `digital_events`, `campaign_sends` and `marketing_campaigns`. TRAZO handles the intake of disputed card and account charges; marketing campaigns say nothing about a charge, a customer's products or how a complaint was handled. `digital_events` could add fraud signals, such as the country of the IP address against the country of the transaction, but that belongs to a later investigation step, not to intake.

### Freshness policy

The source is a static export of daily partitions, so the pipeline is built as if new days kept arriving:

- **Incremental by partition.** `state/partitions.json` records, for every bronze file, a fingerprint of its content and of what it depends on. A partition already loaded is not read again unless its file, a snapshot table (customers, products, branches, agents, exchange rates), the interactions of the same day, or the pipeline version changed.
- **Reprocessing window.** The partitions of the last 7 days before the latest one are always read again, to take in late corrections. The window is set with `PIPELINE_REPROCESS_DAYS`.
- **Idempotent.** Reading a partition replaces its output folder, so it never duplicates rows. An id that already exists in another partition goes to quarantine as a duplicate. A full load and an incremental run over the same input produce identical files.
- **Late partitions** (an old day delivered after newer ones) are loaded on the next run because they are new, even outside the window.
- **Schema changes.** A partition whose header differs from the contract goes whole to quarantine as `schema_mismatch`, and the rest of the load continues.
- **Lineage.** Every silver and gold row carries `source_file`, `partition_date`, `batch_id`, `ingested_at` and `pipeline_version`.

Because the data never changes, `tests/fixtures/update/` (test data, not real data) simulates a late partition and a partition with a new column; `tests/integration/test_update_fixture.py` checks both.

### Commands

```bash
make extract   # S3 -> DATA_DIR/raw, read-only, AWS profile from .env; skips unchanged files
make data      # extract, then bronze manifest, silver, quarantine, gold and the quality report
```

On the full dataset, a full load takes 3 to 4 minutes and a run that only reads the window about 1.5 minutes. Silver and gold take about 0.75 GB next to the 1.2 GB of bronze.

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
| `config/policy.yaml` | Business policy of design section 8 (version 2026.09.1): USD amount bands, security, escalation and approval rules, routing per intent and the level of each action. Code applies it; the LLM never reads it |
| `eval/cases/` | Golden conversation cases, one YAML per case |
| `web/` | Reserved for the customer and back-office web app |
| `tests/unit/` | Unit tests, no database |
| `tests/integration/` | Integration tests against Postgres |

## License

MIT, see [LICENSE](LICENSE).
