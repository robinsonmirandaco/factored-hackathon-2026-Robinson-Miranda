"""Comprehension harness (TRZ-13 CA5, TRZ-12 CA9, design 6.1).

Runs one or more comprehension systems over a frozen split and measures them the same way: field
accuracy (amount inside the tolerance of the label, date window containing the true date,
merchant, channel, card possession, language), macro F1 of intent, out-of-scope accuracy and the
rate of faithful fragments, overall, by language and by variant.

A system is any function from a redacted message and its context to a `Comprehension`, so the
rules baseline and the LLM go through the same code. Clues whose evidence is not in the message
count against the faithful rate and are dropped before the fields are scored, as in production.

The LLM (TRZ-12) runs several times through `pipeline.comprehension_llm`; its rows show the
mean with the range across runs, next to its tokens, cost and latency.

The report holds counts and rates only, never a message or a row.
"""

import argparse
import hashlib
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.adapters.llm import LLMClient
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.domain.comprehension_rules import RULES_VERSION, comprehend_rules
from app.schemas.comprehension import INTENTS, Comprehension, ComprehensionContext
from pipeline.cases.schema import CaseRecord, Split
from pipeline.cases.splits import load_split, read_manifest
from pipeline.comprehension_llm import (
    CACHE_FILE,
    LLMRuns,
    ReadingCache,
    as_system,
    check_prompt_sources,
    combine_runs,
    usage_summary,
)
from pipeline.settings import PipelineSettings

log = get_logger("pipeline.comprehension_eval")

System = Callable[[str, ComprehensionContext], Comprehension]
FIELDS = ("amount", "date", "merchant", "channel", "card_in_possession", "language")
POLICY_PATH = Path("config/policy.yaml")
REPORT_PATH = Path("docs/reports/comprension_desarrollo.md")


@dataclass(frozen=True)
class SystemSpec:
    """A comprehension system and what identifies its run.

    Attributes:
        name: Row label in the report.
        model: Model id, or "none" for rules.
        prompt_version: Prompt version, or the rules version.
        run: The system itself; None for the LLM, which the runner reads case by case.
        runs: Repeated runs behind its rows.
    """

    name: str
    model: str
    prompt_version: str
    run: System | None
    runs: int = 1


SYSTEMS: dict[str, SystemSpec] = {
    "rules": SystemSpec("rules", "none", RULES_VERSION, comprehend_rules),
}


@dataclass
class CaseScore:
    """Outcome of one case.

    Attributes:
        language: es or pt.
        variant: Language variant of the message.
        true_intent: Label.
        pred_intent: Output of the system.
        correct: Per field, whether a mentioned clue was read right; absent when not scored.
        spurious: Per field, whether a clue was produced that the message was not meant to carry.
        emitted: Clues the system produced.
        faithful: Of those, clues whose evidence is in the message.
        merchant_category: The merchant was named by its category, which is not scored.
    """

    language: str
    variant: str
    true_intent: str
    pred_intent: str
    correct: dict[str, bool] = field(default_factory=dict)
    spurious: dict[str, bool] = field(default_factory=dict)
    emitted: int = 0
    faithful: int = 0
    merchant_category: bool = False


def context_of(case: CaseRecord) -> ComprehensionContext:
    """Builds what the system knows besides the message; the customer id is left out.

    Args:
        case: Evaluation case.

    Returns:
        The context of the case.
    """
    return ComprehensionContext(
        now=case.now,
        country_code=case.truth.country_code,
        local_currency=case.truth.local_currency,
    )


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(ch for ch in decomposed if ch.isalnum())


def merchant_matches(predicted: str, stated: str) -> bool:
    """Compares the merchant a system read with the text the customer wrote.

    Case, accents, spaces and punctuation are ignored; a part of the stated name, or the stated
    name inside a longer fragment, counts.

    Args:
        predicted: Merchant clue of the system.
        stated: Merchant text of the label.

    Returns:
        True when one contains the other and the prediction has three characters or more.
    """
    p, s = _norm(predicted), _norm(stated)
    return len(p) >= 3 and (p in s or s in p)


