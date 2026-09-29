"""Case generator and splits (TRZ-42 CA1 to CA10), on a made-up context with a fake LLM."""

import ast
import json
import random
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from app.adapters.llm import LLMCallStats
from app.domain.clock import SimulatedClock
from pipeline.cases import handwritten
from pipeline.cases.labels import LabelRules, expected_action
from pipeline.cases.noise import (
    NoiseConfig,
    draw_amount,
    draw_date,
    draw_presence,
    misspell,
    relative_windows,
)
from pipeline.cases.render import (
    BudgetExceeded,
    Complete,
    Generator,
    LLMCache,
    Paraphraser,
    PromptFacts,
    check_paraphrase,
    draft,
    render_base,
)
from pipeline.cases.sampling import Context, sample_split
from pipeline.cases.schema import (
    VARIANTS,
    AmountClue,
    BaseCase,
    CaseRecord,
    DateClue,
    Mention,
    MerchantClue,
    Noise,
    Scenario,
    Truth,
)
from pipeline.cases.splits import (
    HeldOutLocked,
    HeldOutNotFrozen,
    SplitMismatch,
    canonical,
    check_separation,
    load_split,
    sha256,
    write_split,
)
from tests.cases_data import CONFIG, ROOT, SMALL_MIX, UNUSED_MERCHANT, EchoLLM, make_context

RULES = LabelRules.from_config(CONFIG)
Written = tuple[list[BaseCase], Path]
NOISE = NoiseConfig.from_config(CONFIG)


def _truth(**extra: object) -> Truth:
    fields: dict[str, object] = {
        "transaction_id": "TX-1",
        "timestamp": datetime(2026, 3, 2, 10, 0),
        "country_code": "MX",
        "segment": "Basic",
        "product_type": "credit_card",
        "transaction_type": "Purchase",
        "status": "Approved",
        "channel": "POS",
        "amount": 300.0,
        "currency": "USD",
        "amount_usd": 300.0,
        "local_amount": 5100.0,
        "local_currency": "MXN",
        "merchant_name": "Farmacia Norte",
        "merchant_category": "Health",
        "city": "Ciudad",
        "transaction_country": "MX",
        "card_in_possession": True,
    }
    fields.update(extra)
    return Truth(**fields)


def _noise(said: bool = True) -> Noise:
    return Noise(
        amount=AmountClue(mentioned=said, form="exact", value=300.0, currency="USD"),
        date=DateClue(
            mentioned=said,
            form="exact",
            expression="exact",
            window_start=date(2026, 3, 2),
            window_end=date(2026, 3, 2),
        ),
        merchant=MerchantClue(mentioned=said, form="complete", value="Farmacia Norte"),
        channel=Mention(mentioned=False),
        product=Mention(mentioned=False),
        card_possession=Mention(mentioned=False),
    )


# --- CA3 labels: design 8, independent of the policy engine -----------------------------------


@pytest.mark.parametrize(
    ("intent", "truth", "scenario", "action"),
    [
        ("unrecognized_charge", {}, {}, "register_and_offer_block"),
        ("unrecognized_charge", {"card_in_possession": False}, {}, "register_and_block"),
        ("unrecognized_charge", {"amount_usd": 500.0}, {}, "register_and_offer_block"),
        ("unrecognized_charge", {"amount_usd": 500.01}, {}, "analyst_approval"),
        ("unrecognized_charge", {"amount_usd": 1000.0}, {}, "analyst_approval"),
        ("unrecognized_charge", {"amount_usd": 1000.01}, {}, "escalate"),
        ("unrecognized_charge", {"open_dispute_claims": 1}, {}, "escalate"),
        ("unrecognized_charge", {}, {"tool_failure": "register_dispute"}, "escalate"),
        ("unrecognized_charge", {}, {"nonexistent_charge": True}, "escalate"),
        ("unrecognized_charge", {"recognizes_after_detail": True}, {}, "recognized_closed"),
        # Recognition comes before the decision (design 5): no amount rule applies.
        (
            "unrecognized_charge",
            {"recognizes_after_detail": True, "amount_usd": 5000.0},
            {},
            "recognized_closed",
        ),
        # Security comes first (design 8), even above the amount rules.
        ("unrecognized_charge", {"amount_usd": 5000.0}, {"injection": True}, "security_blocked"),
        ("unrecognized_charge", {}, {"other_customer_id": "C9"}, "security_blocked"),
        ("unrecognized_charge", {}, {"session_expires_at_turn": 2}, "expired"),
        ("billing_error_amount", {}, {"agreed_amount": 250.0}, "register"),
        (
            "billing_error_duplicate",
            {},
            {"fixture_rows": [{"transaction_status": "Pending"}]},
            "explain_and_watch",
        ),
        (
            "billing_error_duplicate",
            {},
            {"fixture_rows": [{"transaction_status": "Approved"}]},
            "register",
        ),
        ("out_of_scope", {}, {"topic": "loan"}, "abstain_and_redirect"),
        ("claim_status", {"open_dispute_claims": 1}, {}, "report_claim_status"),
    ],
)
def test_expected_action_follows_design_section_8(
    intent: str, truth: dict, scenario: dict, action: str
) -> None:
    got = expected_action(intent, _truth(**truth), _noise(), Scenario(**scenario), RULES)
    assert got.action == action
    assert got.rule.startswith("design 4.2 ")


