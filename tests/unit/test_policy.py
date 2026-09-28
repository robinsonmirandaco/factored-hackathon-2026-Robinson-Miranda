"""Policy engine (TRZ-17): load, order of evaluation, amount bands, one test per rule."""

import ast
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings
from app.domain.comprehension_rules import comprehend_rules
from app.domain.identification import Candidate, duplicate_twin
from app.domain.policy import (
    AutonomyLevel,
    Language,
    PolicyContext,
    PolicyEngine,
    PolicyError,
    initial_autonomy,
    load_policy,
)
from app.main import build_runtime
from app.schemas.comprehension import ComprehensionContext, Intent

POLICY = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"
VERSION = "2026.09.1"


@pytest.fixture(scope="module")
def engine() -> PolicyEngine:
    return PolicyEngine.from_file(POLICY)


def _fixed(level: AutonomyLevel):
    def lookup(intent: Intent, language: Language) -> AutonomyLevel:
        return level

    return lookup


A0 = _fixed("A0")


def ctx(**changes: Any) -> PolicyContext:
    values: dict[str, Any] = {
        "intent": "unrecognized_charge",
        "language": "es",
        "amount_usd": 100.0,
        "card_in_possession": True,
    }
    values.update(changes)
    return PolicyContext(**values)


# ---- the engine and the TRZ-42 labels are independent -------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
        elif isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
    return modules


def test_the_service_never_imports_the_case_generator_or_its_labels() -> None:
    # The engine is measured against the labels; reading them would make the agreement circular.
    for path in sorted((ROOT / "src/app").rglob("*.py")):
        assert not any(m.split(".")[0] == "pipeline" for m in _imports(path)), path
        assert "cases.yaml" not in path.read_text("utf-8"), path


def test_the_labels_never_import_the_engine_nor_the_comparison() -> None:
    # test_cases.py checks app.domain.policy; the comparison imports the engine, so it is closed
    # too, and so is config/policy.yaml.
    for path in sorted((ROOT / "pipeline/cases").glob("*.py")):
        for module in _imports(path):
            assert not module.startswith(("app.domain.policy", "pipeline.policy_agreement")), path


# ---- CA1: loading --------------------------------------------------------------------------


def test_the_policy_file_is_the_design_4_2_policy() -> None:
    policy = load_policy(POLICY)
    assert policy.version == VERSION
    assert (policy.amount_usd.auto_register_max, policy.amount_usd.human_review_above) == (
        500,
        1000,
    )
    assert policy.dispute_window_days == 120
    assert policy.open_dispute_lookback_days == 90
    assert policy.autonomy.initial_level == "A0"


def _broken(tmp_path: Path, change) -> Path:
    raw = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    change(raw)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


BROKEN = {
    "missing version": lambda r: r.pop("version"),
    "missing routing of an intent": lambda r: r["routing"].pop("claim_status"),
    "unknown rule id": lambda r: r["escalate_if"].append("customer_is_rich"),
    "rule listed twice": lambda r: r["escalate_if"].append("verification_failed"),
    "rule missing": lambda r: r["require_analyst_approval_if"].remove("autonomy_a1"),
    "bands in the wrong order": lambda r: r["amount_usd"].update(auto_register_max=2000),
    "unknown action": lambda r: r["routing"]["billing_error_amount"].update(action="refund"),
    "action without a level": lambda r: r["action_level"].pop("register"),
    "unknown key": lambda r: r.update(risk_bands=[0.3, 0.7]),
    "negative window": lambda r: r.update(dispute_window_days=-1),
    "unknown autonomy level": lambda r: r["autonomy"].update(initial_level="A9"),
}


@pytest.mark.parametrize("change", BROKEN.values(), ids=BROKEN.keys())
def test_an_invalid_policy_is_refused_with_a_clear_error(tmp_path: Path, change) -> None:
    path = _broken(tmp_path, change)
    with pytest.raises(PolicyError, match=str(path)):
        load_policy(path)


def test_a_file_that_is_not_yaml_or_missing_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("version: [unclosed", encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(bad)
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "missing.yaml")


def _settings(**overrides: Any) -> Settings:
    # The runtime is built without connecting; the URL only satisfies Settings.
    return Settings(
        database_url="postgresql+psycopg://unused@localhost:1/unused",
        llm_enabled=False,
        **overrides,
    )


