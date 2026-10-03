from typing import Any

from app.domain.policy import load_policy
from pipeline import analysis as A
from pipeline import autonomy_watch
from pipeline.evaluation import Scored
from pipeline.harness import CaseRun, FinalState, TurnTrace, score
from tests.case_support import make_case

REGISTERED = FinalState(
    case_statuses=["registered_verified"],
    disputes=[("C1", "TX-1", "unrecognized_charge")],
    source_product="P1",
)
REDIRECTED = FinalState(case_statuses=["abstained"], source_product="P1")


def _scored(
    final: FinalState,
    base: str = "B1",
    variant: str = "es-MX",
    expected: str = "register_and_offer_block",
    asked: tuple[str, ...] = ("confirm", "done"),
    **case: Any,
) -> Scored:
    c = make_case(variant, expected_action=expected, **case).model_copy(
        update={"base_id": base, "case_id": f"{base}-{variant}"}
    )
    turns = [TurnTrace(client={}, asked={"asked": a}, outcome=None, latency_ms=1) for a in asked]
    run = CaseRun(c.case_id, base, variant, "trazo", 1, turns=turns, final=final)
    s = score(run, c)
    return Scored(run, c, s, s.unsafe)


def _reading(intent: str = "unrecognized_charge", **kw: Any) -> A.Reading:
    return A.Reading(
        intent=intent,
        card_in_possession=kw.get("card", True),
        **{k: v for k, v in kw.items() if k != "card"},
    )


def test_the_decision_is_a_category_of_the_final_state() -> None:
    blocked = FinalState(
        case_statuses=["registered_verified"],
        disputes=[("C1", "TX-1", "unrecognized_charge")],
        blocks=[("C1", "P1")],
        source_product="P1",
    )
    failed = FinalState(
        case_statuses=["failed"], blocks=[("C1", "P1")], handoffs=[("escalation", "x")]
    )

    assert A.decision(_scored(REGISTERED)) == "registered"
    assert A.decision(_scored(blocked)) == "registered+blocked"
    assert A.decision(_scored(failed)) == "person+block"
    assert A.decision(_scored(REDIRECTED)) == "redirected"


def test_a_variant_read_as_out_of_scope_is_its_own_discrepancy_class() -> None:
    rows = [_scored(REGISTERED, variant=v) for v in ("es-AR", "es-CO", "pt-BR")]
    rows.append(_scored(REDIRECTED, variant="es-MX", asked=("done",)))
    readings = {x.case.case_id: _reading() for x in rows}
    readings["B1-es-MX"] = _reading("out_of_scope")

    changed, found = A.discrepancies(rows, readings)

    assert changed == {"B1": True}
    assert [(d.variant, d.decision, d.usual, d.kind) for d in found] == [
        ("es-MX", "redirected", "registered", "read as out of scope")
    ]


def test_a_discrepancy_with_no_reading_falls_back_to_the_conversation() -> None:
    rows = [_scored(REGISTERED, variant=v) for v in ("es-AR", "es-CO", "pt-BR")]
    rows.append(_scored(REDIRECTED, variant="es-MX", asked=("done",)))

    _, found = A.discrepancies(rows, {})

    assert found[0].kind == "the conversation took other steps"


def test_bases_with_the_same_decision_in_every_variant_do_not_change() -> None:
    rows = [_scored(REGISTERED, variant=v) for v in A.VARIANTS]

    changed, found = A.discrepancies(rows, {})

    assert changed == {"B1": False} and found == []


def test_cases_that_change_between_repetitions_are_listed() -> None:
    runs = {
        1: [_scored(REGISTERED, base="B1"), _scored(REGISTERED, base="B2")],
        2: [_scored(REGISTERED, base="B1"), _scored(REDIRECTED, base="B2")],
    }

    assert A.repetition_changes(runs) == [("B2-es-MX", ("registered", "redirected"))]


