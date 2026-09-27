"""`make cases` and the handwritten case commands (TRZ-42).

    python -m pipeline.cases.run template     # handwritten template, once
    python -m pipeline.cases.run check        # checks the handwritten messages
    python -m pipeline.cases.run review 1     # blank review sheet 1 (then 2, a day later)
    python -m pipeline.cases.run agreement    # agreement between the two reviews
    python -m pipeline.cases.run build        # make cases: dev, calibration and test splits

Reads gold (built by `make data`) and config/cases.yaml. Case files go under DATA_DIR/eval,
outside git, because they hold dataset values; eval/splits/manifest.json is the versioned part.
The test split has two blocks with their own hash: the generator B cases (`test_generated`),
written on every run, and the handwritten ones (`test_handwritten`), written only when the 60
messages pass the check.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import duckdb
import yaml

from app.adapters.llm import LLMClient
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from pipeline.cases import handwritten
from pipeline.cases.render import GENERATOR_VERSION, Generator, LLMCache, Paraphraser, render_base
from pipeline.cases.sampling import Context, SplitSample, load_context, sample_split
from pipeline.cases.schema import CaseRecord, Split
from pipeline.cases.splits import Part, check_separation, read_manifest, summary, write_split
from pipeline.settings import PipelineSettings
from pipeline.silver import load_normalization, sql_str

log = get_logger("pipeline.cases")

ROOT = Path(".")


def eval_dir(settings: PipelineSettings) -> Path:
    """Folder of the case files.

    Args:
        settings: Pipeline settings.

    Returns:
        DATA_DIR/eval.
    """
    return settings.data_dir / "eval"


def context(settings: PipelineSettings) -> tuple[Context, dict[str, Any]]:
    """Loads config/cases.yaml and the generator context from gold.

    Args:
        settings: Pipeline settings.

    Returns:
        The context and the parsed configuration.
    """
    config = yaml.safe_load(settings.cases_config_path.read_text(encoding="utf-8"))
    norm = load_normalization(settings.normalization_path)
    con = duckdb.connect()
    # Local times come from zoned timestamps; they must not depend on the machine's zone.
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"SET temp_directory = {sql_str(str(settings.data_dir / '.duckdb_tmp'))}")
    ctx = load_context(con, settings.data_dir / "gold", config, norm.currencies, settings.seed)
    return ctx, config


def template(settings: PipelineSettings) -> None:
    """Draws the handwritten base cases from the test customers and writes their template.

    Args:
        settings: Pipeline settings.
    """
    ctx, config = context(settings)
    sample = sample_split(ctx, "test", config["mix"]["handwritten"], "handwritten")
    path = handwritten.export_template(sample.bases, eval_dir(settings) / "handwritten")
    log.info("handwritten_template_written", path=str(path), base_cases=len(sample.bases))


def check(settings: PipelineSettings) -> bool:
    """Checks the handwritten messages and logs every error and warning.

    Args:
        settings: Pipeline settings.

    Returns:
        True when there are no errors.
    """
    folder = eval_dir(settings) / "handwritten"
    bases = handwritten.load_bases(folder)
    messages = handwritten.load_messages(folder)
    errors, warnings = handwritten.check(bases, messages)
    for line in errors:
        log.error("handwritten_check_error", detail=line)
    for line in warnings:
        log.warning("handwritten_check_warning", detail=line)
    log.info(
        "handwritten_check",
        messages=len(messages),
        expected=len(bases) * 4,
        errors=len(errors),
        warnings=len(warnings),
    )
    return not errors


def review(settings: PipelineSettings, n: int) -> None:
    """Writes a blank review sheet for the handwritten messages.

    Args:
        settings: Pipeline settings.
        n: Review number, 1 or 2.
    """
    folder = eval_dir(settings) / "handwritten"
    bases = handwritten.load_bases(folder)
    path = handwritten.export_review(bases, handwritten.load_messages(folder), folder, n)
    log.info("review_sheet_written", path=str(path))


def agreement(settings: PipelineSettings) -> None:
    """Computes the agreement of the two reviews and records it in the manifest.

    Args:
        settings: Pipeline settings.
    """
    folder = eval_dir(settings) / "handwritten"
    result = handwritten.agreement(folder, handwritten.load_bases(folder))
    manifest = read_manifest(settings.cases_manifest_path)
    manifest["handwritten_review"] = result
    _write_manifest(settings.cases_manifest_path, manifest)
    log.info("handwritten_agreement", **{k: v for k, v in result.items() if k != "fields"})
    for field, values in result["fields"].items():
        log.info("handwritten_agreement_field", field=field, **values)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def _paraphraser(settings: PipelineSettings, config: dict[str, Any], offline: bool) -> Paraphraser:
    llm = config["llm"]
    complete = None
    if not offline:
        app = Settings().model_copy(
            update={
                "llm_model_primary": llm["model"],
                "llm_timeout_seconds": float(llm["timeout_seconds"]),
                "llm_provider": "anthropic",
            }
        )
        client = LLMClient(app)
        if not client.available:
            raise SystemExit("LLM not available: set ANTHROPIC_API_KEY or run with --offline")
        complete = client.complete
    return Paraphraser(
        complete=complete,
        model=llm["model"],
        cache=LLMCache(eval_dir(settings) / "llm_cache.jsonl"),
        max_tokens=int(llm["max_tokens"]),
        max_calls=int(llm["max_calls"]),
        max_cost_usd=float(llm["max_cost_usd"]),
        price_in=float(llm["price_per_mtok"]["input"]),
        price_out=float(llm["price_per_mtok"]["output"]),
    )


def _marginals(sample: SplitSample) -> dict[str, Any]:
    out = {}
    for m, pop in sample.population.items():
        total = sum(pop.values())
        out[m] = {
            str(v): {"cases": sample.marginals[m][v], "population_share": round(n / total, 4)}
            for v, n in sorted(pop.items())
        }
    return out


def build(settings: PipelineSettings, offline: bool, refreeze: bool) -> None:
    """Draws, writes and hashes the splits, and updates the manifest.

    Args:
        settings: Pipeline settings.
        offline: Read LLM answers from the cache only.
        refreeze: Allow a test block that differs from its frozen hash.
    """
    ctx, config = context(settings)
    folder = eval_dir(settings)
    hw_folder = folder / "handwritten"
    if not (hw_folder / handwritten.BASES).exists():
        raise SystemExit("no handwritten cases yet: run `make cases-template` first")
    hw_bases = handwritten.load_bases(hw_folder)
    para = _paraphraser(settings, config, offline)
    version = str(config["version"])
    gens = {g: Generator.load(g, ROOT, float(config["llm"]["temperature"][g])) for g in ("a", "b")}
    plan: dict[Split, tuple[SplitSample, Generator]] = {
        "dev": (sample_split(ctx, "dev", config["mix"]["dev"], "generator_a"), gens["a"]),
        "calibration": (
            sample_split(ctx, "calibration", config["mix"]["calibration"], "generator_a"),
            gens["a"],
        ),
        "test": (
            sample_split(
                ctx,
                "test",
                config["mix"]["test"],
                "generator_b",
                exclude={b.customer_id for b in hw_bases},
                taken=hw_bases,
            ),
            gens["b"],
        ),
    }
    parts: dict[Part, list[CaseRecord]] = {}
    sources: dict[Part, Split] = {
        "dev": "dev",
        "calibration": "calibration",
        "test_generated": "test",
    }
    tokens: dict[Part, dict[str, Any]] = {}
    for part, split in sources.items():
        sample, gen = plan[split]
        versions = gen.versions(para.model, version)
        start = len(para.used)
        parts[part] = [
            c
            for b in sample.bases
            for c in render_base(b, gen, para, settings.seed, ctx.merchants, versions)
        ]
        tokens[part] = para.tokens(para.used[start:])
        log.info("split_rendered", part=part, cases=len(parts[part]), **tokens[part])

    messages = handwritten.load_messages(hw_folder)
    errors, _ = handwritten.check(hw_bases, messages)
    hw_versions = {"generator": GENERATOR_VERSION, "config": version, "author": "handwritten"}
    if errors:
        log.warning("handwritten_block_pending", errors=len(errors), messages=len(messages))
    else:
        parts["test_handwritten"] = handwritten.records(hw_bases, messages, hw_versions)

    check_separation(
        {
            "dev": parts["dev"],
            "calibration": parts["calibration"],
            "test": parts["test_generated"] + parts.get("test_handwritten", []),
        }
    )
    manifest = read_manifest(settings.cases_manifest_path)
    splits = manifest.setdefault("splits", {})
    for part, part_cases in parts.items():
        digest = write_split(folder, part, part_cases, manifest, refreeze)
        entry: dict[str, Any] = {
            "sha256": digest,
            "file": f"DATA_DIR/eval/{part}.jsonl",
            **summary(part_cases),
        }
        if part == "test_handwritten":
            entry["versions"] = hw_versions
        else:
            sample, gen = plan[sources[part]]
            entry.update(
                {
                    # The marginals of the test split count both blocks: the quotas did.
                    "marginals": _marginals(sample),
                    "second_pass_categories": sample.second_pass,
                    "llm": tokens[part],
                    "versions": gen.versions(para.model, version),
                }
            )
        splits[part] = entry
    manifest.update(
        {
            "config_version": version,
            "seed": settings.seed,
            "presence_rates": {
                "source": "gold/service_complaints, subcategory in dispute subcategories",
                "amount": round(sum(v for (a, _), v in ctx.presence.items() if a), 4),
                "product": round(sum(v for (_, p), v in ctx.presence.items() if p), 4),
                "joint": {
                    f"amount={a},product={p}": round(v, 4)
                    for (a, p), v in sorted(ctx.presence.items())
                },
            },
        }
    )
    _write_manifest(settings.cases_manifest_path, manifest)
    log.info(
        "cases_built",
        splits={p: splits[p]["sha256"][:12] for p in parts},
        llm_calls=para.usage.calls,
        llm_failed_calls=para.usage.failed_calls,
        cache_hits=para.usage.cache_hits,
        input_tokens=para.usage.input_tokens,
        output_tokens=para.usage.output_tokens,
        cost_usd=round(para.usage.cost_usd, 4),
    )


def main() -> None:
    """Parses the command and runs it."""
    parser = argparse.ArgumentParser(prog="pipeline.cases.run")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--offline", action="store_true", help="read LLM answers from the cache only")
    b.add_argument("--refreeze", action="store_true", help="allow a new test split hash")
    sub.add_parser("template")
    sub.add_parser("check")
    r = sub.add_parser("review")
    r.add_argument("n", type=int, choices=[1, 2])
    sub.add_parser("agreement")
    args = parser.parse_args()
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    if args.command == "build":
        build(settings, args.offline, args.refreeze)
    elif args.command == "template":
        template(settings)
    elif args.command == "check":
        sys.exit(0 if check(settings) else 1)
    elif args.command == "review":
        review(settings, args.n)
    else:
        agreement(settings)


if __name__ == "__main__":
    main()
