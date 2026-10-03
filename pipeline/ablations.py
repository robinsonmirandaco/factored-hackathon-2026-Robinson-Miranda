"""Additional ablations on the held-out split (TRZ-50; design 6.1, 6.2, 13.1).

Run once, after the single run on the test split, and declared as such:

1. Comprehension of four systems on the same held-out cases: the rules baseline, TF-IDF with
   logistic regression for the intent, the LLM (Haiku, 3 runs) and Sonnet (1 run). The LLM
   readings come from the reading cache with a zero budget.
2. TF-IDF + logistic regression (CA1): trained on the development split, its softmax
   temperature calibrated on the calibration split, evaluated on the test split.
3. A ranker against the manual score (CA3): logistic regression on the same five components,
   trained on the development split; its temperature fitted on development as the manual score's
   was; q-hat on the calibration split; mean set size and coverage on the test split at the
   same alpha, with Brier and ECE.

Every setting below is fixed in code before the evaluation runs; nothing is chosen by looking at
the test split. The results go to eval/ablations.json (counts and rates only) and the command
refuses to run again once it exists, unless a reason is given, which the report shows.
"""

import argparse
import json
import math
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import sklearn
import yaml
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline, make_pipeline

from app.adapters.llm import LLMClient
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.domain.comprehension_rules import comprehend_rules
from app.domain.identification import COMPONENTS, Params, conformal_quantile, load_params
from app.domain.pii import redact
from app.domain.policy import load_policy
from pipeline.cases.schema import CaseRecord
from pipeline.cases.splits import load_split
from pipeline.comprehension_eval import CaseScore, evaluate, summarize
from pipeline.comprehension_llm import CACHE_FILE, LLMRuns, ReadingCache, as_system
from pipeline.evaluation import BOOTSTRAP_SEED, CASES_CONFIG, MANIFEST, OpenLog, split_hashes
from pipeline.identification_eval import CONFIG_PATH as IDENTIFICATION_CONFIG
from pipeline.identification_eval import (
    TEMPERATURE_RANGE,
    Prepared,
    base_scores,
    golden_minimum,
    in_population,
    load_gold,
    mean_nll,
    prepare,
)
from pipeline.identification_eval import outcome as identify_case
from pipeline.identification_eval import summarize as summarize_identification

log = get_logger("pipeline.ablations")

RESULTS_PATH = Path("eval/ablations.json")
POLICY_PATH = Path("config/policy.yaml")
SEED = BOOTSTRAP_SEED
# Fixed before the evaluation (2026-10-03); no search over them.
TFIDF = {"analyzer": "char_wb", "ngram_range": (2, 5), "sublinear_tf": True, "min_df": 2}
LOGISTIC_C = 1.0
ALPHAS = (0.20, 0.10, 0.05, 0.02)
CALIBRATION_BINS = 10
HAIKU_RUNS = 3
SONNET = "claude-sonnet-5-5"


class AlreadyEvaluated(RuntimeError):
    """The ablations were already evaluated on the test split."""


# ---- TF-IDF + logistic regression (CA1) ---------------------------------------------------


def _text(case: CaseRecord) -> str:
    # The model reads what the service's comprehension reads: the redacted message.
    return redact(case.message)[0]


def train_intent(cases: Sequence[CaseRecord]) -> Pipeline:
    """TF-IDF of character n-grams and a logistic regression on the intent labels.

    Args:
        cases: Development cases.

    Returns:
        The fitted pipeline.
    """
    model = make_pipeline(
        TfidfVectorizer(**TFIDF),  # type: ignore[arg-type]
        LogisticRegression(C=LOGISTIC_C, max_iter=5000, random_state=SEED),
    )
    model.fit([_text(c) for c in cases], [c.intent for c in cases])
    return model


def intent_probabilities(model: Pipeline, cases: Sequence[CaseRecord], temperature: float) -> Any:
    """Softmax of the model's logits divided by a temperature, one row per case."""
    logits = np.asarray(model.decision_function([_text(c) for c in cases]))
    if logits.ndim == 1:  # two classes: the score is the logit of the second against the first
        logits = np.column_stack([np.zeros_like(logits), logits])
    logits = logits / temperature
    logits -= logits.max(axis=1, keepdims=True)
    e = np.exp(logits)
    return e / e.sum(axis=1, keepdims=True)


def _nll(model: Pipeline, cases: Sequence[CaseRecord], temperature: float) -> float:
    probs = intent_probabilities(model, cases, temperature)
    index = {k: i for i, k in enumerate(model.classes_)}
    return -float(
        np.mean([math.log(max(probs[i, index[c.intent]], 1e-300)) for i, c in enumerate(cases)])
    )


def calibrate_temperature(model: Pipeline, cases: Sequence[CaseRecord]) -> float:
    """Temperature that minimizes the negative log-likelihood on calibration cases."""
    return golden_minimum(lambda t: _nll(model, cases, t), *TEMPERATURE_RANGE)