def test_first_step_is_constrained_only_where_the_case_builds_it() -> None:
    silent = expected_action("unrecognized_charge", _truth(), _noise(False), Scenario(), RULES)
    vague = expected_action(
        "unrecognized_charge", _truth(), _noise(), Scenario(weak_clues=True), RULES
    )
    plain = expected_action("unrecognized_charge", _truth(), _noise(), Scenario(), RULES)
    assert (silent.first_step, vague.first_step, plain.first_step) == (
        "ask_data",
        "show_options",
        "any",
    )


def test_labels_never_read_the_policy_engine() -> None:
    """The oracle of TRZ-17 must not come from TRZ-17."""
    for path in sorted((ROOT / "pipeline/cases").glob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
            and n.body
            and isinstance(n.body[0], ast.Expr)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and id(node) not in docstrings:
                assert "policy.yaml" not in str(node.value), path.name
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("app.domain.policy"), path.name
            if isinstance(node, ast.Import):
                assert all(not a.name.startswith("app.domain.policy") for a in node.names)


# --- CA2 noise -------------------------------------------------------------------------------


def test_presence_follows_the_joint_table() -> None:
    table = {(True, True): 0.22, (True, False): 0.11, (False, True): 0.44, (False, False): 0.23}
    rng = random.Random(1)
    draws = [draw_presence(rng, table) for _ in range(20_000)]
    amount = sum(a for a, _ in draws) / len(draws)
    product = sum(p for _, p in draws) / len(draws)
    assert abs(amount - 0.33) < 0.015
    assert abs(product - 0.66) < 0.015


def test_every_stated_amount_is_compatible_with_the_true_one() -> None:
    truth = _truth(amount=318.4, local_amount=5412.8)
    forms = set()
    for i in range(3000):
        clue = draw_amount(truth, random.Random(i), NOISE, True)
        forms.add((clue.form, clue.qualifier, clue.currency))
        true = truth.amount if clue.currency == "USD" else truth.local_amount
        assert clue.low is not None and clue.high is not None
        assert clue.low <= true <= clue.high, clue
        if clue.form == "exact":
            assert clue.currency == "USD"
    assert {f[0] for f in forms} == {"exact", "rounded", "approximate"}
    assert {"MXN", "USD"} <= {f[2] for f in forms}
    assert ("approximate", "more_than", "USD") in forms or (
        "approximate",
        "more_than",
        "MXN",
    ) in forms


def test_date_windows_always_contain_the_true_date() -> None:
    today = date(2026, 3, 20)
    for back in range(0, 60):
        tx_day = today - timedelta(days=back)
        for i in range(20):
            clue = draw_date(tx_day, today, random.Random(i), NOISE, True)
            assert clue is not None and clue.window_start and clue.window_end
            assert clue.window_start <= tx_day <= clue.window_end, (back, clue.expression)
    assert relative_windows(today)["yesterday"] == (date(2026, 3, 19), date(2026, 3, 19))


def test_misspelling_changes_the_name() -> None:
    for i in range(200):
        assert misspell("Farmacia Norte", random.Random(i)) != "Farmacia Norte"


# --- CA1, CA5, CA7 sampling and splits -------------------------------------------------------


@pytest.fixture(scope="module")
def ctx() -> Context:
    return make_context()


def test_every_case_comes_from_a_real_record_of_its_split(ctx: Context) -> None:
    sample = sample_split(ctx, "test", SMALL_MIX, "generator_b")
    assert len(sample.bases) == sum(SMALL_MIX.values())
    for b in sample.bases:
        assert ctx.buckets[b.customer_id] == "test"
        if b.category == "claim_status":
            assert b.truth.complaint_id in {c.complaint_id for c in ctx.claims[b.customer_id]}
            continue
        source = {t.transaction_id: t for t in ctx.by_customer[b.customer_id]}
        assert b.truth.transaction_id in source
        assert b.truth.timestamp == source[b.truth.transaction_id].ts < b.now


def test_each_category_gets_the_action_it_was_built_for(ctx: Context) -> None:
    got = {b.category: b for b in sample_split(ctx, "test", SMALL_MIX, "generator_b").bases}
    assert got["high_amount"].expected.action == "escalate"
    assert got["duplicate_pending"].expected.action == "explain_and_watch"
    assert got["duplicate_pending"].scenario.fixture_rows[0]["constructed"] is True
    assert got["no_match"].expected.action == "escalate"
    assert got["no_match"].noise.merchant.value == UNUSED_MERCHANT
    assert got["claim_status"].expected.action == "report_claim_status"
    assert got["other_customer"].scenario.other_customer_id not in (None, "")
    assert got["ambiguous"].expected.first_step == "show_options"


def test_splits_share_no_customer_and_follow_each_other_in_time(ctx: Context) -> None:
    llm = EchoLLM()
    para = _paraphraser(llm, Path(tempfile.mkdtemp()))
    gen = Generator.load("a", ROOT, 0.7)
    split_cases = {}
    for split in ("dev", "calibration", "test"):
        bases = sample_split(ctx, split, SMALL_MIX, "generator_a").bases
        split_cases[split] = [
            c for b in bases for c in render_base(b, gen, para, 42, ctx.merchants, {})
        ]
    check_separation(split_cases)
    customers = [{c.customer_id for c in cs} for cs in split_cases.values()]
    assert not (
        customers[0] & customers[1] or customers[0] & customers[2] or customers[1] & customers[2]
    )
    last_dev = max(c.now for c in split_cases["dev"])
    first_test = min(c.truth.timestamp or c.truth.claim_created for c in split_cases["test"])
    assert last_dev < first_test


def test_check_separation_rejects_a_shared_customer(ctx: Context) -> None:
    para = _paraphraser(EchoLLM(), Path(tempfile.mkdtemp()))
    gen = Generator.load("a", ROOT, 0.7)
    base = sample_split(ctx, "dev", {"normal": 1}, "generator_a").bases[0]
    case = render_base(base, gen, para, 42, ctx.merchants, {})[0]
    moved = case.model_copy(update={"split": "test"})
    with pytest.raises(ValueError, match="customers are in both"):
        check_separation({"dev": [case], "test": [moved]})


def test_same_seed_gives_identical_bytes_and_another_seed_does_not() -> None:
    def build(seed: int) -> bytes:
        ctx = make_context(seed=seed)
        para = _paraphraser(EchoLLM(), Path(tempfile.mkdtemp()))
        gen = Generator.load("b", ROOT, 1.0)
        bases = sample_split(ctx, "test", SMALL_MIX, "generator_b").bases
        return canonical(
            [c for b in bases for c in render_base(b, gen, para, seed, ctx.merchants, {})]
        )

    assert sha256(build(42)) == sha256(build(42))
    assert sha256(build(42)) != sha256(build(7))


def test_mix_in_config_has_300_to_400_test_cases_with_60_handwritten() -> None:
    mix = CONFIG["mix"]
    handwritten_bases = sum(mix["handwritten"].values())
    test_bases = sum(mix["test"].values()) + handwritten_bases
    assert handwritten_bases * 4 == 60
    assert 300 <= test_bases * 4 <= 400
    required = {
        "normal", "ambiguous", "out_of_scope", "high_amount", "recognized", "fraud_card_lost",
        "duplicate_pending", "injection", "other_customer", "session_expired", "missing_data",
        "no_match", "tool_failure", "multilingual", "claim_status",
    }  # fmt: skip
    assert required <= set(mix["test"])
    assert sum(mix["dev"].values()) == 150 and sum(mix["calibration"].values()) == 100


# --- CA6 frozen hash ----------------------------------------------------------------------------


def _cases(ctx: Context) -> list[CaseRecord]:
    para = _paraphraser(EchoLLM(), Path(tempfile.mkdtemp()))
    gen = Generator.load("b", ROOT, 1.0)
    bases = sample_split(ctx, "test", {"normal": 2}, "generator_b").bases
    return [c for b in bases for c in render_base(b, gen, para, 42, ctx.merchants, {})]


def test_a_changed_test_block_is_refused_unless_refrozen(ctx: Context, tmp_path: Path) -> None:
    cases = _cases(ctx)
    digest = write_split(tmp_path, "test_generated", cases, {})
    manifest = {"splits": {"test_generated": {"sha256": digest}}}
    assert write_split(tmp_path, "test_generated", cases, manifest) == digest
    changed = [cases[0].model_copy(update={"message": "otro"}), *cases[1:]]
    with pytest.raises(SplitMismatch):
        write_split(tmp_path, "test_generated", changed, manifest)
    assert write_split(tmp_path, "test_generated", changed, manifest, refreeze=True) != digest
    # The handwritten block is written and frozen on its own, later.
    assert write_split(tmp_path, "test_handwritten", changed[:1], manifest)


def _frozen_test_split(ctx: Context, folder: Path) -> tuple[Path, int]:
    cases = _cases(ctx)
    hashes = {
        part: {"sha256": write_split(folder, part, block, {})}
        for part, block in (("test_generated", cases[:4]), ("test_handwritten", cases[4:]))
    }
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps({"splits": hashes}))
    return manifest, len(cases)


