"""`make data`: bronze manifest, incremental silver with quarantine, gold and the quality report.

The pipeline has no random step; the seed is recorded with the run for traceability. Running it
twice on the same bronze gives the same output hashes, listed in `manifest/outputs.json`.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from app.core.logging import configure_logging, get_logger
from app.core.time import utcnow
from pipeline.contracts import CONTRACTS, PIPELINE_VERSION
from pipeline.gold import build_gold
from pipeline.manifest import scan_bronze
from pipeline.report import render
from pipeline.settings import PipelineSettings
from pipeline.silver import (
    Lineage,
    TableResult,
    build_table,
    load_normalization,
    register_reference,
    register_silver_view,
    sql_str,
)
from pipeline.state import (
    batch_id,
    index_inputs,
    load_state,
    record,
    save_state,
    select_files,
)

log = get_logger("pipeline.run")

OUTPUT_LAYERS = ("silver", "quarantine", "gold")


@dataclass(frozen=True)
class RunResult:
    """Outcome of one pipeline run.

    Attributes:
        lineage: Batch id and pipeline version of the run.
        tables: Row counts per table, in processing order.
        gold: Row counts per gold table.
        outputs: SHA-256 per output file, keyed by path relative to the data directory.
    """

    lineage: Lineage
    tables: list[TableResult]
    gold: dict[str, int]
    outputs: dict[str, str]


def hash_outputs(data_dir: Path) -> dict[str, str]:
    """Hashes every Parquet file of silver, quarantine and gold and saves the listing.

    Args:
        data_dir: Root data directory.

    Returns:
        SHA-256 per file, keyed by relative path, sorted.
    """
    hashes = {
        str(p.relative_to(data_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for layer in OUTPUT_LAYERS
        for p in sorted((data_dir / layer).rglob("*.parquet"))
    }
    path = data_dir / "manifest" / "outputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hashes, indent=1, sort_keys=True) + "\n")
    return hashes


def run(settings: PipelineSettings, now: datetime) -> RunResult:
    """Runs bronze, silver and gold over the data directory and writes the quality report.

    Args:
        settings: Pipeline settings: data directory, mapping, report path, reprocessing window,
            simulated clock and seed.
        now: Run time, naive UTC; only used to date newly registered bronze files.

    Returns:
        Row counts and output hashes.
    """
    data_dir = settings.data_dir
    manifest = scan_bronze(data_dir, now)
    norm = load_normalization(settings.normalization_path)
    lineage = Lineage(batch_id(manifest, norm.version), PIPELINE_VERSION)
    inputs = index_inputs(manifest, norm.version)
    state = {k: v for k, v in load_state(data_dir).items() if k in manifest}
    con = duckdb.connect()
    # Dates derived from zoned timestamps must not depend on the machine's zone.
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"SET temp_directory = {sql_str(str(data_dir / '.duckdb_tmp'))}")
    register_reference(con, norm)
    tables = []
    for contract in CONTRACTS:
        files = sorted(
            (f for f in manifest.values() if f.table == contract.table), key=lambda f: f.path
        )
        read, keep = select_files(
            contract, files, state, inputs, settings.pipeline_reprocess_days, data_dir
        )
        # Partitioned tables always pass through build_table, which also drops the output of
        # partitions whose bronze file is gone.
        if read or contract.partitioned:
            counts = build_table(con, data_dir, contract, read, keep, lineage)
            record(state, read, counts, contract, inputs, lineage.batch_id)
        else:
            register_silver_view(con, data_dir, contract)
        entries = [state[f.path] for f in files]
        result = TableResult(
            table=contract.table,
            files=len(files),
            files_read=len(read),
            bronze_rows=sum(e.bronze_rows for e in entries),
            silver_rows=sum(e.silver_rows for e in entries),
            quarantine_rows=sum(e.quarantine_rows for e in entries),
        )
        log.info("silver_table", **result.__dict__)
        tables.append(result)
    save_state(data_dir, state)
    gold = build_gold(con, data_dir, lineage)
    header = [
        f"- Pipeline version: {lineage.pipeline_version}",
        f"- Batch id: {lineage.batch_id}",
        f"- Reprocessing window: {settings.pipeline_reprocess_days} days",
        f"- Seed: {settings.seed}",
    ]
    settings.quality_report_path.parent.mkdir(parents=True, exist_ok=True)
    settings.quality_report_path.write_text(
        render(con, data_dir, tables, manifest, norm, header, settings.trazo_now)
    )
    return RunResult(lineage=lineage, tables=tables, gold=gold, outputs=hash_outputs(data_dir))


def main() -> None:
    """Runs the pipeline with the configured settings."""
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    log.info("data_start", data_dir=str(settings.data_dir), seed=settings.seed)
    result = run(settings, utcnow())
    digest = hashlib.sha256(json.dumps(result.outputs, sort_keys=True).encode()).hexdigest()
    log.info(
        "data_done",
        batch_id=result.lineage.batch_id,
        outputs=len(result.outputs),
        outputs_sha256=digest,
        gold=result.gold,
    )


if __name__ == "__main__":
    main()
