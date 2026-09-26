"""Bronze manifest: one entry per extracted source file, which is kept byte for byte as delivered.

The extracted CSV files under `DATA_DIR/raw` are the bronze layer. They are never modified or
copied; the manifest records where each came from and what it contained when it was registered.
"""

import csv
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from pipeline.contracts import BY_TABLE

PARTITION_RE = re.compile(r"_(\d{8})\.csv$")
CHUNK = 1 << 20


@dataclass(frozen=True)
class BronzeFile:
    """A registered source file.

    Attributes:
        path: Path relative to the data directory, for example `raw/complaints/.../x.csv`.
        table: Table the file belongs to.
        partition_date: Partition day for daily files; None for snapshot files.
        size: Size in bytes.
        sha256: SHA-256 of the file content.
        md5: MD5 of the file content, comparable with a single-part S3 ETag.
        modified_at: File modification time, UTC ISO. Extraction sets it to the S3 LastModified.
        loaded_at: When this content was first registered, UTC ISO. Kept while the hash is the same.
        header: Column names of the first line, without the UTF-8 BOM.
        source: Origin of the file: an s3:// URI, or "local" if it was already on disk.
        etag: S3 ETag of the object this content was downloaded from, if any.
    """

    path: str
    table: str
    partition_date: str | None
    size: int
    sha256: str
    md5: str
    modified_at: str
    loaded_at: str
    header: list[str]
    source: str
    etag: str | None = None

    @property
    def partition(self) -> date | None:
        """Partition day as a date, or None for a snapshot file."""
        return date.fromisoformat(self.partition_date) if self.partition_date else None

    @property
    def lineage_date(self) -> date:
        """Day stamped as `partition_date` on the file's rows.

        A snapshot file has no partition, so its rows carry the day the file was last modified,
        which extraction sets to the S3 LastModified.
        """
        return self.partition or date.fromisoformat(self.modified_at[:10])


def manifest_path(data_dir: Path) -> Path:
    """Returns where the bronze manifest is stored.

    Args:
        data_dir: Root data directory.

    Returns:
        Path of the manifest JSON file.
    """
    return data_dir / "manifest" / "bronze.json"


def table_of(relative_to_raw: Path) -> str | None:
    """Returns the in-scope table a raw file belongs to, or None if it is out of scope.

    Args:
        relative_to_raw: Path relative to `raw/`, such as `customers.csv`.

    Returns:
        The table name, or None.
    """
    parts = relative_to_raw.parts
    name = parts[0] if len(parts) > 1 else relative_to_raw.stem
    contract = BY_TABLE.get(name)
    if contract is None or contract.partitioned != (len(parts) > 1):
        return None
    return name


def partition_of(path: Path) -> date | None:
    """Reads the partition day from a daily file name such as `complaints_20240315.csv`.

    Args:
        path: File path.

    Returns:
        The partition date, or None if the name carries no date.
    """
    match = PARTITION_RE.search(path.name)
    return datetime.strptime(match.group(1), "%Y%m%d").date() if match else None


def read_header(path: Path) -> list[str]:
    """Reads the first line of a CSV file as column names.

    Args:
        path: CSV file path.

    Returns:
        Column names, with the UTF-8 BOM removed.
    """
    with path.open(newline="", encoding="utf-8-sig") as f:
        return next(csv.reader(f), [])


def hash_file(path: Path) -> tuple[str, str]:
    """Hashes a file in one pass.

    Args:
        path: File path.

    Returns:
        The SHA-256 and MD5 hex digests.
    """
    sha256, md5 = hashlib.sha256(), hashlib.md5(usedforsecurity=False)
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            sha256.update(chunk)
            md5.update(chunk)
    return sha256.hexdigest(), md5.hexdigest()


def load_manifest(data_dir: Path) -> dict[str, BronzeFile]:
    """Loads the manifest.

    Args:
        data_dir: Root data directory.

    Returns:
        Entries keyed by relative path; empty if there is no manifest yet.
    """
    path = manifest_path(data_dir)
    if not path.exists():
        return {}
    return {e["path"]: BronzeFile(**e) for e in json.loads(path.read_text())}


def save_manifest(data_dir: Path, entries: dict[str, BronzeFile]) -> None:
    """Writes the manifest sorted by path, so the same content always gives the same file.

    Args:
        data_dir: Root data directory.
        entries: Entries keyed by relative path.
    """
    path = manifest_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(entries[k]) for k in sorted(entries)]
    path.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")


def register(
    data_dir: Path,
    path: Path,
    entries: dict[str, BronzeFile],
    source: str,
    now: datetime,
    etag: str | None = None,
) -> BronzeFile:
    """Adds or refreshes the manifest entry of one raw file.

    `loaded_at` keeps its first value while the content hash does not change, so re-registering the
    same file leaves the entry, and everything derived from it, identical.

    Args:
        data_dir: Root data directory.
        path: Absolute path of a file under `raw/`.
        entries: Manifest entries, updated in place.
        source: Origin of the file (s3:// URI or "local").
        now: Registration time, naive UTC.
        etag: S3 ETag when the file comes from an extraction.

    Returns:
        The entry for the file.

    Raises:
        ValueError: If the file is not an in-scope table file.
    """
    relative = path.relative_to(data_dir)
    table = table_of(path.relative_to(data_dir / "raw"))
    if table is None:
        raise ValueError(f"{relative} is not an in-scope table file")
    sha256, md5 = hash_file(path)
    previous = entries.get(str(relative))
    same = previous if previous is not None and previous.sha256 == sha256 else None
    partition = partition_of(path) if BY_TABLE[table].partitioned else None
    entry = BronzeFile(
        path=str(relative),
        table=table,
        partition_date=partition.isoformat() if partition else None,
        size=path.stat().st_size,
        sha256=sha256,
        md5=md5,
        modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC)
        .replace(tzinfo=None)
        .isoformat(timespec="seconds"),
        loaded_at=same.loaded_at if same else now.isoformat(timespec="seconds"),
        header=read_header(path),
        # A local rescan must not erase the S3 origin recorded at extraction.
        source=same.source if same and source == "local" else source,
        etag=etag or (same.etag if same else None),
    )
    entries[entry.path] = entry
    return entry


def scan_bronze(data_dir: Path, now: datetime) -> dict[str, BronzeFile]:
    """Registers every in-scope raw file and drops entries whose file is gone.

    Out-of-scope tables under `raw/` are ignored and never registered.

    Args:
        data_dir: Root data directory.
        now: Registration time, naive UTC.

    Returns:
        The saved manifest entries.
    """
    raw = data_dir / "raw"
    entries = load_manifest(data_dir)
    found = {}
    for path in sorted(raw.rglob("*.csv")):
        if table_of(path.relative_to(raw)) is not None:
            entry = register(data_dir, path, entries, "local", now)
            found[entry.path] = entry
    save_manifest(data_dir, found)
    return found