def intent_calibration(
    probs: Any, classes: Sequence[str], truth: Sequence[str], bins: int = CALIBRATION_BINS
) -> dict[str, float]:
    """Multiclass Brier score and top-label ECE of intent probabilities.

    Args:
        probs: One row of probabilities per case, columns in `classes` order.
        classes: Intent of each column.
        truth: True intent of each case.
        bins: Equal-width confidence bins of the ECE.

    Returns:
        {"brier", "ece"}.
    """
    onehot = np.array([[1.0 if k == t else 0.0 for k in classes] for t in truth])
    brier = float(np.mean(np.sum((probs - onehot) ** 2, axis=1)))
    top = probs.argmax(axis=1)
    confidence = probs.max(axis=1)
    correct = np.array([classes[j] == t for j, t in zip(top, truth, strict=True)], dtype=float)
    ece = 0.0
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        mask = (confidence > low) & (confidence <= high) if b else (confidence <= high)
        if mask.any():
            ece += mask.sum() * abs(confidence[mask].mean() - correct[mask].mean())
    return {"brier": brier, "ece": float(ece / len(truth))}


def intent_scores(model: Pipeline, cases: Sequence[CaseRecord]) -> list[CaseScore]:
    """Case scores with the predicted intent only: TF-IDF reads no other field."""
    predicted = model.predict([_text(c) for c in cases])
    return [
        CaseScore(c.language, c.variant, c.intent, str(p))
        for c, p in zip(cases, predicted, strict=True)
    ]


def intent_summary(scores: list[CaseScore]) -> dict[str, Any]:
    """Intent metrics overall, by language and by variant, like comprehension_eval.evaluate."""
    groups: dict[str, dict[str, list[CaseScore]]] = {"by_language": {}, "by_variant": {}}
    for s in scores:
        groups["by_language"].setdefault(s.language, []).append(s)
        groups["by_variant"].setdefault(s.variant, []).append(s)
    return {
        "overall": summarize(scores),
        **{g: {k: summarize(v) for k, v in sorted(d.items())} for g, d in groups.items()},
    }


# ---- ranker against the manual score (CA3) ------------------------------------------------


def ranker_weights(preps: Sequence[Prepared]) -> dict[str, float]:
    """Logistic regression of "is the true charge" on the five components, per candidate.

    The coefficients are divided by the sum of their absolute values, so that, as with the
    manual weights, the temperature alone sets how sharp the probabilities are.

    Args:
        preps: Prepared development cases; those without the true charge are skipped.

    Returns:
        Weight of each component.
    """
    rows, labels = [], []
    for p in preps:
        if p.true_index is None:
            continue
        for i, values in enumerate(p.values):
            rows.append([values[k] for k in COMPONENTS])
            labels.append(int(i == p.true_index))
    model = LogisticRegression(C=LOGISTIC_C, max_iter=5000, random_state=SEED)
    model.fit(np.array(rows), np.array(labels))
    coef = model.coef_[0]
    scale = float(np.abs(coef).sum()) or 1.0
    return {k: float(c / scale) for k, c in zip(COMPONENTS, coef, strict=True)}


def conformal_curve(
    params: Params, calibration: Sequence[Prepared], test: Sequence[Prepared]
) -> list[dict[str, Any]]:
    """Test coverage, set size and calibration at each alpha, q-hat from calibration only.

    No absolute rejection threshold, for either score, so the sets differ only by the scores.

    Args:
        params: Weights and temperature (q-hat and threshold are replaced).
        calibration: Prepared calibration cases.
        test: Prepared test cases.

    Returns:
        One row per alpha.
    """
    weights = dict(params.weights)
    scores = base_scores(calibration, weights, params.temperature)
    out = []
    for alpha in ALPHAS:
        qhat = conformal_quantile(scores.values(), alpha)
        p = replace(params, qhat=qhat, reject_below=-math.inf)
        s = summarize_identification([identify_case(x, p) for x in test])
        out.append({"alpha": alpha, "qhat": qhat, **_identification_fields(s)})
    return out


def _identification_fields(s: dict[str, Any]) -> dict[str, Any]:
    keys = ("n", "bases", "covered", "coverage", "mean_size", "size_one", "brier", "ece")
    return {k: s[k] for k in keys}


