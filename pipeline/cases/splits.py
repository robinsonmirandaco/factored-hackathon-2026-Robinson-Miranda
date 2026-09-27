"""Split files, their hashes and the versioned manifest (TRZ-42 CA5, CA6).

Each split is written as canonical JSONL: one case per line, sorted by case_id, keys sorted, so
the same cases always give the same bytes and the same SHA-256. The files hold dataset values and
stay under DATA_DIR/eval; only `eval/splits/manifest.json` is versioned, with the hash, counts
and versions of each split and never a row.

The test split has two blocks with their own file and hash: the cases of generator B
(`test_generated`) and the handwritten ones (`test_handwritten`). Each block is frozen by
committing the manifest with its hash, before the first tuning run; the generated block can be
frozen while the handwritten messages are still being written. From then on `make cases` refuses
to write a block that differs from its frozen hash unless told to refreeze.

`load_split` is the only way into a split. It refuses a file whose hash is not the committed
one, and it refuses the held-out test split while the handwritten block has no frozen hash, so
no evaluation can run on half of it.
"""

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pipeline.cases.schema import CaseRecord, Split

Part = Literal["dev", "calibration", "test_generated", "test_handwritten"]
TEST_BLOCKS: tuple[Part, ...] = ("test_generated", "test_handwritten")


class SplitMismatch(RuntimeError):
    """A split file or a new split does not match the frozen hash."""


class HeldOutNotFrozen(RuntimeError):
    """The test split is requested while one of its blocks has no frozen hash."""


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


def check_separation(splits: dict[str, list[CaseRecord]]) -> None:
    """Checks CA5: no customer in two splits and test later than calibration and dev.

    Args:
        splits: Cases per split (dev, calibration, test with both blocks).

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

    order = [s for s in ("dev", "calibration", "test") if splits.get(s)]
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
    part: Part,
    cases: list[CaseRecord],
    manifest: dict[str, Any],
    refreeze: bool = False,
) -> str:
    """Writes a split or test block and returns its hash, refusing to change a frozen block.

    Args:
        folder: DATA_DIR/eval.
        part: dev, calibration, test_generated or test_handwritten.
        cases: Its cases.
        manifest: Current manifest; the hash of a test block in it is the frozen one.
        refreeze: Allow a test block that differs from its frozen hash.

    Returns:
        SHA-256 of the written file.

    Raises:
        SplitMismatch: When a test block would change without `refreeze`.
    """
    data = canonical(cases)
    digest = sha256(data)
    frozen = manifest.get("splits", {}).get(part, {}).get("sha256")
    if part in TEST_BLOCKS and frozen and frozen != digest and not refreeze:
        raise SplitMismatch(
            f"{part} would change from the frozen {frozen[:12]} to {digest[:12]}; "
            "rerun with --refreeze and record the reason in the bitácora"
        )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{part}.jsonl").write_bytes(data)
    return digest


def load_split(folder: Path, split: Split, manifest_path: Path) -> list[CaseRecord]:
    """Reads a split after checking every file against the versioned manifest.

    The test split is the union of its two blocks and is only served when both are frozen.

    Args:
        folder: DATA_DIR/eval.
        split: dev, calibration or test.
        manifest_path: eval/splits/manifest.json.

    Returns:
        The cases.

    Raises:
        HeldOutNotFrozen: When the test split is asked for and a block has no frozen hash.
        SplitMismatch: When a file is not the one the manifest names.
    """
    hashes = read_manifest(manifest_path).get("splits", {})
    parts: tuple[str, ...] = TEST_BLOCKS if split == "test" else (split,)
    missing = [p for p in parts if not hashes.get(p, {}).get("sha256")]
    if split == "test" and missing:
        raise HeldOutNotFrozen(
            f"the held-out test split is not frozen yet ({', '.join(missing)} has no hash); "
            "no evaluation may run on it"
        )
    cases = []
    for part in parts:
        data = (folder / f"{part}.jsonl").read_bytes()
        if hashes.get(part, {}).get("sha256") != sha256(data):
            raise SplitMismatch(f"{part}.jsonl does not match the manifest hash")
        cases += [CaseRecord.model_validate_json(x) for x in data.decode("utf-8").splitlines()]
    return cases
