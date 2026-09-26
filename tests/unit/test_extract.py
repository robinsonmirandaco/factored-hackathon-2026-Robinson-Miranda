"""Incremental S3 extraction with a fake client (TRZ-02 CA8)."""

import hashlib
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from pipeline.extract import extract
from pipeline.manifest import load_manifest
from pipeline.run import run
from tests.pipeline_data import build_dataset, write_dataset

BUCKET = "bucket"
PREFIX = "data/"
NOW = datetime(2026, 9, 26, 12, 0)
LAST_MODIFIED = datetime(2026, 8, 31, 21, 36, tzinfo=UTC)


class FakeS3:
    """In-memory bucket with the two operations extraction uses."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.downloads: list[str] = []
        self.corrupt: set[str] = set()
        self.multipart: set[str] = set()

    def get_paginator(self, operation_name: str) -> "FakeS3":
        assert operation_name == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:  # noqa: N803
        contents = [
            {
                "Key": key,
                "Size": len(body),
                "ETag": self._etag(key, body),
                "LastModified": LAST_MODIFIED,
            }
            for key, body in sorted(self.objects.items())
            if key.startswith(Prefix)
        ]
        yield {"Contents": contents}

    def _etag(self, key: str, body: bytes) -> str:
        md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
        # A multipart ETag is a hash of part hashes plus the part count, not the file's MD5.
        return f'"{md5[::-1]}-2"' if key in self.multipart else f'"{md5}"'

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:  # noqa: N803
        self.downloads.append(Key)
        body = self.objects[Key]
        Path(Filename).write_bytes(body[:-1] if Key in self.corrupt else body)


@pytest.fixture
def bucket(tmp_path: Path) -> FakeS3:
    source = tmp_path / "source"
    write_dataset(source, build_dataset([date(2024, 1, 10), date(2024, 1, 11)]))
    objects = {
        PREFIX + str(p.relative_to(source / "raw")): p.read_bytes()
        for p in (source / "raw").rglob("*.csv")
    }
    objects[PREFIX + "marketing_campaigns.csv"] = b"campaign_id\nC1\n"
    objects[PREFIX + "digital_events/year=2024/month=01/day=10/digital_events_20240110.csv"] = (
        b"event_id\nE1\n"
    )
    return FakeS3(objects)


def test_downloads_only_in_scope_tables_and_registers_them(bucket: FakeS3, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    stats = extract(bucket, BUCKET, PREFIX, data_dir, NOW)

    assert stats.listed == stats.downloaded == 15
    assert not any("marketing" in k or "digital_events" in k for k in bucket.downloads)
    entry = load_manifest(data_dir)["raw/customers.csv"]
    assert entry.source == f"s3://{BUCKET}/{PREFIX}customers.csv"
    assert entry.modified_at == "2026-08-31T21:36:00"


def test_second_extraction_skips_files_with_same_size_and_hash(
    bucket: FakeS3, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    extract(bucket, BUCKET, PREFIX, data_dir, NOW)
    bucket.downloads.clear()

    stats = extract(bucket, BUCKET, PREFIX, data_dir, datetime(2026, 9, 27))

    assert stats.skipped == 15 and stats.downloaded == 0
    assert bucket.downloads == []
    assert load_manifest(data_dir)["raw/customers.csv"].loaded_at == NOW.isoformat(
        timespec="seconds"
    )


def test_changed_object_is_downloaded_again(bucket: FakeS3, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    extract(bucket, BUCKET, PREFIX, data_dir, NOW)
    key = PREFIX + "branches.csv"
    bucket.objects[key] += b"SUC-9,BR9\n"
    bucket.downloads.clear()

    later = datetime(2026, 9, 27)
    stats = extract(bucket, BUCKET, PREFIX, data_dir, later)

    assert bucket.downloads == [key] and stats.downloaded == 1
    entry = load_manifest(data_dir)["raw/branches.csv"]
    assert entry.sha256 == hashlib.sha256(bucket.objects[key]).hexdigest()
    assert entry.loaded_at == later.isoformat(timespec="seconds")


def test_multipart_object_is_skipped_only_while_its_etag_and_content_are_unchanged(
    bucket: FakeS3, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    key = PREFIX + "customers.csv"
    bucket.multipart.add(key)
    extract(bucket, BUCKET, PREFIX, data_dir, NOW)
    bucket.downloads.clear()

    extract(bucket, BUCKET, PREFIX, data_dir, NOW)
    assert bucket.downloads == []

    bucket.objects[key] += b"CUS-9\n"
    extract(bucket, BUCKET, PREFIX, data_dir, NOW)
    assert bucket.downloads == [key]


def test_download_that_does_not_match_its_etag_fails_and_leaves_nothing(
    bucket: FakeS3, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    bucket.corrupt.add(PREFIX + "customers.csv")

    with pytest.raises(ValueError, match="customers.csv"):
        extract(bucket, BUCKET, PREFIX, data_dir, NOW)

    assert not (data_dir / "raw" / "customers.csv").exists()
    assert not list((data_dir / "raw").glob("*.part"))


def test_data_starts_from_an_empty_folder(bucket: FakeS3, tmp_path: Path) -> None:
    data_dir = tmp_path / "empty"

    extract(bucket, BUCKET, PREFIX, data_dir, NOW)
    result = run(data_dir, Path("config/normalization.yaml"), NOW)

    assert sum(t.bronze_rows for t in result.tables) > 0
    assert all(t.quarantine_rows == 0 for t in result.tables)