def _cases_config(folder: Path, text: str) -> Path:
    path = folder / "cases.yaml"
    path.write_text(text)
    return path


def test_held_out_is_refused_until_both_blocks_are_frozen(ctx: Context, tmp_path: Path) -> None:
    cases = _cases(ctx)
    generated, written_by_hand = cases[:4], cases[4:]
    hashes = {"test_generated": {"sha256": write_split(tmp_path, "test_generated", generated, {})}}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"splits": hashes}))
    unlocked = _cases_config(tmp_path, "held_out_locked: false\n")
    with pytest.raises(HeldOutNotFrozen, match="test_handwritten"):
        load_split(tmp_path, "test", manifest, unlocked)

    hashes["test_handwritten"] = {
        "sha256": write_split(tmp_path, "test_handwritten", written_by_hand, {})
    }
    manifest.write_text(json.dumps({"splits": hashes}))
    assert len(load_split(tmp_path, "test", manifest, unlocked)) == len(cases)

    block = tmp_path / "test_handwritten.jsonl"
    block.write_bytes(block.read_bytes() + b"\n")
    with pytest.raises(SplitMismatch):
        load_split(tmp_path, "test", manifest, unlocked)


@pytest.mark.parametrize("config", ["held_out_locked: true\n", "version: 1\n", ""])
def test_a_frozen_held_out_stays_locked_unless_the_config_opens_it(
    ctx: Context, tmp_path: Path, config: str
) -> None:
    manifest, _ = _frozen_test_split(ctx, tmp_path)
    with pytest.raises(HeldOutLocked, match="held_out_locked"):
        load_split(tmp_path, "test", manifest, _cases_config(tmp_path, config))


