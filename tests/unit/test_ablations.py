import math

import numpy as np
import pytest

from app.domain.identification import COMPONENTS
from pipeline import ablations as A
from pipeline.identification_eval import Prepared
from tests.case_support import make_case

MESSAGES = {
    "unrecognized_charge": [
        "No reconozco un cargo de MercaYa",
        "Aparece una compra que no hice",
        "Não reconheço essa compra no cartão",
        "Hay un cobro que yo no hice",
    ],
    "out_of_scope": [
        "Quiero saber mi saldo",
        "Quero abrir uma conta nova",
        "Cómo pido un préstamo",
        "Necesito cambiar mi dirección",
    ],
}


def _cases() -> list:
    out = []
    for intent, messages in MESSAGES.items():
        for i, m in enumerate(messages):
            c = make_case(message=m).model_copy(
                update={"intent": intent, "case_id": f"{intent}-{i}"}
            )
            out.append(c)
    return out


def test_the_intent_model_is_the_same_with_the_same_seed() -> None:
    first, second = A.train_intent(_cases()), A.train_intent(_cases())
    probe = _cases()

    assert list(first.predict([A._text(c) for c in probe])) == list(
        second.predict([A._text(c) for c in probe])
    )
    assert np.array_equal(first[-1].coef_, second[-1].coef_)


def test_a_higher_temperature_flattens_the_intent_probabilities() -> None:
    model = A.train_intent(_cases())
    sharp = A.intent_probabilities(model, _cases(), 0.5)
    flat = A.intent_probabilities(model, _cases(), 5.0)

    assert np.allclose(sharp.sum(axis=1), 1.0)
    assert sharp.max(axis=1).mean() > flat.max(axis=1).mean()


def test_brier_and_ece_of_known_probabilities() -> None:
    probs = np.array([[0.8, 0.2], [0.6, 0.4]])

    got = A.intent_calibration(probs, ["a", "b"], ["a", "b"])

    # Brier: (0.04 + 0.04 + 0.36 + 0.36) / 2. ECE: bin 0.8 right (gap 0.2), bin 0.6 wrong (0.6).
    assert got["brier"] == pytest.approx(0.4)
    assert got["ece"] == pytest.approx(0.4)


def _prep(values: list[dict[str, float]], true_index: int | None) -> Prepared:
    return Prepared(
        case=make_case(),
        clues=None,  # type: ignore[arg-type]
        candidates=(),
        ids=tuple(f"TX-{i}" for i in range(len(values))),
        values=tuple(values),
        true_index=true_index,
        not_convertible=False,
        rates={},
    )


def test_the_ranker_learns_the_component_that_finds_the_charge_and_is_seeded() -> None:
    def row(amount: float) -> dict[str, float]:
        return {k: (amount if k == "amount" else 0.5) for k in COMPONENTS}

    preps = [_prep([row(1.0), row(0.1), row(0.2)], 0) for _ in range(10)]
    preps += [_prep([row(0.3), row(0.9)], 1) for _ in range(10)]
    preps.append(_prep([row(0.5)], None))

    weights = A.ranker_weights(preps)

    assert weights == A.ranker_weights(preps)
    assert math.isclose(sum(abs(w) for w in weights.values()), 1.0)
    assert weights["amount"] == max(weights.values()) and weights["amount"] > 0


def test_the_ablations_are_evaluated_once(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "ablations.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(A, "RESULTS_PATH", path)

    with pytest.raises(A.AlreadyEvaluated):
        A.evaluate_all(None, None)  # type: ignore[arg-type]
