"""Incremental load state: which bronze files are already in silver, and from what input.

A file is read again only when its content, the content it depends on, or the pipeline version
changed, when its output is missing, or when its partition falls inside the reprocessing window.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from pipeline.contracts import BY_TABLE, PIPELINE_VERSION, Contract
from pipeline.manifest import BronzeFile
from pipeline.silver import FileCounts, partition_dirs, silver_dir


@dataclass(frozen=True)
class FileState:
    """What the last run that read a bronze file produced from it.

    Attributes:
        table: Table name.
        partition_date: Partition day, or None for a snapshot file.
        fingerprint: Hash of everything the file's output depends on.
        batch_id: Batch that wrote the file's rows.
        bronze_rows: Rows in the file.
        silver_rows: Rows written to silver.
        quarantine_rows: Rows written to quarantine.
    """

    table: str
    partition_date: str | None
    fingerprint: str
    batch_id: str
    bronze_rows: int
    silver_rows: int
    quarantine_rows: int


@dataclass(frozen=True)
class Inputs:
    """Content hashes a file's output can depend on, indexed once per run.

    Attributes:
        base: Pipeline and normalization versions plus every snapshot file hash, joined.
        partitions: Hash of each daily file by (table, partition date).
    """

    base: str
    partitions: dict[tuple[str, str], str]


def state_path(data_dir: Path) -> Path:
    """Returns where the load state is stored.

    Args:
        data_dir: Root data directory.

    Returns:
        Path of the state JSON file.
    """
    return data_dir / "state" / "partitions.json"


def load_state(data_dir: Path) -> dict[str, FileState]:
    """Loads the load state.

    Args:
        data_dir: Root data directory.

    Returns:
        State per bronze file path; empty if nothing was processed yet.
    """
    path = state_path(data_dir)
    if not path.exists():
        return {}
    return {k: FileState(**v) for k, v in json.loads(path.read_text()).items()}


def save_state(data_dir: Path, state: dict[str, FileState]) -> None:
    """Writes the load state sorted by path.

    Args:
        data_dir: Root data directory.
        state: State per bronze file path.
    """
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = {k: asdict(state[k]) for k in sorted(state)}
    path.write_text(json.dumps(rows, indent=1) + "\n")


def batch_id(manifest: dict[str, BronzeFile], normalization_version: int) -> str:
    """Identifies a run's input: the same bronze content and versions give the same id.

    Args:
        manifest: Bronze manifest.
        normalization_version: Version of the normalization mapping.

    Returns:
        16 hexadecimal characters.
    """
    digest = hashlib.sha256(f"{PIPELINE_VERSION}|{normalization_version}".encode())
    for path in sorted(manifest):
        digest.update(f"|{path}={manifest[path].sha256}".encode())
    return digest.hexdigest()[:16]


def index_inputs(manifest: dict[str, BronzeFile], normalization_version: int) -> Inputs:
    """Indexes the manifest for fingerprints.

    Args:
        manifest: Bronze manifest.
        normalization_version: Version of the normalization mapping.

    Returns:
        The index.
    """
    snapshots = [manifest[p].sha256 for p in sorted(manifest) if not manifest[p].partition_date]
    return Inputs(
        base="|".join([PIPELINE_VERSION, str(normalization_version), *snapshots]),
        partitions={
            (f.table, f.partition_date): f.sha256 for f in manifest.values() if f.partition_date
        },
    )


def fingerprint(file: BronzeFile, contract: Contract, inputs: Inputs) -> str:
    """Hashes what a file's output depends on.

    That is the file itself, the versions, every snapshot table (time zones and references come
    from them) and, for surveys and transcripts, the interactions file of the same partition.

    Args:
        file: Bronze file.
        contract: Its table contract.
        inputs: Indexed input hashes.

    Returns:
        SHA-256 hex digest.
    """
    parts = [inputs.base, file.sha256]
    for ref in contract.references:
        if BY_TABLE[ref.table].partitioned and file.partition_date:
            parts.append(inputs.partitions.get((ref.table, file.partition_date), "missing"))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _changed(
    file: BronzeFile, contract: Contract, state: dict[str, FileState], inputs: Inputs
) -> bool:
    entry = state.get(file.path)
    return entry is None or entry.fingerprint != fingerprint(file, contract, inputs)


def select_files(
    contract: Contract,
    files: list[BronzeFile],
    state: dict[str, FileState],
    inputs: Inputs,
    reprocess_days: int,
    data_dir: Path,
) -> tuple[list[BronzeFile], set[date]]:
    """Chooses which files of a table to read in this run.

    Args:
        contract: Table contract.
        files: Bronze files of the table.
        state: Load state.
        inputs: Indexed input hashes.
        reprocess_days: Partitions of the last this many days, counted back from the table's
            latest partition, are always read again to take in late corrections.
        data_dir: Root data directory.

    Returns:
        Files to read, and partition dates that stay as they are.
    """
    folder = silver_dir(data_dir, contract.table)
    if not contract.partitioned:
        changed = any(_changed(f, contract, state, inputs) for f in files)
        missing = not any(folder.glob("*.parquet"))
        return (files if changed or missing else []), set()
    days = [f.partition for f in files if f.partition]
    if not days:
        return files, set()
    window_start = max(days) - timedelta(days=reprocess_days)
    written = set(partition_dirs(folder))
    read, keep = [], set()
    for file in files:
        entry = state.get(file.path)
        day = file.partition
        if (
            day is None
            or day > window_start
            or _changed(file, contract, state, inputs)
            or (entry is not None and entry.silver_rows > 0 and day not in written)
        ):
            read.append(file)
        else:
            keep.add(day)
    return read, keep


def record(
    state: dict[str, FileState],
    files: list[BronzeFile],
    counts: dict[str, FileCounts],
    contract: Contract,
    inputs: Inputs,
    batch: str,
) -> None:
    """Stores the outcome of reading files, updating the state in place.

    Args:
        state: Load state.
        files: Files read in this run.
        counts: Row counts per file path.
        contract: Table contract.
        inputs: Indexed input hashes.
        batch: Batch id of this run.
    """
    for file in files:
        c = counts[file.path]
        state[file.path] = FileState(
            table=file.table,
            partition_date=file.partition_date,
            fingerprint=fingerprint(file, contract, inputs),
            batch_id=batch,
            bronze_rows=c.bronze,
            silver_rows=c.silver,
            quarantine_rows=c.quarantine,
        )