# ---- the single evaluation ----------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def evaluate_all(settings: Settings, reason: str | None = None) -> dict[str, Any]:
    """Runs the three ablations once and writes eval/ablations.json.

    Args:
        settings: Settings (DATA_DIR and the LLM client of the cache keys).
        reason: Why the ablations are evaluated again; required when the results exist.

    Returns:
        The results written.

    Raises:
        AlreadyEvaluated: When the results exist and no reason is given.
    """
    if RESULTS_PATH.exists() and not reason:
        raise AlreadyEvaluated(f"{RESULTS_PATH} exists; a new evaluation needs --reason")
    folder = Path(settings.data_dir) / "eval"
    dev = load_split(folder, "dev", MANIFEST, CASES_CONFIG)  # type: ignore[arg-type]
    calibration = load_split(folder, "calibration", MANIFEST, CASES_CONFIG)  # type: ignore[arg-type]
    opens = OpenLog(folder)
    sys.addaudithook(opens.hook)
    opens.active = True
    test = load_split(folder, "test", MANIFEST, CASES_CONFIG)  # type: ignore[arg-type]
    opens.active = False

    cache = ReadingCache(folder / CACHE_FILE)
    # A budget of what is already spent: an uncached request stops instead of calling the API.
    haiku = LLMRuns(LLMClient(settings), cache, cache.spent())
    sonnet = LLMRuns(
        LLMClient(settings.model_copy(update={"llm_model_primary": SONNET})),
        cache,
        cache.spent(),
    )
    haiku_runs = [haiku.run(test, r) for r in range(HAIKU_RUNS)]
    sonnet_run = sonnet.run(test, 0)

    # Comprehension of the four systems (same held-out cases).
    model = train_intent(dev)
    temperature = calibrate_temperature(model, calibration)
    probs_raw = intent_probabilities(model, test, 1.0)
    probs_cal = intent_probabilities(model, test, temperature)
    classes = [str(k) for k in model.classes_]
    truth = [c.intent for c in test]
    comprehension = {
        "rules": evaluate(test, comprehend_rules),
        "tfidf_lr": intent_summary(intent_scores(model, test)),
        "haiku": [evaluate(test, as_system(test, o)) for o in haiku_runs],
        "sonnet": evaluate(test, as_system(test, sonnet_run)),
    }
    tfidf = {
        "classes": classes,
        "temperature": temperature,
        "dev_cases": len(dev),
        "calibration_cases": len(calibration),
        "calibration_split": intent_summary(intent_scores(model, calibration))["overall"],
        "test_uncalibrated": intent_calibration(probs_raw, classes, truth),
        "test_calibrated": intent_calibration(probs_cal, classes, truth),
    }

    # Ranker against the manual score, on the LLM readings of run 0 (the service's comprehension).
    population = {
        name: [c for c in cases if in_population(c)]
        for name, cases in (("dev", dev), ("calibration", calibration), ("test", test))
    }
    customers = {c.customer_id for cases in population.values() for c in cases}
    by_customer, rates = load_gold(Path(settings.data_dir) / "gold", customers)
    readings = {}
    for name, cases in population.items():
        outcomes = haiku.run(cases, 0)
        readings[name] = {
            c.case_id: outcomes[c.case_id].reading.faithful(_text(c))[0] for c in cases
        }
    preps = {
        name: prepare(cases, readings[name], by_customer, rates)
        for name, cases in population.items()
    }
    manual = load_params(IDENTIFICATION_CONFIG, "llm")
    weights = ranker_weights(preps["dev"])
    ranker_temperature = golden_minimum(
        lambda t: mean_nll(preps["dev"], weights, t), *TEMPERATURE_RANGE
    )
    ranker = replace(manual, weights=weights, temperature=ranker_temperature)
    production = summarize_identification([identify_case(x, manual) for x in preps["test"]])
    identification = {
        "manual": {
            "weights": dict(manual.weights),
            "temperature": manual.temperature,
            "dev_nll": mean_nll(preps["dev"], dict(manual.weights), manual.temperature),
            "curve": conformal_curve(manual, preps["calibration"], preps["test"]),
            "production": _identification_fields(production),
        },
        "ranker": {
            "weights": weights,
            "temperature": ranker_temperature,
            "dev_nll": mean_nll(preps["dev"], weights, ranker_temperature),
            "curve": conformal_curve(ranker, preps["calibration"], preps["test"]),
        },
        "population": {k: len(v) for k, v in population.items()},
    }

    client = LLMClient(settings)
    results = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "commit": _git("rev-parse", "HEAD").strip(),
        "reason": reason,
        "split_sha256": {
            **split_hashes("test"),
            **split_hashes("dev"),
            **split_hashes("calibration"),
        },
        "seed": SEED,
        "sklearn": sklearn.__version__,
        "settings": {
            "tfidf": {**TFIDF, "ngram_range": list(TFIDF["ngram_range"])},  # type: ignore[call-overload]
            "logistic_c": LOGISTIC_C,
            "alphas": list(ALPHAS),
        },
        "models": {"haiku": client.model, "sonnet": SONNET},
        "prompt_version": client.comprehension_prompt.version,
        "policy_version": load_policy(POLICY_PATH).version,
        "identification_version": yaml.safe_load(IDENTIFICATION_CONFIG.read_text(encoding="utf-8"))[
            "version"
        ],
        "llm_spend_usd": cache.spent() - haiku.max_cost_usd,
        "test_file_opens": dict(opens.opens),
        "comprehension": comprehension,
        "tfidf": tfidf,
        "identification": identification,
    }
    RESULTS_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=float) + "\n", encoding="utf-8"
    )
    log.info("ablations_written", path=str(RESULTS_PATH))
    return results


def main(argv: list[str] | None = None) -> int:
    """Entry point of `make eval-ablations`.

    Args:
        argv: Arguments; defaults to sys.argv.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reason", default=None)
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    evaluate_all(Settings(log_level="WARNING"), args.reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