def test_the_repository_config_keeps_the_held_out_locked(ctx: Context, tmp_path: Path) -> None:
    manifest, _ = _frozen_test_split(ctx, tmp_path)
    with pytest.raises(HeldOutLocked):
        load_split(tmp_path, "test", manifest, ROOT / "config" / "cases.yaml")


def test_dev_and_calibration_load_without_the_held_out(ctx: Context, tmp_path: Path) -> None:
    cases = _cases(ctx)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"splits": {"dev": {"sha256": write_split(tmp_path, "dev", cases, {})}}})
    )
    locked = _cases_config(tmp_path, "held_out_locked: true\n")
    assert len(load_split(tmp_path, "dev", manifest, locked)) == len(cases)


# --- CA4, CA8 generators and variants ---------------------------------------------------------

SENTENCES = (
    "openers",
    "billing",
    "duplicate",
    "injection",
    "other_customer",
    "code_mixed",
    "claim_status",
    "out_of_scope",
    "closers",
    "card",
)


def _leaves(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {s for v in value.values() for s in _leaves(v)}
    if isinstance(value, list):
        return {s for v in value for s in _leaves(v)}
    return set()


def test_generators_a_and_b_share_no_sentence_template_and_no_prompt() -> None:
    a = yaml.safe_load((ROOT / "eval/templates/generator_a.yaml").read_text("utf-8"))
    b = yaml.safe_load((ROOT / "eval/templates/generator_b.yaml").read_text("utf-8"))
    for variant in VARIANTS:
        sa = _leaves({k: a["variants"][variant][k] for k in SENTENCES})
        sb = _leaves({k: b["variants"][variant][k] for k in SENTENCES})
        assert not sa & sb, (variant, sa & sb)
    assert a["order"] != b["order"]
    prompt_a = (ROOT / "eval/prompts/generator_a.md").read_text("utf-8")
    prompt_b = (ROOT / "eval/prompts/generator_b.md").read_text("utf-8")
    assert prompt_a != prompt_b


def test_every_base_case_exists_in_the_four_variants(ctx: Context) -> None:
    para = _paraphraser(EchoLLM(), Path(tempfile.mkdtemp()))
    for name in ("a", "b"):
        gen = Generator.load(name, ROOT, 0.7)
        for b in sample_split(ctx, "dev", SMALL_MIX, "generator_a").bases:
            cases = render_base(b, gen, para, 42, ctx.merchants, {})
            assert [c.variant for c in cases] == list(VARIANTS)
            assert {c.base_id for c in cases} == {b.base_id}
            assert [c.language for c in cases] == ["es", "es", "es", "pt"]


# --- CA10 what reaches the LLM ----------------------------------------------------------------

ALLOWED_KEYS = {
    "intent",
    "code_mixed",
    "variation",
    "variants",
    "language",
    "draft",
    "style",
    *VARIANTS,
}


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_prompts_carry_only_allowed_fields_and_no_identifier(ctx: Context) -> None:
    llm = EchoLLM()
    para = _paraphraser(llm, Path(tempfile.mkdtemp()))
    gen = Generator.load("b", ROOT, 1.0)
    bases = sample_split(ctx, "test", SMALL_MIX, "generator_b").bases
    for b in bases:
        render_base(b, gen, para, 42, ctx.merchants, {})
    assert len(llm.prompts) == len(bases)
    forbidden = set()
    for b in bases:
        forbidden |= {b.customer_id, b.base_id}
        forbidden |= {x for x in (b.truth.transaction_id, b.truth.complaint_id) if x}
        forbidden |= {
            x for x in (b.scenario.other_customer_id, b.scenario.twin_transaction_id) if x
        }
        forbidden |= {t.product_id for t in ctx.by_customer[b.customer_id]}
    for system, user in llm.prompts:
        assert _keys(json.loads(user)) <= ALLOWED_KEYS
        PromptFacts.model_validate_json(user)
        for value in forbidden:
            assert value not in user and value not in system
    other = next(b for b in bases if b.category == "other_customer")
    assert any("{OTHER_ID}" in user for _, user in llm.prompts)
    cases = render_base(other, gen, para, 42, ctx.merchants, {})
    assert all(other.scenario.other_customer_id in c.message for c in cases)


def test_prompt_facts_refuse_any_other_field() -> None:
    with pytest.raises(ValueError):
        PromptFacts.model_validate(
            {
                "intent": "claim_status",
                "code_mixed": False,
                "variation": 1,
                "variants": {},
                "customer_id": "C1",
            }
        )


# --- paraphrase checks, cache and budget -------------------------------------------------------


def _base(ctx: Context) -> BaseCase:
    def full(b: BaseCase) -> bool:
        m = b.noise.merchant
        return m.mentioned and m.form == "complete" and b.noise.amount.mentioned

    return next(filter(full, sample_split(ctx, "dev", {"normal": 20}, "generator_a").bases))


def test_check_paraphrase_rejects_changed_numbers_merchants_tokens_and_personal_data(
    ctx: Context,
) -> None:
    base = _base(ctx)
    gen = Generator.load("a", ROOT, 0.7)
    source = draft(base, "es-MX", gen, 42)
    assert check_paraphrase(source, source, base, ctx.merchants) == []
    assert check_paraphrase(source + " Fueron 99.", source, base, ctx.merchants)
    assert check_paraphrase(f"{source} También en {UNUSED_MERCHANT}.", source, base, ctx.merchants)
    assert check_paraphrase(source + " Escríbanme a ana@correo.com", source, base, ctx.merchants)
    assert check_paraphrase("Hola {OTHER_ID}", "Hola", base, ctx.merchants)
    merchant = base.noise.merchant.value or ""
    assert check_paraphrase(source.replace(merchant, "una tienda"), source, base, ctx.merchants)


def _paraphraser(complete: Complete | None, cache_dir: Path, max_calls: int = 1000) -> Paraphraser:
    return Paraphraser(
        complete=complete,
        model="test-model",
        cache=LLMCache(cache_dir / "cache.jsonl"),
        max_tokens=900,
        max_calls=max_calls,
        max_cost_usd=100.0,
        price_in=1.0,
        price_out=5.0,
    )


def test_cached_answers_cost_nothing_the_second_time(ctx: Context, tmp_path: Path) -> None:
    base = _base(ctx)
    gen = Generator.load("a", ROOT, 0.7)
    first = _paraphraser(EchoLLM(), tmp_path)
    cases = render_base(base, gen, first, 42, ctx.merchants, {})
    assert first.usage.calls == 1
    offline = _paraphraser(None, tmp_path)
    again = render_base(base, gen, offline, 42, ctx.merchants, {})
    assert offline.usage.calls == 0 and offline.usage.cache_hits == 1
    assert [c.message for c in again] == [c.message for c in cases]
    assert offline.tokens(offline.used) == first.tokens(first.used)


def test_invalid_paraphrase_is_retried_once_then_falls_back_to_the_draft(
    ctx, tmp_path: Path
) -> None:
    base = _base(ctx)
    gen = Generator.load("a", ROOT, 0.7)
    calls = []

    def bad(
        system: str, user: str, max_tokens: int, temperature: float
    ) -> tuple[str, LLMCallStats]:
        calls.append(system)
        return json.dumps({v: "Fueron 12345 pesos." for v in VARIANTS}), LLMCallStats(10, 10)

    cases = render_base(base, gen, _paraphraser(bad, tmp_path), 42, ctx.merchants, {})
    assert len(calls) == 2 and "broke these rules" in calls[1]
    assert {c.message_source for c in cases} == {"template"}
    assert cases[0].message == draft(base, "es-MX", gen, 42)


def test_the_budget_stops_the_run_before_the_next_call(ctx: Context, tmp_path: Path) -> None:
    gen = Generator.load("a", ROOT, 0.7)
    para = _paraphraser(EchoLLM(), tmp_path, max_calls=1)
    bases = sample_split(ctx, "dev", {"normal": 2}, "generator_a").bases
    render_base(bases[0], gen, para, 42, ctx.merchants, {})
    with pytest.raises(BudgetExceeded):
        render_base(bases[1], gen, para, 42, ctx.merchants, {})


# --- CA7 to CA9 handwritten cases -------------------------------------------------------------


@pytest.fixture
def written(ctx: Context, tmp_path: Path) -> Written:
    bases = sample_split(ctx, "test", {"normal": 2, "out_of_scope": 1}, "handwritten").bases
    handwritten.export_template(bases, tmp_path)
    return bases, tmp_path


def test_template_shows_no_identifier_and_is_never_overwritten(
    ctx: Context, written: Written
) -> None:
    bases, folder = written
    text = (folder / handwritten.TEMPLATE).read_text("utf-8")
    for b in bases:
        assert b.customer_id not in text
        assert not b.truth.transaction_id or b.truth.transaction_id not in text
        assert all(t.product_id not in text for t in ctx.by_customer[b.customer_id])
    with pytest.raises(FileExistsError):
        handwritten.export_template(bases, folder)
    assert [b.base_id for b in handwritten.load_bases(folder)] == [b.base_id for b in bases]


def _fill(folder: Path, text: str) -> None:
    path = folder / handwritten.TEMPLATE
    data = yaml.safe_load(path.read_text("utf-8"))
    for case in data["casos"]:
        case["mensajes"] = {v: f"{text} {case['base_id']} {v}" for v in VARIANTS}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), "utf-8")