def score_case(case: CaseRecord, raw: Comprehension) -> CaseScore:
    """Scores one output against the labels of its case.

    Args:
        case: Evaluation case.
        raw: Output of the system, before the faithfulness check.

    Returns:
        The score of the case.
    """
    out, dropped = raw.faithful(case.message)
    emitted = len(raw.clues())
    score = CaseScore(
        language=case.language,
        variant=case.variant,
        true_intent=case.intent,
        pred_intent=out.intent,
        emitted=emitted,
        faithful=emitted - len(dropped),
    )
    noise, truth = case.noise, case.truth

    def mark(name: str, mentioned: bool, produced: bool, right: bool | None) -> None:
        if mentioned:
            if right is not None:
                score.correct[name] = right
        else:
            score.spurious[name] = produced

    amount = noise.amount
    mark(
        "amount",
        amount.mentioned,
        out.amount is not None,
        None
        if amount.low is None or amount.high is None
        else out.amount is not None and amount.low <= out.amount.value <= amount.high,
    )
    date_right = None
    if truth.timestamp is not None:
        day = truth.timestamp.date()
        date_right = out.date is not None and out.date.window()[0] <= day <= out.date.window()[1]
    mark("date", noise.date.mentioned, out.date is not None, date_right)

    merchant = noise.merchant
    score.merchant_category = merchant.mentioned and merchant.form == "category"
    merchant_right = None
    if not score.merchant_category and merchant.value:
        merchant_right = out.merchant_hint is not None and merchant_matches(
            out.merchant_hint.value, merchant.value
        )
    mark("merchant", merchant.mentioned, out.merchant_hint is not None, merchant_right)

    channel_right = None
    if truth.channel is not None:
        channel_right = out.channel_hint is not None and out.channel_hint.value == truth.channel
    mark("channel", noise.channel.mentioned, out.channel_hint is not None, channel_right)

    possession_right = None
    if truth.card_in_possession is not None:
        possession_right = (
            out.card_in_possession is not None
            and out.card_in_possession.value == truth.card_in_possession
        )
    mark(
        "card_in_possession",
        noise.card_possession.mentioned,
        out.card_in_possession is not None,
        possession_right,
    )
    score.correct["language"] = out.language == case.variant
    return score


def _f1_by_intent(scores: list[CaseScore]) -> dict[str, float]:
    f1 = {}
    for intent in INTENTS:
        tp = sum(s.true_intent == intent and s.pred_intent == intent for s in scores)
        fp = sum(s.true_intent != intent and s.pred_intent == intent for s in scores)
        fn = sum(s.true_intent == intent and s.pred_intent != intent for s in scores)
        if tp + fp + fn:
            f1[intent] = 2 * tp / (2 * tp + fp + fn)
    return f1


def _rate(hits: int, total: int) -> dict[str, Any]:
    return {"hits": hits, "n": total, "rate": hits / total if total else None}


def summarize(scores: list[CaseScore]) -> dict[str, Any]:
    """Aggregates case scores into the metrics of design 6.1.

    Args:
        scores: Scores of the cases of one group.

    Returns:
        Cases, per-field accuracy and spurious rate, intent F1 (macro and per intent),
        out-of-scope recall, in-scope cases sent out of scope, and the faithful rate.
    """
    f1 = _f1_by_intent(scores)
    fields = {}
    for name in FIELDS:
        scored = [s.correct[name] for s in scores if name in s.correct]
        extra = [s.spurious[name] for s in scores if name in s.spurious]
        fields[name] = {
            "accuracy": _rate(sum(scored), len(scored)),
            "spurious": _rate(sum(extra), len(extra)),
        }
    oos = [s for s in scores if s.true_intent == "out_of_scope"]
    in_scope = [s for s in scores if s.true_intent != "out_of_scope"]
    return {
        "cases": len(scores),
        "intent_macro_f1": sum(f1.values()) / len(f1) if f1 else None,
        "intent_f1": f1,
        "out_of_scope_recall": _rate(sum(s.pred_intent == "out_of_scope" for s in oos), len(oos)),
        "in_scope_sent_out": _rate(
            sum(s.pred_intent == "out_of_scope" for s in in_scope), len(in_scope)
        ),
        "faithful": _rate(sum(s.faithful for s in scores), sum(s.emitted for s in scores)),
        "merchant_by_category": sum(s.merchant_category for s in scores),
        "fields": fields,
    }


