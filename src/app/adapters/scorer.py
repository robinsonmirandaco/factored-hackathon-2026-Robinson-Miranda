"""Risk scorer backends behind the domain Scorer protocol.

RandomScorer is the step 1.13 placeholder: deterministic per transaction id so tests are stable.
ModelScorer loads a trained pipeline once the challenge data exists (step 2.5 onwards).
"""

import hashlib
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.domain.risk import FEATURE_NAMES, RiskResult, Scorer, TxFeatures

log = get_logger("scorer")


class RandomScorer:
    """Deterministic pseudo-random score keyed by transaction id. Not a model."""

    name = "random"

    def score(self, tx_id: str, feats: TxFeatures) -> RiskResult:
        """Scores a transaction from a hash of its id plus two obvious signals.

        Args:
            tx_id: Transaction id; the same id always gets the same score.
            feats: Transaction features.

        Returns:
            The risk result.
        """
        h = int(hashlib.sha256(tx_id.encode()).hexdigest()[:8], 16)
        base = (h % 1000) / 1000.0
        vec = feats.vector()
        # Foreign country and amount ratio nudge the score so demos behave plausibly.
        s = 0.6 * base + 0.25 * vec[5] + 0.15 * min(vec[1], 3.0) / 3.0
        s = max(0.0, min(0.99, s))
        top = [
            {"feature": "is_foreign", "contribution": 0.25 * vec[5], "value": vec[5]},
            {"feature": "amount_vs_avg_ratio", "contribution": 0.15 * vec[1], "value": vec[1]},
            {"feature": "is_night", "contribution": 0.05 * vec[3], "value": vec[3]},
        ]
        top.sort(key=lambda f: -abs(f["contribution"]))
        return RiskResult(score=round(s, 4), top_factors=top, backend=self.name)


class ModelScorer:
    """Scores with a trained scikit-learn or XGBoost pipeline saved with joblib."""

    def __init__(self, path: str | Path) -> None:
        """Loads the model bundle.

        Args:
            path: Path to a joblib bundle with keys model, backend, version and explainer.
        """
        import joblib

        bundle: dict[str, Any] = joblib.load(path)
        self.model = bundle["model"]
        self.name: str = bundle.get("backend", "model")
        self.version: str = bundle.get("version", "1")
        self.explainer = bundle.get("explainer")

    def score(self, tx_id: str, feats: TxFeatures) -> RiskResult:
        """Scores a transaction and explains it with SHAP when an explainer is available.

        Args:
            tx_id: Transaction id.
            feats: Transaction features.

        Returns:
            The risk result.
        """
        import numpy as np

        x = np.array([feats.vector()])
        proba = float(self.model.predict_proba(x)[0, 1])
        top: list[dict[str, Any]] = []
        if self.explainer is not None:
            try:
                sv = self.explainer(x)
                vals = sv.values[0] if hasattr(sv, "values") else sv[0]
                if getattr(vals, "ndim", 1) == 2:
                    vals = vals[:, 1]
                pairs = list(zip(FEATURE_NAMES, vals, x[0], strict=False))
                pairs.sort(key=lambda p: -abs(float(p[1])))
                top = [
                    {"feature": n, "contribution": float(c), "value": float(v)}
                    for n, c, v in pairs[:5]
                ]
            except Exception as exc:
                # A broken explainer must not block the score; the case just loses layer 1.
                log.warning("explainer_failed", tx_id=tx_id, error=type(exc).__name__)
        return RiskResult(
            score=round(proba, 4), top_factors=top, backend=self.name, model_version=self.version
        )


def build_scorer(settings: Settings) -> Scorer:
    """Selects the scorer backend from settings.

    Args:
        settings: Application settings.

    Returns:
        ModelScorer when a model file exists and the backend is not "random", else RandomScorer.
    """
    if settings.scorer_backend == "random":
        return RandomScorer()
    if not Path(settings.model_path).exists():
        log.warning("model_missing_using_random", path=settings.model_path)
        return RandomScorer()
    return ModelScorer(settings.model_path)
