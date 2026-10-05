.PHONY: init install dev test test-web lint up down migrate seed seed-synthetic seed-demo jobs expire-info-requests send-email purge golden eval eval-run eval-sensitivity eval-degradation eval-analysis eval-ablations load report-load figures density extract data report-data report-impact diff-backup cases cases-template cases-check cases-review cases-agreement eval-comprehension eval-language fit-identification eval-identification policy-agreement

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

test-web:
	node --test tests/web/*.test.mjs

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

# Demo people and starting cases on a database that holds the cohort (TRZ-38): needs both
# ADMIN_DATABASE_URL and DATABASE_URL, and case_customers.txt of make cases. Run it again after
# make seed REPLACE=1, which empties the demo too.
seed-demo:
	uv run --frozen python -m app.cli.seed_demo

# The scheduled processes, once each, against DATABASE_URL (trazo_app). The simulated clock is
# fixed at TRAZO_NOW, so a deadline is reached only with a later AS_OF (YYYY-MM-DD).
jobs:
	uv run --frozen python -m app.cli.jobs all
expire-info-requests:
	uv run --frozen python -m app.cli.jobs expire-info-requests $(if $(AS_OF),--as-of $(AS_OF))
# Sends the emails of the outbox that are due; does nothing while EMAIL_ENABLED is off.
send-email:
	uv run --frozen python -m app.cli.jobs send-email
# Replaces the conversation text older than CONVERSATION_RETENTION_DAYS; NOW (naive UTC,
# YYYY-MM-DDTHH:MM) runs it as of a later time, to see the purge without waiting.
purge:
	uv run --frozen python -m app.cli.jobs purge $(if $(NOW),--now $(NOW))

# The synthetic fixture instead of the cohort, as in CI and a fresh compose stack.
seed-synthetic:
	uv run --frozen python -m app.cli.seed synthetic $(if $(REPLACE),--replace)

# Golden conversation cases (eval/cases), with the LLM off, as CI runs them.
golden:
	LLM_ENABLED=false uv run --frozen python -m app.cli.eval eval/cases --out eval/reports

# The five measures of the statement for TRAZO and the free agent (TRZ-45):
# docs/reports/evaluacion.md from the runs recorded in eval/runs.jsonl. Makes no LLM call.
eval:
	uv run --frozen python -m pipeline.evaluation report --split $(or $(SPLIT),test)

# One recorded run of a system through the harness, within BUDGET USD of new LLM spend:
#   make eval-run SPLIT=test SYSTEM=trazo REPS=3 BUDGET=4.5
# On the test split it runs once: a clean tree is required, and a recorded run of the same
# system, model, prompt and policy is refused unless RERUN="reason" (a later adjustment).
eval-run:
	uv run --frozen python -m pipeline.evaluation run --split $(SPLIT) --system $(SYSTEM) \
		--repetitions $(or $(REPS),1) --budget $(BUDGET) $(if $(BASES),--bases $(BASES)) \
		$(if $(RERUN),--rerun-reason "$(RERUN)")

# TRAZO under half and double amount thresholds and alpha 0.10, from the LLM cache only (CA11).
eval-sensitivity:
	uv run --frozen python -m pipeline.evaluation sensitivity --split $(SPLIT) \
		$(if $(BASES),--bases $(BASES))

# TRAZO on the PT-BR cases of the test split with the comprehension prompt without its Portuguese
# example (TRZ-47), REPS repetitions within BUDGET USD of new LLM spend. Only measured; the
# autonomy watch section of make eval replays it:  make eval-degradation BUDGET=0.5
eval-degradation:
	uv run --frozen python -m pipeline.evaluation degradation --repetitions $(or $(REPS),3) \
		--budget $(BUDGET)

# Invariance, repetitions and error analysis of the held-out run (TRZ-48, TRZ-49) from the
# recorded runs and the reading cache only: no LLM call. Writes docs/reports/analisis.md.
eval-analysis:
	uv run --frozen python -m pipeline.analysis

# Ablations of TRZ-50, evaluated once on the held-out split: comprehension of four systems,
# TF-IDF + logistic regression for the intent and a ranker against the manual score, trained on
# development and calibrated on calibration. Writes eval/ablations.json; refuses to run again
# unless REASON="..." is given. No LLM call: readings come from the cache.
eval-ablations:
	uv run --frozen python -m pipeline.ablations $(if $(REASON),--reason "$(REASON)")

# Load test of TRZ-40 CA7 on an isolated stack (compose project "load", ports 5442 and 8010),
# never the public URL. Starts from a fresh database with the synthetic fixture and removes the
# stack at the end; appends to eval/load/runs.jsonl. Only SCENARIO=real calls the LLM, within
# BUDGET USD:  make load SCENARIO=rules|simulated|real [LEVELS=1,2,4] [BUDGET=0.25]
load:
	uv run --frozen python -m pipeline.load run --scenario $(SCENARIO) \
		$(if $(LEVELS),--levels $(LEVELS)) $(if $(BUDGET),--budget $(BUDGET))

# docs/reports/carga.md from the last recorded run of each load scenario. Makes no request.
report-load:
	uv run --frozen python -m pipeline.load report

# README figures from docs/reports; matplotlib comes from the figures group only.
figures:
	uv run --frozen --group figures python -m pipeline.figures

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

# docs/reports/impacto.md from gold/demand_interactions of the last make data: agent hours of
# disputes and their cost, as a labeled projection. Writes nothing under $(DATA_DIR).
report-impact:
	uv run --frozen python -m pipeline.impact

# Downloads the in-scope tables of the backup prefix (read-only, incremental) into
# $(DATA_DIR)/backup, apart from raw/, and compares them with the current version:
# docs/reports/diferencias_versiones.md.
diff-backup:
	uv run --frozen --group pipeline python -m pipeline.diff_backup

# Evaluation cases (TRZ-42) from the gold of the last make data. Case files go to $(DATA_DIR)/eval,
# outside git; the versioned part is eval/splits/manifest.json. Paraphrases come from the LLM
# through a local cache: OFFLINE=1 only reads the cache, REFREEZE=1 accepts a new test hash.
cases:
	uv run --frozen python -m pipeline.cases.run build $(if $(OFFLINE),--offline) $(if $(REFREEZE),--refreeze)

# Draws the 15 handwritten base cases and writes their template, once; it is never overwritten.
cases-template:
	uv run --frozen python -m pipeline.cases.run template

# Checks the handwritten messages: missing ones and personal data block the test split.
cases-check:
	uv run --frozen python -m pipeline.cases.run check

# Blank review sheet N (1 or 2) of the handwritten messages; the second one a day after the first.
cases-review:
	uv run --frozen python -m pipeline.cases.run review $(N)

# Agreement between the two reviews, written to eval/splits/manifest.json.
cases-agreement:
	uv run --frozen python -m pipeline.cases.run agreement

# Comprehension metrics (TRZ-13, TRZ-12) of the rules baseline and the LLM (3 runs) on the
# development split, from the frozen split files under $(DATA_DIR)/eval:
# docs/reports/comprension_desarrollo.md. LLM answers are cached in $(DATA_DIR)/eval, so a rerun
# costs nothing; BASES=N evaluates N base cases only (prompt iterations, with OUT=<path>).
# SPLIT=test with HARNESS_BUDGET=<USD> reads through the evaluation harness cache (TRZ-45).
eval-comprehension:
	uv run --frozen python -m pipeline.comprehension_eval --systems $(or $(SYSTEMS),rules llm) \
		$(if $(SPLIT),--split $(SPLIT)) $(if $(RUNS),--runs $(RUNS)) \
		$(if $(BASES),--bases $(BASES)) $(if $(OUT),--out $(OUT)) \
		$(if $(HARNESS_BUDGET),--harness-budget $(HARNESS_BUDGET))

# Language and variant of the first message on the development split (TRZ-11): the detector,
# and the turn with and without the LLM variant. The LLM part reads only the answers cached by
# make eval-comprehension; it makes no call. Writes docs/reports/idioma.md.
eval-language:
	uv run --frozen python -m pipeline.language_eval $(if $(OUT),--out $(OUT))

# Fits the identification weights and temperature on the development split and q-hat on the
# calibration split, for the rules and the LLM comprehension (TRZ-15), into
# config/identification.yaml. LLM readings come from the cache in $(DATA_DIR)/eval; new ones may
# spend at most BUDGET USD (default 3).
fit-identification:
	uv run --frozen python -m pipeline.identification_eval fit $(if $(BUDGET),--budget-usd $(BUDGET))

# Coverage, set size, Brier, ECE and reliability of identification from
# config/identification.yaml: docs/reports/identificacion.md. TEST=1 also measures the test split
# with the same parameters, after make eval-comprehension SPLIT=test cached its readings; nothing
# is fitted on it.
eval-identification:
	uv run --frozen python -m pipeline.identification_eval report \
		$(if $(BUDGET),--budget-usd $(BUDGET)) $(if $(TEST),--test)

# Policy engine against the TRZ-42 labels on the development split: docs/reports/politica.md.
# Reads $(DATA_DIR)/eval (make cases) and never the test split.
policy-agreement:
	uv run --frozen python -m pipeline.policy_agreement