def evaluate(cases: Iterable[CaseRecord], system: System) -> dict[str, Any]:
    """Runs a system over cases and summarizes it overall, by language and by variant.

    Args:
        cases: Cases of one split.
        system: Comprehension system.

    Returns:
        {"overall": ..., "by_language": {...}, "by_variant": {...}}.
    """
    scores = [score_case(c, system(c.message, context_of(c))) for c in cases]
    by_language: dict[str, list[CaseScore]] = defaultdict(list)
    by_variant: dict[str, list[CaseScore]] = defaultdict(list)
    for s in scores:
        by_language[s.language].append(s)
        by_variant[s.variant].append(s)
    return {
        "overall": summarize(scores),
        "by_language": {k: summarize(v) for k, v in sorted(by_language.items())},
        "by_variant": {k: summarize(v) for k, v in sorted(by_variant.items())},
    }


def _pct(metric: dict[str, Any]) -> str:
    if metric["rate"] is None:
        return "n/a"
    spread = f" [{metric['min']:.1%}, {metric['max']:.1%}]" if "min" in metric else ""
    return f"{metric['rate']:.1%}{spread} ({metric['n']})"


def _num(value: float | dict[str, float] | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, dict):
        return f"{value['mean']:.3f} [{value['min']:.3f}, {value['max']:.3f}]"
    return f"{value:.3f}"


_DEV_LABEL = (
    "> **Development split.** These numbers come from the development split, the same cases the "
    "rules were tuned on and the LLM prompt was tuned on, so they are optimistic. They are for "
    "error analysis only. The valid comparison between the rules and the LLM is on the frozen "
    "held-out test split."
)