def test_check_blocks_missing_messages_and_personal_data(written: Written) -> None:
    bases, folder = written
    errors, _ = handwritten.check(bases, handwritten.load_messages(folder))
    assert len(errors) == len(bases) * 4
    _fill(folder, "Mi correo es ana@correo.com, no reconozco un cargo")
    errors, _ = handwritten.check(bases, handwritten.load_messages(folder))
    assert errors and all("personal data" in e for e in errors)
    _fill(folder, "No reconozco un cargo de 300")
    errors, _ = handwritten.check(bases, handwritten.load_messages(folder))
    assert errors == []


def test_handwritten_records_mark_assisted_and_non_native_variants(written: Written) -> None:
    bases, folder = written
    _fill(folder, "No reconozco un cargo")
    cases = handwritten.records(bases, handwritten.load_messages(folder), {})
    flags = {c.variant: c.non_native_writer for c in cases}
    assert flags == {"es-MX": False, "es-CO": False, "es-AR": True, "pt-BR": True}
    assert {c.message_source for c in cases} == {"assisted"}


def _answer(folder: Path, n: int, when: str, bases: list[BaseCase], flip: int = 0) -> None:
    path = folder / f"revision_{n}.yaml"
    data = yaml.safe_load(path.read_text("utf-8"))
    truth = {b.base_id: handwritten.truth_labels(b) for b in bases}
    data["terminada"] = when
    for i, item in enumerate(data["items"]):
        labels = truth[item["base_id"]]
        for f in handwritten.REVIEW_FIELDS:
            value = labels[f]
            item[f] = value == "True" if value in ("True", "False") else value
        if i < flip:
            item["accion"] = "escalate"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), "utf-8")


