"""Incremental extraction of the in-scope tables from S3 into the bronze layer (`make extract`).

Read-only: it lists and downloads, never writes to the bucket. A file already on disk with the
same size and the same MD5 as the object's ETag is skipped. Every file ends up in the manifest.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from app.core.logging import configure_logging, get_logger
from app.core.time import utcnow
from pipeline.contracts import CONTRACTS
from pipeline.manifest import BronzeFile, hash_file, load_manifest, register, save_manifest
from pipeline.settings import PipelineSettings

log = get_logger("pipeline.extract")


class S3Client(Protocol):
    """The two boto3 S3 client operations extraction uses."""

    def get_paginator(self, operation_name: str) -> Any:
        """Returns a paginator for a list operation."""
        ...

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:  # noqa: N803
        """Downloads one object to a local file."""
        ...


@dataclass(frozen=True)
class RemoteObject:
    """One S3 object.

    Attributes:
        key: Object key.
        size: Size in bytes.
        etag: ETag without quotes. For single-part uploads it is the MD5 of the content.
        last_modified: Last modification time.
    """

    key: str
    size: int
    etag: str
    last_modified: datetime


@dataclass(frozen=True)
class ExtractStats:
    """Outcome of one extraction.

    Attributes:
        listed: In-scope objects found in the bucket.
        downloaded: Objects downloaded because they were missing or different.
        skipped: Objects already on disk with the same size and hash.
    """

    listed: int
    downloaded: int
    skipped: int


def make_client(profile: str, region: str) -> S3Client:
    """Builds a boto3 S3 client with explicit timeouts and bounded retries.

    boto3 lives in the `pipeline` dependency group, so it is imported here and not at module load.

    Args:
        profile: Local AWS profile that holds the read-only credentials.
        region: Bucket region.

    Returns:
        An S3 client.
    """
    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=5, read_timeout=60, retries={"max_attempts": 3, "mode": "standard"}
    )
    return boto3.Session(profile_name=profile, region_name=region).client("s3", config=config)


def list_in_scope(client: S3Client, bucket: str, prefix: str) -> Iterator[RemoteObject]:
    """Lists the objects of the in-scope tables only.

    Args:
        client: S3 client.
        bucket: Bucket name.
        prefix: Key prefix of the current data version, e.g. `data/`.

    Yields:
        Each CSV object of a partitioned table folder or a snapshot file.
    """
    paginator = client.get_paginator("list_objects_v2")
    for contract in CONTRACTS:
        table_prefix = (
            f"{prefix}{contract.table}/"
            if contract.partitioned
            else f"{prefix}{contract.table}.csv"
        )
        for page in paginator.paginate(Bucket=bucket, Prefix=table_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".csv") or (not contract.partitioned and key != table_prefix):
                    continue
                yield RemoteObject(key, obj["Size"], obj["ETag"].strip('"'), obj["LastModified"])


def is_current(path: Path, obj: RemoteObject, entry: BronzeFile | None) -> bool:
    """Tells whether the local file already holds the object's content.

    Args:
        path: Local file path.
        obj: Remote object.
        entry: Manifest entry of the file, if registered.

    Returns:
        True if the file exists with the same size and its hash matches: the MD5 equals the
        ETag, or, for a multipart upload, whose ETag is not an MD5, the ETag is the one recorded
        when this same content (same SHA-256) was downloaded.
    """
    if not path.exists() or path.stat().st_size != obj.size:
        return False
    sha256, md5 = hash_file(path)
    if "-" not in obj.etag:
        return md5 == obj.etag
    return entry is not None and entry.etag == obj.etag and entry.sha256 == sha256


def extract(
    client: S3Client, bucket: str, prefix: str, data_dir: Path, now: datetime
) -> ExtractStats:
    """Downloads missing or changed in-scope objects and registers every one in the manifest.

    Args:
        client: S3 client.
        bucket: Bucket name.
        prefix: Key prefix, e.g. `data/`.
        data_dir: Root data directory; files land under `raw/` with the same relative layout.
        now: Registration time, naive UTC.

    Returns:
        Counts of listed, downloaded and skipped objects.

    Raises:
        ValueError: If a downloaded file does not match its ETag.
    """
    entries = load_manifest(data_dir)
    listed = downloaded = 0
    for obj in list_in_scope(client, bucket, prefix):
        listed += 1
        path = data_dir / "raw" / obj.key.removeprefix(prefix)
        if not is_current(path, obj, entries.get(str(path.relative_to(data_dir)))):
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".csv.part")
            client.download_file(Bucket=bucket, Key=obj.key, Filename=str(partial))
            # Multipart ETags are not an MD5, so only size can be checked for them.
            if partial.stat().st_size != obj.size or (
                "-" not in obj.etag and hash_file(partial)[1] != obj.etag
            ):
                partial.unlink()
                raise ValueError(f"download of {obj.key} does not match its size or ETag")
            os.replace(partial, path)
            downloaded += 1
            log.info("extract_downloaded", key=obj.key, size=obj.size)
        # The file carries the S3 modification time, which dates snapshot tables.
        ts = obj.last_modified.timestamp()
        os.utime(path, (ts, ts))
        register(data_dir, path, entries, f"s3://{bucket}/{obj.key}", now, etag=obj.etag)
    save_manifest(data_dir, entries)
    return ExtractStats(listed=listed, downloaded=downloaded, skipped=listed - downloaded)


def main() -> None:
    """Runs the extraction with the configured bucket, prefix, profile and data directory."""
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    client = make_client(settings.aws_profile, settings.aws_region)
    stats = extract(client, settings.s3_bucket, settings.s3_prefix, settings.data_dir, utcnow())
    log.info(
        "extract_done", listed=stats.listed, downloaded=stats.downloaded, skipped=stats.skipped
    )


if __name__ == "__main__":
    main()
