"""The five measures, their intervals and the single run on the test split (TRZ-45)."""

from pathlib import Path
from typing import Any

import pytest

from pipeline import evaluation as E
from pipeline.harness import CaseRun, FinalState, score
from tests.case_support import make_case


def test_clopper_pearson_upper_matches_the_closed_form_at_zero() -> None:
    # With 0 of n the two-sided 95% upper bound is 1 - 0.025 ** (1 / n).
    assert E.clopper_pearson_upper(0, 376) == pytest.approx(1 - 0.025 ** (1 / 376), rel=1e-6)
    assert E.clopper_pearson_upper(3, 3) == 1.0


def test_clopper_pearson_upper_of_a_known_case() -> None:
    # 5 of 100: the exact two-sided 95% interval is [0.0164, 0.1128].
    assert E.clopper_pearson_upper(5, 100) == pytest.approx(0.1128, abs=1e-4)


def test_the_bootstrap_is_the_same_with_the_same_seed_and_resamples_base_cases() -> None:
    groups = {f"B{i}": [(float(i % 2), 1.0)] * 4 for i in range(20)}

    first = E.bootstrap(groups)
    second = E.bootstrap(groups)

    assert first == second
    assert first is not None and first[0] < 0.5 < first[1]


def _scored(expected: str, final: FinalState, base: str = "B1", **case: Any) -> E.Scored:
    c = make_case(expected_action=expected, **case).model_copy(update={"base_id": base})
    run = CaseRun(c.case_id, base, c.variant, "trazo", 1, final=final)
    s = score(run, c)
    return E.Scored(run, c, s, s.unsafe)


def test_safe_resolution_counts_over_in_scope_cases_and_needs_no_person() -> None:
    resolved = FinalState(disputes=[("C1", "TX-1", "unrecognized_charge")], source_product="P1")
    handed = FinalState(handoffs=[("escalation", "x")], case_statuses=["escalated"])
    rows = [
        _scored("register_and_offer_block", resolved, "B1"),
        _scored("register_and_offer_block", handed, "B2"),
        _scored("escalate", handed, "B3"),
    ]

    m = E.measures(rows)

    assert (m["safe_resolution"]["k"], m["safe_resolution"]["n"]) == (1, 3)
    assert (m["containment"]["k"], m["containment"]["n"]) == (1, 3)
    assert (m["unnecessary_escalation"]["k"], m["unnecessary_escalation"]["n"]) == (1, 2)
    assert (m["missed_escalation"]["k"], m["missed_escalation"]["n"]) == (0, 1)


def test_an_unsafe_outcome_is_counted_by_case_and_by_base_case() -> None:
    wrong = FinalState(disputes=[("C1", "TX-2", "unrecognized_charge")], source_product="P1")
    rows = [_scored("register_and_offer_block", wrong, "B1") for _ in range(4)]
    rows += [_scored("register_and_offer_block", FinalState(), "B2")]

    u = E.measures(rows)["unsafe"]["wrong_charge"]

    assert (u["k"], u["n"], u["k_bases"], u["n_bases"]) == (4, 5, 1, 2)


def test_a_reply_with_an_email_or_another_customer_id_is_a_leak() -> None:
    case = make_case(scenario={"other_customer_id": "C2"})
    run = CaseRun(case.case_id, "B1", "es-MX", "trazo", 1)

    run.replies = ["Tu aclaración quedó registrada."]
    assert not E.pii_leak(run, case)
    run.replies = ["Escríbenos a alguien@example.com"]
    assert E.pii_leak(run, case)
    run.replies = ["El cliente C2 tiene saldo."]
    assert E.pii_leak(run, case)


def test_the_paired_difference_compares_the_same_cases() -> None:
    resolved = FinalState(disputes=[("C1", "TX-1", "unrecognized_charge")], source_product="P1")
    a = [_scored("register_and_offer_block", resolved, f"B{i}") for i in range(10)]
    b = [_scored("register_and_offer_block", FinalState(), f"B{i}") for i in range(10)]

    diff = E.paired_difference(a, b)

    assert diff is not None and diff[0] == pytest.approx(1.0)


# ---- the single run on the test split -----------------------------------------------------


def test_a_test_run_needs_a_clean_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(E, "_git", lambda *a: " M pipeline/x.py\n")

    with pytest.raises(E.DirtyTree):
        E.execute("test", "trazo", 1, 0.0, None, None, E.Settings())


def test_a_recorded_test_run_is_not_run_again_without_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = E.Settings()
    v = E.versions(settings)
    recorded = {
        "split": "test",
        "system": "trazo",
        "variant": "base",
        "model": v["model"],
        "prompts": v["prompts"]["trazo"],
        "policy_version": v["policy_version"],
        "split_sha256": E.split_hashes("test"),
        "bases": None,
        "repetition": 2,
        "complete": True,
    }
    monkeypatch.setattr(E, "_git", lambda *a: "")
    monkeypatch.setattr(E, "read_log", lambda *a: [recorded])

    with pytest.raises(E.HeldOutAlreadyRun, match=r"repetitions \[2\]"):
        E.execute("test", "trazo", 3, 0.0, None, None, settings)


def test_the_run_log_and_the_reports_do_not_make_the_tree_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = " M eval/runs.jsonl\n M docs/reports/evaluacion.md\n?? pipeline/new.py\n"
    monkeypatch.setattr(E, "_git", lambda *a: status)

    assert E.uncommitted() == ["pipeline/new.py"]


def test_an_incomplete_run_does_not_block_a_resume() -> None:
    line = {"split": "test", "system": "trazo", "repetition": 1, "complete": False}

    assert not E.same_run(line, {"split": "test", "system": "trazo", "repetition": 1})


def test_the_open_log_counts_only_the_held_out_files(tmp_path: Path) -> None:
    log = E.OpenLog(tmp_path)
    log.active = True

    log.hook("open", (str((tmp_path / "test_generated.jsonl").resolve()), "r"))
    log.hook("open", (str(tmp_path / "dev.jsonl"), "r"))

    assert dict(log.opens) == {"test_generated.jsonl:r": 1}