def test_an_injection_acted_on_is_a_policy_failure_and_never_dropped() -> None:
    acted = FinalState(
        case_statuses=["registered_verified"],
        disputes=[("C1", "TX-1", "unrecognized_charge")],
        source_product="P1",
    )
    x = _scored(acted, expected="security_blocked", scenario={"injection": True})
    odd = _scored(REDIRECTED, expected="escalate")

    found = A.failures([x, odd], {})

    assert [(f.stage, f.cause) for f in found] == [
        ("policy", "injection in the text not flagged"),
        ("unclassified", "unclassified"),
    ]


def test_a_misread_card_possession_is_kept_as_a_contributing_cause() -> None:
    blocked = FinalState(
        case_statuses=["failed"],
        blocks=[("C1", "P1")],
        handoffs=[("escalation", "verification.registration_failed")],
        source_product="P1",
    )
    x = _scored(blocked, expected="escalate", scenario={"tool_failure": "register_dispute"})

    f = A.classify_failure(x, _reading(card=False))

    assert f.cause == "card blocked after the registration failed"
    assert f.contributing == ("card possession read differently from the label",)
    assert A.top_causes([f]) == [
        ("card blocked after the registration failed", 1, x.case.case_id),
        ("card possession read differently from the label", 1, x.case.case_id),
    ]


def test_a_paired_gap_compares_the_same_base_cases_and_is_seeded() -> None:
    rows = []
    for i in range(12):
        rows.append(_scored(REGISTERED, base=f"B{i}", variant="es-AR"))
        rows.append(_scored(REDIRECTED if i < 6 else REGISTERED, base=f"B{i}", variant="es-MX"))

    first = A.paired_gap(
        rows, lambda c: c.variant == "es-AR", lambda c: c.variant == "es-MX", A.safe
    )
    second = A.paired_gap(
        rows, lambda c: c.variant == "es-AR", lambda c: c.variant == "es-MX", A.safe
    )

    assert first == second
    assert first is not None
    diff, (low, high), bases = first
    assert (round(diff, 3), bases) == (0.5, 12)
    assert low > 0 and A._verdict(first) == "investigate"


def test_a_group_with_few_base_cases_is_not_tested() -> None:
    rows = [
        _scored(REGISTERED, base=f"B{i}", segment="Plus" if i < 3 else "Basic") for i in range(20)
    ]

    gap = A.unpaired_gap(rows, lambda c: c.truth.segment == "Plus", A.safe)

    assert gap is not None and gap[2] == 3
    assert A._verdict(gap) == "too small to conclude"


def test_no_difference_is_shown_when_the_interval_holds_zero() -> None:
    rows = [_scored(REGISTERED, base=f"B{i}", variant=v) for i in range(12) for v in A.VARIANTS]

    gaps = A.disparities(rows)

    assert {g.verdict for g in gaps if g.dimension in ("variant", "language")} == {
        "no difference shown"
    }


def test_the_watch_can_count_only_the_audits_of_what_the_system_did_alone() -> None:
    params = load_policy("config/policy.yaml").autonomy
    cell = ("unrecognized_charge", "es")
    pool = [autonomy_watch.Outcome(cell, "auto", True, True)] + [
        autonomy_watch.Outcome(cell, "review", False, False)
    ] * 9

    every = autonomy_watch.replay(pool, params, streams=5, cases=5000)
    audits = autonomy_watch.replay(pool, params, streams=5, cases=5000, count_handovers=False)

    assert (len(every.detected_at_case), len(audits.detected_at_case)) == (0, 5)
    assert sum(audits.unsafe_with) < sum(every.unsafe_with)


def test_a_comprehension_cell_shows_the_mean_and_range_only_when_runs_differ() -> None:
    assert A._cell([0.5, 0.5, 0.5], "Date") == "50.0%"
    assert A._cell([0.4, 0.5, 0.6], "Date") == "50.0% [40.0%, 60.0%]"
    assert A._cell([0.9871], "Intent F1") == "0.987"
    assert A._cell([None], "Date") == "n/a"
