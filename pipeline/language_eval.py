"""Language of the first message on the development split (TRZ-11 CA5): `make eval-language`.

Three readings of every case, against its language (es, pt) and variant (es-MX, es-CO, es-AR,
pt-BR):

- detector: the rule-based detector alone, as the service applies it to a first message.
- turn without LLM: the full decision of a turn when the LLM does not answer (variant from the
  customer's country).
- turn with LLM: the full decision with the variant the LLM read, from the answers the
  comprehension harness cached (`make eval-comprehension`), 3 runs, mean and range. No call is
  made: a case missing from the cache stops the run.

Only the development split is read, and only counts leave it: the report holds no message.
The same split, detector and cached answers give the same report.
"""

import argparse
import math
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.adapters.llm import LLMClient
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.domain.language import DETECTOR_VERSION, MIN_WORDS, decide, signals
from app.schemas.comprehension import LanguageVariant
from pipeline.cases.schema import CaseRecord
from pipeline.cases.splits import load_split, read_manifest
from pipeline.comprehension_llm import CACHE_FILE, LLMRuns, ReadingCache
from pipeline.settings import PipelineSettings

log = get_logger("pipeline.language_eval")

REPORT_PATH = Path("docs/reports/idioma.md")
TARGET = 0.95
RUNS = 3
GROUPS = ("es-MX", "es-CO", "es-AR", "pt-BR")

Reading = tuple[str, LanguageVariant | None]


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval of a proportion.

    Args:
        hits: Successes.
        n: Trials.
        z: Normal quantile; 1.96 for 95%.

    Returns:
        Lower and upper bound; (0, 1) when n is 0.
    """
    if n == 0:
        return 0.0, 1.0
    p = hits / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def detector(case: CaseRecord) -> Reading:
    """The detector alone on a first message; a tie reads as Spanish, as the service does."""
    return signals(case.message).predominant or "es", None


def turn(case: CaseRecord, llm_variant: LanguageVariant | None) -> Reading:
    """The decision of a first turn, with or without the LLM's variant."""
    d = decide(case.message, llm_variant, None, case.truth.country_code)
    return d.language, d.variant


def score(cases: list[CaseRecord], read: Callable[[CaseRecord], Reading]) -> dict[str, Any]:
    """Language and variant accuracy, overall and by variant.

    Args:
        cases: Cases.
        read: Language and variant of a case (variant None when not produced).

    Returns:
        {group: {"n", "language", "variant"}} with hits; variant hits are None if not produced.
    """
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "language": 0, "variant": 0})
    produced_variant = True
    for case in cases:
        language, variant = read(case)
        produced_variant = produced_variant and variant is not None
        for group in ("all", case.variant):
            out[group]["n"] += 1
            out[group]["language"] += language == case.language
            out[group]["variant"] += variant == case.variant
    if not produced_variant:
        for group in out.values():
            group["variant"] = None
    return dict(out)


def profile(cases: list[CaseRecord]) -> dict[str, int]:
    """How many first messages are ties, mixed or short.

    Args:
        cases: Cases.

    Returns:
        Counts by kind.
    """
    found = [signals(c.message) for c in cases]
    return {
        "cases": len(cases),
        "ties": sum(s.predominant is None for s in found),
        "mixed": sum(s.mixed for s in found),
        "short": sum(len(c.message.split()) < MIN_WORDS for c in cases),
    }


def llm_variants(settings: PipelineSettings, cases: list[CaseRecord]) -> list[dict[str, Any]]:
    """The variant the LLM read in each cached run; None where it fell back to the rules.

    Args:
        settings: Pipeline settings.
        cases: Cases.

    Returns:
        One mapping of case id to variant per run.

    Raises:
        BudgetExceeded: A case is missing from the cache (the budget is 0: nothing is called).
    """
    client = LLMClient(Settings())
    runner = LLMRuns(client, ReadingCache(settings.data_dir / "eval" / CACHE_FILE), 0.0)
    runs = []
    for r in range(RUNS):
        outcomes = runner.run(cases, r)
        runs.append(
            {cid: None if o.fallback else o.reading.language for cid, o in outcomes.items()}
        )
    return runs


def _cell(hits: int | None, n: int) -> str:
    if hits is None:
        return "n/a"
    low, high = wilson(hits, n)
    return f"{hits / n:.1%} ({hits}/{n}; {low:.1%} to {high:.1%})"


def _runs_cell(values: list[int | None], n: int) -> str:
    if any(v is None for v in values):
        return "n/a"
    rates = [v / n for v in values if v is not None]
    mean = sum(rates) / len(rates)
    return f"{mean:.1%} [{min(rates):.1%}, {max(rates):.1%}] ({n})"