def write_report(
    results: dict[str, dict[str, Any]], meta: dict[str, Any], specs: list[SystemSpec], path: Path
) -> None:
    """Writes the Markdown report; the same inputs always give the same bytes.

    Args:
        results: Output of `evaluate` per system name.
        meta: Split, hash, seed and versions of the run.
        specs: Systems in the order of the rows.
        path: Report path.
    """
    title = {"dev": "development", "calibration": "calibration", "test": "held-out test"}
    lines = [f"# Comprehension on the {title[meta['split']]} split", ""]
    if meta["split"] == "dev":
        lines += [_DEV_LABEL, ""]
    lines += [
        "Generated by `make eval-comprehension` (stories TRZ-13 and TRZ-12; design 6.1). Counts "
        "and rates only: no message or row is shown.",
        "",
        "## Run",
        "",
        f"- Split: {meta['split']} ({meta['cases']} cases), sha256 `{meta['sha256']}`",
        f"- Case generator seed: {meta['seed']}; cases config version: {meta['config_version']}",
        f"- Policy version: {meta['policy_version']}",
        "",
        "| System | Model | Prompt or rules version | Runs |",
        "| --- | --- | --- | --- |",
    ]
    lines += [f"| {s.name} | {s.model} | {s.prompt_version} | {s.runs} |" for s in specs]
    for note in meta.get("notes", []):
        lines += ["", note]
    lines += [
        "",
        "## How each metric is scored",
        "",
        "- **Field accuracy** is over the cases whose first message carries the clue. Amount: "
        "the value falls inside the tolerance of the label (`low`..`high`). Date: the window "
        "contains the date of the true transaction. Merchant: the clue and the stated text "
        "contain one another, ignoring case, accents and punctuation; a merchant named only by "
        "its category is not scored. Channel and card possession: equal to the true value. "
        "Language: equal to the variant of the message.",
        "- **Spurious** is over the cases whose first message does not carry the clue: the "
        "share where the system still produced one.",
        "- **Intent** is the macro F1 over the five intents. **Out of scope** is the recall on "
        "out-of-scope cases, next to the share of in-scope cases sent out of scope.",
        "- **Faithful** is the share of produced clues whose evidence is a literal part of the "
        "message (case and repeated spaces ignored). Unfaithful clues are dropped before the "
        "fields are scored.",
        "- A system with more than one run shows the mean, the range across runs in brackets "
        "and the mean number of cases or clues in parentheses.",
        "",
    ]

    def block(title: str, group: str | None) -> None:
        lines.extend([f"## {title}", ""])
        keys = [None] if group is None else list(results[specs[0].name][group])
        header = (
            "| System | Group | Cases | Intent F1 | OOS recall | In-scope sent out | Faithful |"
        )
        header += "".join(f" {f} |" for f in FIELDS)
        lines.extend([header, "| --- " * (7 + len(FIELDS)) + "|"])
        for spec in specs:
            for key in keys:
                r = results[spec.name]["overall"] if key is None else results[spec.name][group][key]
                row = (
                    f"| {spec.name} | {key or 'all'} | {r['cases']} | {_num(r['intent_macro_f1'])}"
                    f" | {_pct(r['out_of_scope_recall'])} | {_pct(r['in_scope_sent_out'])}"
                    f" | {_pct(r['faithful'])} |"
                )
                row += "".join(f" {_pct(r['fields'][f]['accuracy'])} |" for f in FIELDS)
                lines.append(row)
        lines.append("")

    block("Overall", None)
    block("By language", "by_language")
    block("By variant", "by_variant")

    lines += ["## Spurious clues", "", "| System | " + " | ".join(FIELDS[:-1]) + " |"]
    lines.append("| --- " * len(FIELDS) + "|")
    for spec in specs:
        fields = results[spec.name]["overall"]["fields"]
        lines.append(
            f"| {spec.name} | "
            + " | ".join(_pct(fields[f]["spurious"]) for f in FIELDS[:-1])
            + " |"
        )
    lines += ["", "## Intent F1 by intent", "", "| System | " + " | ".join(INTENTS) + " |"]
    lines.append("| --- " * (len(INTENTS) + 1) + "|")
    for spec in specs:
        f1 = results[spec.name]["overall"]["intent_f1"]
        lines.append(f"| {spec.name} | " + " | ".join(_num(f1.get(i)) for i in INTENTS) + " |")
    category = results[specs[0].name]["overall"]["merchant_by_category"]
    lines += ["", f"Merchants named only by category, not scored: {category} cases.", ""]
    if meta.get("usage"):
        lines += _usage_lines(meta["usage"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _usage_lines(usage: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines = [
        "## Tokens, cost and latency",
        "",
        "Per run, as first paid (a cached rerun reports the same figures). Latency is the wall "
        "time of the calls of a case, retries included. Cost is at the list price of the model, "
        "prompt cache writes at 1.25 and reads at 0.1 times the input price.",
        "",
        "| System | Run | Cases | Calls | Fallbacks | Input tokens not from cache "
        "| Cache read tokens | Output tokens | Cost USD | Cost per case USD "
        "| Latency p50 ms | Latency p95 ms |",
        "| --- " * 12 + "|",
    ]
    for name, runs in usage.items():
        for i, u in enumerate(runs, start=1):
            lines.append(
                f"| {name} | {i} | {u['cases']} | {u['calls']} | {u['fallbacks']} "
                f"| {u['input_tokens'] + u['cache_write_tokens']} | {u['cache_read_tokens']} "
                f"| {u['output_tokens']} | {u['cost_usd']:.4f} | {u['cost_per_case']:.5f} "
                f"| {u['latency_p50_ms']:.0f} | {u['latency_p95_ms']:.0f} |"
            )
    return lines + [""]


def sample_bases(cases: list[CaseRecord], bases: int | None) -> list[CaseRecord]:
    """Keeps every variant of a fixed subset of base cases, for cheap prompt iterations.

    Args:
        cases: Cases of the split.
        bases: Base cases to keep; None keeps all.

    Returns:
        The cases of the first `bases` base ids ordered by the hash of the id.
    """
    if bases is None:
        return cases
    ids = sorted({c.base_id for c in cases}, key=lambda b: hashlib.sha256(b.encode()).hexdigest())
    keep = set(ids[:bases])
    return [c for c in cases if c.base_id in keep]


def _llm_client() -> LLMClient:
    # The harness reads the key and the model from the environment like the service does.
    return LLMClient(Settings())


def run(
    settings: PipelineSettings,
    split: Split,
    names: list[str],
    out: Path,
    runs: int = 3,
    bases: int | None = None,
    max_cost_usd: float = 8.0,
) -> dict[str, dict[str, Any]]:
    """Evaluates the named systems on a frozen split and writes the report.

    Args:
        settings: Pipeline settings (DATA_DIR and the manifest path).
        split: dev, calibration or test; the test split loads only once frozen.
        names: Keys of SYSTEMS, or "llm".
        out: Report path.
        runs: Repeated runs of the LLM.
        bases: Evaluate only this many base cases (all variants), for prompt iterations.
        max_cost_usd: Most total LLM spend recorded in the cache.

    Returns:
        Results per system name.
    """
    manifest = read_manifest(settings.cases_manifest_path)
    cases = load_split(settings.data_dir / "eval", split, settings.cases_manifest_path)
    split_cases = len(cases)
    cases = sample_bases(cases, bases)
    parts = ("test_generated", "test_handwritten") if split == "test" else (split,)
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    meta = {
        "split": split,
        "cases": len(cases),
        "sha256": " + ".join(manifest["splits"][p]["sha256"] for p in parts),
        "seed": manifest["seed"],
        "config_version": manifest["config_version"],
        "policy_version": policy["version"],
    }
    if bases is not None:
        meta["notes"] = [
            f"Sample: {len(cases)} of the {split_cases} cases ({bases} base cases, every variant)."
        ]
    specs = [SYSTEMS[n] for n in names if n != "llm"]
    results = {s.name: evaluate(cases, s.run) for s in specs if s.run is not None}
    if "llm" in names:
        client = _llm_client()
        eval_dir = settings.data_dir / "eval"
        dev = load_split(eval_dir, "dev", settings.cases_manifest_path)
        check_prompt_sources(
            client, dev, load_split(eval_dir, "calibration", settings.cases_manifest_path)
        )
        runner = LLMRuns(client, ReadingCache(eval_dir / CACHE_FILE), max_cost_usd)
        per_run, usage = [], []
        for r in range(runs):
            outcomes = runner.run(cases, r)
            per_run.append(evaluate(cases, as_system(cases, outcomes)))
            usage.append(usage_summary(outcomes))
            log.info("comprehension_llm_run", run=r, **usage[-1])
        results["llm"] = combine_runs(per_run)
        specs.append(
            SystemSpec("llm", client.model, client.comprehension_prompt.version, None, runs)
        )
        meta["usage"] = {"llm": usage}
        meta["notes"] = meta.get("notes", []) + [
            f"LLM: temperature 0, {runs} runs; prompt examples checked against the development "
            "and calibration splits (none cited or contained). LLM spend recorded in the cache, "
            f"prompt iterations included: {runner.cache.spent():.4f} USD."
        ]
    write_report(results, meta, specs, out)
    for spec in specs:
        overall = results[spec.name]["overall"]
        log.info(
            "comprehension_evaluated",
            system=spec.name,
            split=split,
            cases=overall["cases"],
            intent_macro_f1=_num(overall["intent_macro_f1"]),
            **{f: overall["fields"][f]["accuracy"]["rate"] for f in FIELDS},
        )
    log.info(
        "comprehension_report_written",
        path=str(out),
        **{k: v for k, v in meta.items() if k not in {"usage", "notes"}},
    )
    return results


def main(argv: list[str] | None = None) -> int:
    """Command line entry point of `make eval-comprehension`.

    Args:
        argv: Arguments; defaults to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev", "calibration", "test"], default="dev")
    parser.add_argument(
        "--systems", nargs="+", choices=[*sorted(SYSTEMS), "llm"], default=["rules"]
    )
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    parser.add_argument("--runs", type=int, default=3, help="repeated runs of the LLM")
    parser.add_argument("--bases", type=int, default=None, help="only this many base cases")
    parser.add_argument("--max-cost-usd", type=float, default=8.0, help="total LLM spend cap")
    args = parser.parse_args(argv)
    settings = PipelineSettings()
    configure_logging(settings.log_level)
    run(settings, args.split, args.systems, args.out, args.runs, args.bases, args.max_cost_usd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