def test_the_service_does_not_start_with_an_invalid_policy(tmp_path: Path) -> None:
    path = _broken(tmp_path, lambda r: r.pop("routing"))
    with pytest.raises(PolicyError):
        build_runtime(_settings(policy_path=str(path)))


def test_the_service_does_not_start_when_alpha_differs_from_the_fit(tmp_path: Path) -> None:
    path = _broken(tmp_path, lambda r: r["conformal"].update(alpha=0.10))
    with pytest.raises(PolicyError, match="alpha"):
        build_runtime(_settings(policy_path=str(path)))


def test_the_valid_policy_builds_the_runtime() -> None:
    runtime = build_runtime(_settings())
    assert runtime.agent.policy.version == VERSION
    assert set(runtime.agent.identification) == {"rules", "llm"}
    runtime.db.dispose()


# ---- CA3, CA6: amount bands and their edges ------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "action", "rule"),
    [
        (0.01, "register_and_offer_block", "routing.unrecognized_charge.card_in_possession"),
        (499.99, "register_and_offer_block", "routing.unrecognized_charge.card_in_possession"),
        (500.00, "register_and_offer_block", "routing.unrecognized_charge.card_in_possession"),
        (500.01, "analyst_approval", "approval.amount_above_auto_register"),
        (999.99, "analyst_approval", "approval.amount_above_auto_register"),
        (1000.00, "analyst_approval", "approval.amount_above_auto_register"),
        (1000.01, "escalate", "escalate.amount_above_human_review"),
        (None, "escalate", "escalate.amount_unknown"),
    ],
)
def test_amount_bands(engine: PolicyEngine, amount: float | None, action: str, rule: str) -> None:
    d = engine.decide(ctx(amount_usd=amount), A0)
    assert (d.action, d.rule, d.version) == (action, rule, VERSION)


def test_up_to_500_the_autonomy_level_decides(engine: PolicyEngine) -> None:
    assert engine.decide(ctx(amount_usd=500.0), _fixed("A1")).action == "analyst_approval"
    assert engine.decide(ctx(amount_usd=500.0), _fixed("A2")).action == "escalate"


# ---- CA5, CA6: one test per rule -----------------------------------------------------------

RULES = [
    ({"security_event": True}, "A0", "security_blocked", "security.security_event", "L3"),
    (
        {"intent": "out_of_scope", "card_in_possession": False, "amount_usd": None},
        "A0",
        "abstain_and_redirect",
        "routing.out_of_scope.lost_card_without_charge",
        "L0",
    ),
    ({"intent": "out_of_scope"}, "A0", "abstain_and_redirect", "routing.out_of_scope", "L0"),
    ({"intent": "claim_status"}, "A0", "report_claim_status", "routing.claim_status", "L0"),
    (
        {"amount_usd": 5000.0},
        "A0",
        "escalate",
        "escalate.amount_above_human_review",
        "L3",
    ),
    ({"amount_usd": None}, "A0", "escalate", "escalate.amount_unknown", "L3"),
    (
        {"open_dispute_last_90d": True},
        "A0",
        "escalate",
        "escalate.open_dispute_last_90d",
        "L3",
    ),
    (
        {"conformal_set_size": 0, "amount_usd": None},
        "A0",
        "escalate",
        "escalate.conformal_set_empty",
        "L3",
    ),
    ({"verification_failed": True}, "A0", "escalate", "escalate.verification_failed", "L3"),
    ({}, "A2", "escalate", "escalate.autonomy_a2", "L3"),
    ({}, "A1", "analyst_approval", "approval.autonomy_a1", "L3"),
    (
        {"amount_usd": 750.0},
        "A0",
        "analyst_approval",
        "approval.amount_above_auto_register",
        "L3",
    ),
    (
        {"card_in_possession": True},
        "A0",
        "register_and_offer_block",
        "routing.unrecognized_charge.card_in_possession",
        "L1",
    ),
    (
        {"card_in_possession": None},
        "A0",
        "register_and_offer_block",
        "routing.unrecognized_charge.card_in_possession",
        "L1",
    ),
    (
        {"card_in_possession": False},
        "A0",
        "register_and_block",
        "routing.unrecognized_charge.card_not_in_possession",
        "L2",
    ),
    (
        {"intent": "billing_error_duplicate", "duplicate_twin": "one_pending"},
        "A0",
        "explain_and_watch",
        "routing.billing_error_duplicate.one_pending",
        "L0",
    ),
    (
        {"intent": "billing_error_duplicate", "duplicate_twin": "both_approved"},
        "A0",
        "register",
        "routing.billing_error_duplicate.both_approved",
        "L1",
    ),
    (
        {"intent": "billing_error_duplicate", "duplicate_twin": None},
        "A0",
        "escalate",
        "routing.billing_error_duplicate.no_twin",
        "L3",
    ),
    (
        {"intent": "billing_error_amount"},
        "A0",
        "register",
        "routing.billing_error_amount",
        "L1",
    ),
]


