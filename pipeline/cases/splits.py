"""Split files, their hashes and the versioned manifest (TRZ-42 CA5, CA6).

Each split is written as canonical JSONL: one case per line, sorted by case_id, keys sorted, so
the same cases always give the same bytes and the same SHA-256. The files hold dataset values and
stay under DATA_DIR/eval; only `eval/splits/manifest.json` is versioned, with the hash, counts
and versions of each split and never a row.

The test hash is frozen by committing the manifest before the first tuning run. From then on
`make cases` refuses to write a different test split unless told to refreeze, and `load_split`
refuses a file whose hash is not the committed one.
"""

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline.cases.schema import CaseRecord, Split


class SplitMismatch(RuntimeError):
    """A split file or a new split does not match the frozen hash."""


def canonical(cases: list[CaseRecord]) -> bytes:
    """Serializes cases in a stable byte form.

    Args:
        cases: Cases of one split.

    Returns:
        One JSON object per line, sorted by case_id, keys sorted, UTF-8.
    """
    lines = [
        json.dumps(c.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        for c in sorted(cases, key=lambda c: c.case_id)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    """Hex SHA-256 of bytes.

    Args:
        data: Bytes.

    Returns:
        The digest.
    """
    return hashlib.sha256(data).hexdigest()


def check_separation(splits: dict[Split, list[CaseRecord]]) -> None:
    """Checks CA5: no customer in two splits and test later than calibration and dev.

    Args:
        splits: Cases per split.

    Raises:
        ValueError: When a customer is shared or the periods overlap.
    """
    customers = {s: {c.customer_id for c in cases} for s, cases in splits.items()}
    names = sorted(customers)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            shared = customers[a] & customers[b]
            if shared:
                raise ValueError(f"{len(shared)} customers are in both {a} and {b}")

    def moments(cases: list[CaseRecord]) -> list[datetime]:
        return [m for c in cases for m in (c.truth.timestamp, c.truth.claim_created, c.now) if m]

    order: list[Split] = [s for s in ("dev", "calibration", "test") if splits.get(s)]
    for earlier, later in zip(order, order[1:], strict=False):
        if max(moments(splits[earlier])) >= min(moments(splits[later])):
            raise ValueError(f"{later} is not entirely after {earlier}")


def summary(cases: list[CaseRecord]) -> dict[str, Any]:
    """Counts of a split for the manifest; categorical values only.

    Args:
        cases: Cases of one split.

    Returns:
        Counts by category, variant, provenance, message source, action and the marginals.
    """

    def count(values: list[Any]) -> dict[str, int]:
        return dict(sorted(Counter(str(v) for v in values).items()))

    bases = {c.base_id: c for c in cases}.values()
    return {
        "cases": len(cases),
        "base_cases": len(bases),
        "customers": len({c.customer_id for c in cases}),
        "by_category": count([b.category for b in bases]),
        "by_expected_action": count([b.expected.action for b in bases]),
        "by_provenance": count([b.provenance for b in bases]),
        "by_variant": count([c.variant for c in cases]),
        "by_message_source": count([c.message_source for c in cases]),
        "by_country": count([b.truth.country_code for b in bases]),
        "by_segment": count([b.truth.segment for b in bases]),
        "by_channel": count([b.truth.channel for b in bases if b.truth.channel]),
        "by_transaction_type": count(
            [b.truth.transaction_type for b in bases if b.truth.transaction_type]
        ),
        "non_native_cases": sum(c.non_native_writer for c in cases),
    }


def read_manifest(path: Path) -> dict[str, Any]:
    """Reads the manifest, or an empty one.

    Args:
        path: eval/splits/manifest.json.

    Returns:
        The manifest.
    """
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def write_split(
    folder: Path,
    split: Split,
    cases: list[CaseRecord],
    manifest: dict[str, Any],
    refreeze: bool = False,
) -> str:
    """Writes a split and returns its hash, refusing to change a frozen test split.

    Args:
        folder: DATA_DIR/eval.
        split: Split name.
        cases: Its cases.
        manifest: Current manifest; its test hash is the frozen one.
        refreeze: Allow a test split that differs from the frozen hash.

    Returns:
        SHA-256 of the written file.

    Raises:
        SplitMismatch: When the test split would change without `refreeze`.
    """
    data = canonical(cases)
    digest = sha256(data)
    frozen = manifest.get("splits", {}).get("test", {}).get("sha256")
    if split == "test" and frozen and frozen != digest and not refreeze:
        raise SplitMismatch(
            f"test split would change from the frozen {frozen[:12]} to {digest[:12]}; "
            "rerun with --refreeze and record the reason in the bitácora"
        )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{split}.jsonl").write_bytes(data)
    return digest


def load_split(folder: Path, split: Split, manifest_path: Path) -> list[CaseRecord]:
    """Reads a split after checking its hash against the versioned manifest.

    Args:
        folder: DATA_DIR/eval.
        split: Split name.
        manifest_path: eval/splits/manifest.json.

    Returns:
        The cases.

    Raises:
        SplitMismatch: When the file is not the one the manifest names.
    """
    data = (folder / f"{split}.jsonl").read_bytes()
    expected = read_manifest(manifest_path).get("splits", {}).get(split, {}).get("sha256")
    if expected != sha256(data):
        raise SplitMismatch(f"{split}.jsonl does not match the manifest hash")
    return [CaseRecord.model_validate_json(line) for line in data.decode("utf-8").splitlines()]