def write_report(
    meta: dict[str, Any],
    single: dict[str, dict[str, Any]],
    with_llm: list[dict[str, Any]],
    counts: dict[str, int],
    out: Path,
) -> None:
    """Writes the Markdown report.

    Args:
        meta: Split, hashes and versions.
        single: Scores of the detector and of the turn without LLM.
        with_llm: Scores of the turn with the LLM, one per run.
        counts: Profile of the first messages.
        out: Report path.
    """
    detector_all = single["detector"]["all"]
    rate = detector_all["language"] / detector_all["n"]
    lines = [
        "# Language detection (TRZ-11)",
        "",
        "> **Development split.** The detector's word lists were tuned on this split for the "
        "rules baseline of the comprehension, so these numbers are optimistic. Messages written "
        "by hand, and portuñol written on purpose, are only in the held-out test split, which "
        "this report does not read.",
        "",
        f"- Split: {meta['split']}, {meta['cases']} cases ({meta['bases']} base cases, "
        "4 variants each)",
        f"- Split hash: {meta['sha256']}",
        f"- Seed: {meta['seed']}; case config: {meta['config_version']}",
        f"- Detector: {DETECTOR_VERSION}",
        f"- LLM variant: {meta['model']}, prompt {meta['prompt_version']}, {RUNS} runs from the "
        "comprehension cache (no new calls)",
        "",
        "## CA5: language accuracy of the detector",
        "",
        f"Target {TARGET:.0%}. Result {rate:.1%}: " + ("met." if rate >= TARGET else "not met."),
        "",
        "Rates with their 95% Wilson interval. The four variants of a base case share one "
        "message plan, so they are not independent and the intervals are narrower than they "
        "should be.",
        "",
        "The variant without the LLM comes from the customer's country. The four variants of a "
        "base case keep the customer, and so the country, of the base case, so that fallback "
        "can match at most one Spanish variant per base case: its low rates for Spanish measure "
        "how the cases were built, not how often a customer writes in the variant of their "
        "country.",
        "",
        "| Group | Detector: language | Turn without LLM: language | Turn without LLM: variant |",
        "| --- | --- | --- | --- |",
    ]
    for group in ("all", *GROUPS):
        d, t = single["detector"][group], single["turn_rules"][group]
        lines.append(
            f"| {group} | {_cell(d['language'], d['n'])} | {_cell(t['language'], t['n'])} "
            f"| {_cell(t['variant'], t['n'])} |"
        )
    lines += [
        "",
        "## CA2: variant with the LLM",
        "",
        f"Mean over {RUNS} runs, [min, max], (cases). The variant is the LLM's when its "
        "language agrees with the detector's; otherwise the country's.",
        "",
        "| Group | Turn with LLM: language | Turn with LLM: variant |",
        "| --- | --- | --- |",
    ]
    for group in ("all", *GROUPS):
        n = with_llm[0][group]["n"]
        lines.append(
            f"| {group} | {_runs_cell([r[group]['language'] for r in with_llm], n)} "
            f"| {_runs_cell([r[group]['variant'] for r in with_llm], n)} |"
        )
    lines += [
        "",
        "## First messages",
        "",
        f"- Ties (no language with more signals; read as Spanish): {counts['ties']} of "
        f"{counts['cases']}",
        f"- Marked mixed (strong signals of both languages): {counts['mixed']} of "
        f"{counts['cases']}",
        f"- Shorter than {MIN_WORDS} words: {counts['short']} of {counts['cases']}",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


def run(settings: PipelineSettings, out: Path) -> dict[str, Any]:
    """Scores the detector and the turn decision on the development split and writes the report.

    Args:
        settings: Pipeline settings.
        out: Report path.

    Returns:
        The scores.
    """
    manifest = read_manifest(settings.cases_manifest_path)
    cases = load_split(
        settings.data_dir / "eval", "dev", settings.cases_manifest_path, settings.cases_config_path
    )
    single = {
        "detector": score(cases, detector),
        "turn_rules": score(cases, lambda c: turn(c, None)),
    }
    variants = llm_variants(settings, cases)
    with_llm = [score(cases, lambda c, v=v: turn(c, v[c.case_id])) for v in variants]
    client = LLMClient(Settings())
    meta = {
        "split": "dev",
        "cases": len(cases),
        "bases": len({c.base_id for c in cases}),
        "sha256": manifest["splits"]["dev"]["sha256"],
        "seed": manifest["seed"],
        "config_version": manifest["config_version"],
        "model": client.model,
        "prompt_version": client.comprehension_prompt.version,
    }
    counts = profile(cases)
    write_report(meta, single, with_llm, counts, out)
    detector_all = single["detector"]["all"]
    log.info(
        "language_evaluated",
        detector_language=detector_all["language"] / detector_all["n"],
        sha256=meta["sha256"],
        report=str(out),
        **counts,
    )
    return {"meta": meta, "single": single, "with_llm": with_llm, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    """Command line entry point of `make eval-language`.

    Args:
        argv: Arguments; defaults to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    args = parser.parse_args(argv)
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    run(settings, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