@pytest.mark.parametrize(
    ("changes", "level", "action", "rule", "display"), RULES, ids=[r[3] for r in RULES]
)
def test_each_rule_returns_action_rule_version_and_level(
    engine: PolicyEngine,
    changes: dict[str, Any],
    level: AutonomyLevel,
    action: str,
    rule: str,
    display: str,
) -> None:
    d = engine.decide(ctx(**changes), _fixed(level))
    assert (d.action, d.rule, d.version, d.level) == (action, rule, VERSION, display)


def test_every_rule_of_the_policy_is_tested() -> None:
    policy = load_policy(POLICY)
    listed = {
        *(f"security.{r}" for r in policy.security_if),
        *(f"escalate.{r}" for r in policy.escalate_if),
        *(f"approval.{r}" for r in policy.require_analyst_approval_if),
    }
    assert listed <= {r[3] for r in RULES}


def test_confirmation_and_priority_follow_the_route(engine: PolicyEngine) -> None:
    blocked = engine.decide(ctx(card_in_possession=False), A0)
    assert (blocked.confirm, blocked.priority) == (True, "high")
    watch = engine.decide(ctx(intent="billing_error_duplicate", duplicate_twin="one_pending"), A0)
    assert watch.confirm is False
    lost = engine.screen("out_of_scope", "pt", False, False)
    assert lost is not None and (lost.redirect, lost.priority) == ("card_block", "urgent")


def test_an_escalation_carries_the_action_routing_would_have_taken(engine: PolicyEngine) -> None:
    d = engine.decide(ctx(amount_usd=2000.0, card_in_possession=False), A0)
    assert (d.action, d.recommended) == ("escalate", "register_and_block")
    a = engine.decide(ctx(amount_usd=800.0), A0)
    assert (a.action, a.recommended) == ("analyst_approval", "register_and_offer_block")


# ---- CA2: order of evaluation --------------------------------------------------------------


def test_security_comes_before_everything(engine: PolicyEngine) -> None:
    for changes in (
        {"amount_usd": 5000.0},
        {"intent": "out_of_scope", "card_in_possession": False},
        {"intent": "claim_status"},
        {"conformal_set_size": 0},
    ):
        d = engine.decide(ctx(security_event=True, **changes), _fixed("A2"))
        assert d.rule == "security.security_event", changes


def test_security_is_screened_before_identification(engine: PolicyEngine) -> None:
    d = engine.screen("unrecognized_charge", "es", True, security_event=True)
    assert d is not None and d.action == "security_blocked"
    assert engine.screen("unrecognized_charge", "es", True, security_event=False) is None


def test_escalation_comes_before_approval(engine: PolicyEngine) -> None:
    assert engine.decide(ctx(amount_usd=1000.01), _fixed("A1")).rule == (
        "escalate.amount_above_human_review"
    )
    assert engine.decide(ctx(amount_usd=750.0, open_dispute_last_90d=True), A0).rule == (
        "escalate.open_dispute_last_90d"
    )


def test_escalation_follows_the_order_of_the_policy_file(engine: PolicyEngine) -> None:
    both = ctx(amount_usd=5000.0, open_dispute_last_90d=True, verification_failed=True)
    assert engine.decide(both, A0).rule == "escalate.amount_above_human_review"


def test_approval_comes_before_routing(engine: PolicyEngine) -> None:
    d = engine.decide(
        ctx(intent="billing_error_duplicate", duplicate_twin="one_pending"), _fixed("A1")
    )
    assert d.action == "analyst_approval"


def test_out_of_scope_and_claim_status_skip_the_dispute_rules(engine: PolicyEngine) -> None:
    for intent in ("out_of_scope", "claim_status"):
        d = engine.decide(
            ctx(intent=intent, amount_usd=5000.0, open_dispute_last_90d=True), _fixed("A2")
        )
        assert d.action in ("abstain_and_redirect", "report_claim_status"), intent
        assert d.autonomy_level is None


# ---- CA4: autonomy cell --------------------------------------------------------------------


