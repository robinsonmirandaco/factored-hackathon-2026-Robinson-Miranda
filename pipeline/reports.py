"""`make report-data`: the quality and demand reports from the output of the last `make data`.

It reads the bronze manifest, the pipeline state, silver and gold under DATA_DIR and writes
`docs/reports/calidad.md` and `docs/reports/demanda.md`. Nothing under DATA_DIR is written, so the
quality report is identical to the one `make data` wrote for the same run.
"""

import duckdb

from app.core.logging import configure_logging, get_logger
from pipeline import demand, report
from pipeline.contracts import CONTRACTS, PIPELINE_VERSION, Contract
from pipeline.manifest import BronzeFile, load_manifest
from pipeline.settings import PipelineSettings
from pipeline.silver import (
    Lineage,
    TableResult,
    load_normalization,
    register_silver_view,
)
from pipeline.state import FileState, batch_id, load_state

log = get_logger("pipeline.reports")


def table_result(
    contract: Contract, files: list[BronzeFile], state: dict[str, FileState], files_read: int
) -> TableResult:
    """Adds up the recorded row counts of one table's bronze files.

    Args:
        contract: Table contract.
        files: Bronze files of the table.
        state: Pipeline state, holding every file of `files`.
        files_read: Files read by the current run (0 when only reporting).

    Returns:
        Row counts of the table.
    """
    entries = [state[f.path] for f in files]
    return TableResult(
        table=contract.table,
        files=len(files),
        files_read=files_read,
        bronze_rows=sum(e.bronze_rows for e in entries),
        silver_rows=sum(e.silver_rows for e in entries),
        quarantine_rows=sum(e.quarantine_rows for e in entries),
    )


def run_header(lineage: Lineage, settings: PipelineSettings) -> list[str]:
    """Lines that identify a run at the top of every report.

    Args:
        lineage: Batch id and pipeline version.
        settings: Pipeline settings.

    Returns:
        Markdown list items.
    """
    return [
        f"- Pipeline version: {lineage.pipeline_version}",
        f"- Batch id: {lineage.batch_id}",
        f"- Reprocessing window: {settings.pipeline_reprocess_days} days",
        f"- Seed: {settings.seed}",
    ]


def write_reports(settings: PipelineSettings) -> None:
    """Writes the quality and demand reports from DATA_DIR.

    Args:
        settings: Pipeline settings: data directory, mapping, report paths and simulated clock.

    Raises:
        FileNotFoundError: `make data` has not produced silver, gold or its state yet.
    """
    data_dir = settings.data_dir
    manifest = load_manifest(data_dir)
    state = load_state(data_dir)
    marts = [data_dir / "gold" / f"{name}.parquet" for name in demand.MARTS]
    if not manifest or any(p not in state for p in manifest) or not all(m.exists() for m in marts):
        raise FileNotFoundError(f"no complete make data output under {data_dir}")
    norm = load_normalization(settings.normalization_path)
    lineage = Lineage(batch_id(manifest, norm.version), PIPELINE_VERSION)
    con = duckdb.connect()
    # Dates derived from zoned timestamps must not depend on the machine's zone.
    con.execute("SET TimeZone = 'UTC'")
    results = []
    for contract in CONTRACTS:
        register_silver_view(con, data_dir, contract)
        files = sorted(
            (f for f in manifest.values() if f.table == contract.table), key=lambda f: f.path
        )
        results.append(table_result(contract, files, state, 0))
    header = run_header(lineage, settings)
    settings.quality_report_path.parent.mkdir(parents=True, exist_ok=True)
    settings.quality_report_path.write_text(
        report.render(con, data_dir, results, manifest, norm, header, settings.trazo_now)
    )
    settings.demand_report_path.parent.mkdir(parents=True, exist_ok=True)
    settings.demand_report_path.write_text(demand.render(con, data_dir, header))
    log.info(
        "reports_written",
        batch_id=lineage.batch_id,
        quality=str(settings.quality_report_path),
        demand=str(settings.demand_report_path),
    )


def main() -> None:
    """Writes the reports with the configured settings."""
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    write_reports(settings)


if __name__ == "__main__":
    main()
