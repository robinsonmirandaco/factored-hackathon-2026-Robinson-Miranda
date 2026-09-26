"""`make data`: bronze manifest, silver with quarantine, then gold, deterministically.

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
from pipeline.contracts import CONTRACTS
from pipeline.gold import build_gold
from pipeline.manifest import scan_bronze
from pipeline.report import render
from pipeline.settings import PipelineSettings
from pipeline.silver import (
    TableResult,
    build_table,
    load_normalization,
    register_reference,
    sql_str,
)

log = get_logger("pipeline.run")

OUTPUT_LAYERS = ("silver", "quarantine", "gold")


@dataclass(frozen=True)
class RunResult:
    """Outcome of one pipeline run.

    Attributes:
        tables: Row counts per table, in processing order.
        gold: Row counts per gold table.
        outputs: SHA-256 per output file, keyed by path relative to the data directory.
    """

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


def run(
    data_dir: Path,
    normalization_path: Path,
    now: datetime,
    report_path: Path,
    trazo_now: datetime,
    seed: int,
) -> RunResult:
    """Runs bronze, silver and gold over the data directory and writes the quality report.

    Args:
        data_dir: Root data directory with `raw/`.
        normalization_path: Versioned normalization mapping.
        now: Run time, naive UTC; only used to date newly registered bronze files.
        report_path: Where the quality report is written.
        trazo_now: Simulated clock, shown in the time zone section of the report.
        seed: Recorded in the report; no pipeline step is random.

    Returns:
        Row counts and output hashes.
    """
    manifest = scan_bronze(data_dir, now)
    norm = load_normalization(normalization_path)
    con = duckdb.connect()
    # Dates derived from zoned timestamps must not depend on the machine's zone.
    con.execute("SET TimeZone = 'UTC'")
    # Above the default of 100 open files DuckDB splits a partition into several files, and the
    # split depends on thread timing, so output hashes would change between runs.
    con.execute("SET partitioned_write_max_open_files = 100000")
    con.execute(f"SET temp_directory = {sql_str(str(data_dir / '.duckdb_tmp'))}")
    register_reference(con, norm)
    tables = []
    for contract in CONTRACTS:
        files = sorted(
            (f for f in manifest.values() if f.table == contract.table), key=lambda f: f.path
        )
        tables.append(build_table(con, data_dir, contract, files))
    gold = build_gold(con, data_dir)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        render(con, data_dir, tables, manifest, norm, [f"- Seed: {seed}"], trazo_now)
    )
    return RunResult(tables=tables, gold=gold, outputs=hash_outputs(data_dir))


def main() -> None:
    """Runs the pipeline with the configured data directory."""
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    log.info("data_start", data_dir=str(settings.data_dir), seed=settings.seed)
    result = run(
        settings.data_dir,
        settings.normalization_path,
        utcnow(),
        settings.quality_report_path,
        settings.trazo_now,
        settings.seed,
    )
    digest = hashlib.sha256(json.dumps(result.outputs, sort_keys=True).encode()).hexdigest()
    log.info("data_done", outputs=len(result.outputs), outputs_sha256=digest, gold=result.gold)


if __name__ == "__main__":
    main()