def test_the_policy_consults_the_cell_of_intent_and_language(engine: PolicyEngine) -> None:
    asked: list[tuple[str, str]] = []

    def lookup(intent: Intent, language: Language) -> AutonomyLevel:
        asked.append((intent, language))
        return "A1"

    d = engine.decide(ctx(intent="billing_error_amount", language="pt"), lookup)
    assert asked == [("billing_error_amount", "pt")]
    assert (d.autonomy_level, d.action) == ("A1", "analyst_approval")


def test_until_trz_30_every_cell_starts_at_the_initial_level() -> None:
    lookup = initial_autonomy(load_policy(POLICY))
    assert {
        lookup(i, lang)
        for i in ("unrecognized_charge", "billing_error_amount")
        for lang in ("es", "pt")
    } == {"A0"}


# ---- CA9: lost or stolen card, decided by intent -------------------------------------------

CONTEXT = ComprehensionContext(
    now=datetime(2026, 6, 17, 10, 0), country_code="MX", local_currency="MXN"
)


@pytest.mark.parametrize(
    ("message", "language"),
    [
        ("Me robaron la tarjeta, necesito bloquearla ya", "es"),
        ("Perdí mi tarjeta ayer", "es"),
        ("Roubaram meu cartão, preciso bloquear agora", "pt"),
        ("Perdi meu cartão ontem", "pt"),
    ],
)
def test_a_lost_card_without_a_charge_is_an_urgent_redirect(
    engine: PolicyEngine, message: str, language: str
) -> None:
    clues = comprehend_rules(message, CONTEXT)
    assert clues.card_in_possession is not None and clues.card_in_possession.value is False
    d = engine.screen(clues.intent, language, False, False)  # type: ignore[arg-type]
    assert d is not None
    assert (d.action, d.rule, d.redirect, d.priority, d.level) == (
        "abstain_and_redirect",
        "routing.out_of_scope.lost_card_without_charge",
        "card_block",
        "urgent",
        "L0",
    )


@pytest.mark.parametrize(
    ("message", "language"),
    [
        ("Me robaron la tarjeta y me hicieron varias compras", "es"),
        ("Perdí la tarjeta y aparecen compras que no hice", "es"),
        ("Roubaram meu cartão e fizeram várias compras", "pt"),
        ("Perdi meu cartão e apareceram compras que não fiz", "pt"),
    ],
)
def test_a_lost_card_with_charges_is_a_dispute_that_blocks(
    engine: PolicyEngine, message: str, language: str
) -> None:
    clues = comprehend_rules(message, CONTEXT)
    assert clues.intent == "unrecognized_charge"
    assert engine.screen(clues.intent, language, False, False) is None  # type: ignore[arg-type]
    d = engine.decide(ctx(language=language, card_in_possession=False), A0)
    assert (d.action, d.rule) == (
        "register_and_block",
        "routing.unrecognized_charge.card_not_in_possession",
    )


# ---- duplicate twin ------------------------------------------------------------------------

NOW = datetime(2026, 6, 17, 12, 0)


def _cand(tid: str, status: str = "Approved", **changes: Any) -> Candidate:
    values: dict[str, Any] = {
        "transaction_id": tid,
        "timestamp": NOW - timedelta(hours=2),
        "amount": 350.0,
        "currency": "MXN",
        "channel": "POS",
        "merchant_name": "Oxxo",
        "transaction_type": "Purchase",
        "status": status,
    }
    values.update(changes)
    return Candidate(**values)


def test_duplicate_twin() -> None:
    charge = _cand("T1")
    assert duplicate_twin(charge, [charge, _cand("T2")]) == "both_approved"
    assert duplicate_twin(charge, [_cand("T2", "Pending")]) == "one_pending"
    assert duplicate_twin(_cand("T1", "Pending"), [_cand("T2")]) == "one_pending"
    assert duplicate_twin(charge, [_cand("T2", amount=351.0)]) is None
    assert duplicate_twin(charge, [_cand("T2", merchant_name="Walmart")]) is None
    assert duplicate_twin(charge, [_cand("T2", currency="USD")]) is None
    assert (
        duplicate_twin(_cand("T1", merchant_name=None), [_cand("T2", merchant_name=None)]) is None
    )
    near = _cand("T2", "Pending", timestamp=NOW - timedelta(hours=1))
    far = _cand("T3", timestamp=NOW - timedelta(days=20))
    assert duplicate_twin(charge, [far, near]) == "one_pending"