def test_agreement_needs_a_day_between_reviews_and_reports_kappa(written: Written) -> None:
    bases, folder = written
    _fill(folder, "No reconozco un cargo")
    messages = handwritten.load_messages(folder)
    handwritten.export_review(bases, messages, folder, 1)
    handwritten.export_review(bases, messages, folder, 2)
    sheet = (folder / "revision_2.yaml").read_text("utf-8")
    assert "register_and_offer_block (" not in sheet  # blind: no constructed label shown
    _answer(folder, 1, "2026-09-28 10:00", bases)
    _answer(folder, 2, "2026-09-28 20:00", bases, flip=2)
    with pytest.raises(ValueError, match="hours apart"):
        handwritten.agreement(folder, bases)
    _answer(folder, 2, "2026-09-29 11:00", bases, flip=2)
    result = handwritten.agreement(folder, bases)
    items = len(bases) * 4
    assert result["items"] == items
    assert result["fields"]["accion"]["between_reviews"] == round((items - 2) / items, 4)
    assert result["fields"]["accion"]["review_1_vs_labels"] == 1.0
    assert result["fields"]["monto"]["between_reviews"] == 1.0


def test_cohen_kappa_reference_values() -> None:
    assert handwritten.cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    assert handwritten.cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0
    assert handwritten.cohen_kappa(["a", "a"], ["a", "a"]) is None


def test_injection_text_skips_the_llm_and_always_reaches_the_message(ctx: Context) -> None:
    llm = EchoLLM()
    para = _paraphraser(llm, Path(tempfile.mkdtemp()))
    gen = Generator.load("b", ROOT, 1.0)
    base = next(
        b for b in sample_split(ctx, "test", SMALL_MIX, "generator_b").bases if b.scenario.injection
    )
    cases = render_base(base, gen, para, 42, ctx.merchants, {})
    bank = yaml.safe_load((ROOT / "eval/templates/generator_b.yaml").read_text("utf-8"))
    (_, user) = llm.prompts[0]
    assert "{INJECTION}" in user
    for c in cases:
        attacks = bank["variants"][c.variant]["injection"]
        assert all(a not in user for a in attacks)
        assert any(a in c.message for a in attacks) and "{INJECTION}" not in c.message
    dropped = draft(base, "es-MX", gen, 42).replace("{INJECTION}", "")
    assert check_paraphrase(dropped, draft(base, "es-MX", gen, 42), base, ctx.merchants)


def test_generator_windows_are_the_service_windows() -> None:
    # One table for both: a case is labelled with the window the service resolves.
    for offset in range(60):
        today = date(2026, 4, 1) + timedelta(days=offset)
        clock = SimulatedClock(datetime(today.year, today.month, today.day, 23, 59))
        for key, (first, last) in relative_windows(today).items():
            args = ("weeks_ago", 2) if key == "two_weeks_ago" else (key,)
            assert clock.relative_window(*args) == ((today - last).days, (today - first).days)
