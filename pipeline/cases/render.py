"""Writes a base case in the four variants (TRZ-42 CA4, CA8, CA10).

Each generator has its own template bank (eval/templates) and prompt (eval/prompts). A draft is
built per variant from the stated clues only; the LLM paraphrases the four drafts in one call.
A deterministic validator then checks each paraphrase against its draft: the same numbers, the
stated merchant and no other, the {OTHER_ID} token kept, and nothing the PII redaction would
catch. A failed variant gets one retry that names the broken rules; if it fails again, the draft
itself is the message and the case says so (`message_source: template`).

What reaches the LLM is `PromptFacts`: the intent, whether to mix languages, and per variant the
language, the draft and a style. Drafts are built from the noise, so they carry the merchant,
amount, currency, date, channel, product type and card possession the customer states, never an
id, a name or a document. The id of another customer and the attack text of an injection case
are inserted after the LLM, in place of the tokens {OTHER_ID} and {INJECTION}.

Answers are cached in a local JSONL file keyed by the hash of the model, prompt and parameters,
so running `make cases` again costs nothing; a budget stops the run before it spends more than
configured.
"""

import hashlib
import json
import random
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from app.adapters.llm import LLMCallStats
from app.domain.pii import redact
from pipeline.cases.sampling import md5_int
from pipeline.cases.schema import VARIANTS, AmountClue, BaseCase, CaseRecord, Intent, Variant

GENERATOR_VERSION = "1"
OTHER_ID = "{OTHER_ID}"
# The attack text of an injection case goes in after the LLM: a paraphraser softens or drops it.
INJECTION = "{INJECTION}"
STYLES = ("whatsapp_hurried", "formal_email", "voice_transcript", "upset_customer")
LANGUAGES: dict[Variant, str] = {
    "es-MX": "Mexican Spanish",
    "es-CO": "Colombian Spanish",
    "es-AR": "Argentine Spanish (voseo)",
    "pt-BR": "Brazilian Portuguese",
}
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WEEKDAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

Complete = Callable[[str, str, int, float], tuple[str, LLMCallStats]]


class VariantDraft(BaseModel):
    """What the LLM gets for one variant.

    Attributes:
        language: Language and variant, in words.
        draft: Draft message built from the stated clues.
        style: Customer style (generator B only).
    """

    model_config = ConfigDict(extra="forbid")

    language: str
    draft: str
    style: str | None = None


class PromptFacts(BaseModel):
    """Everything the generator sends to the LLM for one base case (TRZ-42 CA10).

    Attributes:
        intent: Intent of the scenario.
        code_mixed: Whether the customer mixes languages.
        variation: Number drawn from the seed. Two base cases can have the same drafts (a claim
            status or an out-of-scope topic); it makes them different requests, so they get
            different paraphrases. It carries no data.
        variants: Draft per variant.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    code_mixed: bool
    variation: int
    variants: dict[Variant, VariantDraft]


@dataclass(frozen=True)
class Generator:
    """A template bank and a prompt.

    Attributes:
        name: a or b.
        provenance: generator_a or generator_b.
        bank: Parsed eval/templates/generator_<name>.yaml.
        system: System prompt, without its version line.
        prompt_version: Version line of the prompt file.
        temperature: Sampling temperature.
    """

    name: Literal["a", "b"]
    provenance: Literal["generator_a", "generator_b"]
    bank: dict[str, Any]
    system: str
    prompt_version: str
    temperature: float

    @classmethod
    def load(cls, name: Literal["a", "b"], root: Path, temperature: float) -> "Generator":
        """Reads the template bank and the prompt of a generator.

        Args:
            name: a or b.
            root: Repository root.
            temperature: Sampling temperature.

        Returns:
            The generator.
        """
        bank = yaml.safe_load((root / f"eval/templates/generator_{name}.yaml").read_text("utf-8"))
        prompt = (root / f"eval/prompts/generator_{name}.md").read_text("utf-8")
        first, _, system = prompt.partition("\n")
        return cls(
            name=name,
            provenance="generator_a" if name == "a" else "generator_b",
            bank=bank,
            system=system.strip(),
            prompt_version=first.removeprefix("version:").strip(),
            temperature=temperature,
        )

    def versions(self, model: str, config_version: str) -> dict[str, str]:
        """Versions stamped on every case this generator writes.

        Args:
            model: LLM model id.
            config_version: Version of config/cases.yaml.

        Returns:
            Name to version.
        """
        return {
            "generator": GENERATOR_VERSION,
            "templates": f"{self.name}-{self.bank['version']}",
            "prompt": f"{self.name}-{self.prompt_version}",
            "model": model,
            "config": config_version,
        }


def fmt_number(value: float, vb: dict[str, Any], decimals: bool) -> str:
    """Writes a number with the separators of a variant.

    Args:
        value: Number.
        vb: Variant block of a template bank.
        decimals: Keep two decimals.

    Returns:
        The number as text.
    """
    text = f"{value:,.2f}" if decimals else f"{round(value):,}"
    sep = vb["number"]
    return text.replace(",", "\0").replace(".", sep["decimal"]).replace("\0", sep["thousands"])


def money_text(value: float, currency: str, exact: bool, vb: dict[str, Any], local: str) -> str:
    """An amount and its currency in a variant, e.g. "5,500 pesos" or "110 lucas".

    Args:
        value: Amount.
        currency: Currency code.
        exact: Keep two decimals.
        vb: Variant block of a template bank.
        local: Local currency of the customer's country ("lucas" only apply to it).

    Returns:
        The text.
    """
    word = vb.get("thousands_word")
    if (
        word
        and not exact
        and currency == local
        and local in ("ARS", "COP")
        and value >= 1000
        and value % 1000 == 0
    ):
        return str(word.format(n=fmt_number(value / 1000, vb, False)))
    return f"{fmt_number(value, vb, exact)} {vb['currency'][currency]}"


def amount_text(clue: AmountClue, vb: dict[str, Any], local: str) -> str:
    """Amount phrase of a variant, e.g. "de como 5,500 pesos".

    Args:
        clue: Amount clue; needs value, currency and form.
        vb: Variant block of a template bank.
        local: Local currency of the customer's country.

    Returns:
        The phrase.
    """
    assert clue.value is not None and clue.currency is not None and clue.form is not None
    text = money_text(clue.value, clue.currency, clue.form == "exact", vb, local)
    return str(vb["amount"][clue.qualifier or clue.form].format(amount=text))


def _clauses(base: BaseCase, vb: dict[str, Any], order: list[str]) -> list[str]:
    n, t = base.noise, base.truth
    parts: dict[str, str] = {}
    if n.amount.mentioned:
        parts["amount"] = amount_text(n.amount, vb, t.local_currency)
    if n.merchant.mentioned and n.merchant.value:
        if n.merchant.form == "category":
            words = vb["categories"][n.merchant.value]
            parts["merchant"] = vb["category"].format(category=words)
        else:
            parts["merchant"] = vb["merchant"].format(merchant=n.merchant.value)
    if n.date.mentioned and n.date.expression and n.date.window_start:
        key, d = n.date.expression, n.date.window_start
        if key == "exact":
            parts["date"] = vb["date"]["exact"].format(day=d.day, month=vb["months"][d.month - 1])
        elif key.startswith("last_") and key[5:] in _WEEKDAY_KEYS:
            index = _WEEKDAY_KEYS.index(key[5:])
            # Portuguese: sábado and domingo are masculine, the other weekdays feminine.
            form = (
                "last_weekend_day"
                if index >= 5 and "last_weekend_day" in vb["date"]
                else ("last_weekday")
            )
            parts["date"] = vb["date"][form].format(weekday=vb["weekdays"][index])
        else:
            parts["date"] = vb["date"][key]
    if n.channel.mentioned and t.channel:
        parts["channel"] = vb["channel"][t.channel]
    if n.product.mentioned and t.product_type in vb["product"]:
        parts["product"] = vb["product"][t.product_type]
    return [parts[k] for k in order if k in parts]


def draft(base: BaseCase, variant: Variant, gen: Generator, seed: int) -> str:
    """Draft message of a base case in one variant, from the stated clues only.

    Args:
        base: Base case.
        variant: Language variant.
        gen: Generator.
        seed: Seed of the run.

    Returns:
        The draft.
    """
    rng = random.Random(md5_int(seed, "draft", gen.name, base.base_id, variant))
    vb = gen.bank["variants"][variant]
    s, t = base.scenario, base.truth
    if base.intent == "out_of_scope":
        assert s.topic is not None
        return vb["out_of_scope"][s.topic]
    if base.intent == "claim_status":
        assert t.claim_subcategory is not None
        return vb["claim_status"][t.claim_subcategory]
    opener = rng.choice(vb["openers"][base.intent])
    greeting = rng.choice(gen.bank["greetings"][variant]) if gen.bank["greetings"] else ""
    if not greeting or greeting.endswith((".", "?")):
        opener = opener[0].upper() + opener[1:]
    clauses = _clauses(base, vb, gen.bank["order"])
    sentences = [" ".join(p for p in (greeting, opener, *clauses) if p) + "."]
    if base.noise.card_possession.mentioned and t.card_in_possession is not None:
        sentences.append(vb["card"][str(t.card_in_possession).lower()])
    if s.agreed_amount is not None:
        assert t.currency is not None
        text = money_text(s.agreed_amount, t.currency, False, vb, t.local_currency)
        sentences.append(vb["billing"].format(agreed=text))
    if s.fixture_rows:
        sentences.append(vb["duplicate"])
    if s.injection:
        sentences.append(INJECTION)
    if s.other_customer_id:
        sentences.append(rng.choice(vb["other_customer"]))
    if s.code_mixed:
        mixed = vb["code_mixed"]
        sentences.append(mixed[0].upper() + mixed[1:] + ".")
    sentences.append(rng.choice(vb["closers"]))
    return " ".join(sentences)


def injection_text(base: BaseCase, variant: Variant, gen: Generator, seed: int) -> str:
    """Attack text of an injection case, from the template bank of the generator.

    Args:
        base: Base case.
        variant: Language variant.
        gen: Generator.
        seed: Seed of the run.

    Returns:
        The instruction aimed at the system.
    """
    rng = random.Random(md5_int(seed, "injection", gen.name, base.base_id, variant))
    return str(rng.choice(gen.bank["variants"][variant]["injection"]))


def prompt_facts(base: BaseCase, gen: Generator, seed: int) -> PromptFacts:
    """Builds what the LLM gets for a base case.

    Args:
        base: Base case.
        gen: Generator.
        seed: Seed of the run.

    Returns:
        The prompt facts.
    """
    rng = random.Random(md5_int(seed, "style", base.base_id))
    return PromptFacts(
        intent=base.intent,
        code_mixed=base.scenario.code_mixed,
        variation=md5_int(seed, "variation", base.base_id) % 10_000,
        variants={
            v: VariantDraft(
                language=LANGUAGES[v],
                draft=draft(base, v, gen, seed),
                style=rng.choice(STYLES) if gen.name == "b" else None,
            )
            for v in VARIANTS
        },
    )


def _fold(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )


def _numbers(text: str) -> set[str]:
    return {re.sub(r"\D", "", m) for m in _NUMBER.findall(text)}


def check_paraphrase(text: str, source: str, base: BaseCase, merchants: list[str]) -> list[str]:
    """Rules a paraphrase must keep from its draft.

    Args:
        text: Paraphrase.
        source: Draft it came from.
        base: Base case.
        merchants: Every merchant name of the data.

    Returns:
        Broken rules; empty when the paraphrase is valid.
    """
    broken = []
    if not text.strip() or len(text) > 800:
        broken.append("message must have 1 to 800 characters")
    if _numbers(text) != _numbers(source):
        broken.append("numbers must be exactly those of the draft")
    folded, folded_source = _fold(text), _fold(source)
    m = base.noise.merchant
    if m.mentioned and m.form != "category" and m.value and _fold(m.value) not in folded:
        broken.append(f"merchant must appear as '{m.value}'")
    if any(_fold(x) in folded and _fold(x) not in folded_source for x in merchants):
        broken.append("no other merchant may be named")
    for token in (OTHER_ID, INJECTION):
        if source.count(token) != text.count(token):
            broken.append(f"the token {token} must appear exactly as in the draft")
    if sum(redact(text)[1].values()) > sum(redact(source)[1].values()):
        broken.append("no personal data may be added")
    return broken


class LLMCache:
    """Answers of the LLM, one JSON object per line, keyed by the hash of the request."""

    def __init__(self, path: Path) -> None:
        """Loads the cache file if it exists.

        Args:
            path: DATA_DIR/eval/llm_cache.jsonl.
        """
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text("utf-8").splitlines():
                if line:
                    entry = json.loads(line)
                    self.entries[entry["key"]] = entry

    def get(self, key: str) -> str | None:
        """Cached text of a request.

        Args:
            key: Request hash.

        Returns:
            The text, or None on a miss.
        """
        entry = self.entries.get(key)
        return None if entry is None else str(entry["text"])

    def put(self, key: str, text: str, stats: LLMCallStats) -> None:
        """Stores an answer and appends it to the file.

        Args:
            key: Request hash.
            text: Answer.
            stats: Tokens of the call.
        """
        entry = {
            "key": key,
            "text": text,
            "input_tokens": stats.input_tokens,
            "output_tokens": stats.output_tokens,
        }
        self.entries[key] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class CacheMiss(RuntimeError):
    """An offline run needed an answer the cache does not have."""


class BudgetExceeded(RuntimeError):
    """The next call would pass the configured number of calls or cost."""


@dataclass
class Usage:
    """Calls, cache hits, tokens and cost of a run.

    Attributes:
        calls: Real LLM calls.
        failed_calls: Calls that returned no text.
        cache_hits: Requests answered by the cache.
        input_tokens: Input tokens of real calls.
        output_tokens: Output tokens of real calls.
        cost_usd: Cost of real calls at the configured prices.
    """

    calls: int = 0
    failed_calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class Paraphraser:
    """Asks the LLM through the cache, within a budget.

    Attributes:
        complete: LLM call, or None for an offline run that only reads the cache.
        model: Model id; part of the cache key.
        cache: Answer cache.
        max_tokens: Output cap per call.
        max_calls: Most real calls per run.
        max_cost_usd: Most spend per run.
        price_in: USD per million input tokens.
        price_out: USD per million output tokens.
        usage: What the run spent.
        used: Cache keys of the answers used, in request order.
    """

    complete: Complete | None
    model: str
    cache: LLMCache
    max_tokens: int
    max_calls: int
    max_cost_usd: float
    price_in: float
    price_out: float
    usage: Usage = field(default_factory=Usage)
    used: list[str] = field(default_factory=list)

    def tokens(self, keys: list[str]) -> dict[str, Any]:
        """Tokens and cost of the answers behind some requests, as first paid for.

        Read from the cache, so a rerun that hits the cache reports the same numbers.

        Args:
            keys: Cache keys.

        Returns:
            Requests, input and output tokens, and cost at the configured prices.
        """
        entries = [self.cache.entries[k] for k in keys]
        tin = sum(int(e["input_tokens"]) for e in entries)
        tout = sum(int(e["output_tokens"]) for e in entries)
        return {
            "requests": len(keys),
            "input_tokens": tin,
            "output_tokens": tout,
            "cost_usd": round((tin * self.price_in + tout * self.price_out) / 1e6, 4),
        }

    def ask(self, system: str, user: str, temperature: float) -> str | None:
        """Returns the answer to a request, from the cache or from the LLM.

        Args:
            system: System prompt.
            user: User message.
            temperature: Sampling temperature.

        Returns:
            The text, or None when the call failed.

        Raises:
            CacheMiss: Offline and not cached.
            BudgetExceeded: The call would pass the budget.
        """
        key = hashlib.sha256(
            json.dumps([self.model, system, user, temperature, self.max_tokens]).encode()
        ).hexdigest()
        cached = self.cache.get(key)
        if cached is not None:
            self.usage.cache_hits += 1
            self.used.append(key)
            return cached
        if self.complete is None:
            raise CacheMiss(f"no cached answer for request {key[:12]}")
        if self.usage.calls + 1 > self.max_calls or self.usage.cost_usd >= self.max_cost_usd:
            raise BudgetExceeded(
                f"{self.usage.calls} calls, {self.usage.cost_usd:.4f} USD spent; limits "
                f"{self.max_calls} calls, {self.max_cost_usd} USD"
            )
        text, stats = self.complete(system, user, self.max_tokens, temperature)
        self.usage.calls += 1
        self.usage.input_tokens += stats.input_tokens
        self.usage.output_tokens += stats.output_tokens
        self.usage.cost_usd += (
            stats.input_tokens * self.price_in + stats.output_tokens * self.price_out
        ) / 1e6
        if stats.fallback or not text.strip():
            self.usage.failed_calls += 1
            return None
        self.cache.put(key, text, stats)
        self.used.append(key)
        return text


def _parse(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def render_base(
    base: BaseCase,
    gen: Generator,
    paraphraser: Paraphraser,
    seed: int,
    merchants: list[str],
    versions: dict[str, str],
) -> list[CaseRecord]:
    """Writes a base case in the four variants.

    Args:
        base: Base case.
        gen: Generator.
        paraphraser: LLM access.
        seed: Seed of the run.
        merchants: Every merchant name of the data.
        versions: Versions to stamp on the cases.

    Returns:
        One case per variant.
    """
    facts = prompt_facts(base, gen, seed)
    drafts = {v: d.draft for v, d in facts.variants.items()}
    user = facts.model_dump_json()
    answer = _parse(paraphraser.ask(gen.system, user, gen.temperature))
    broken = {v: check_paraphrase(answer.get(v, ""), drafts[v], base, merchants) for v in VARIANTS}
    failing = {v: rules for v, rules in broken.items() if rules}
    if failing:
        feedback = "\n".join(f"- {v}: {'; '.join(rules)}" for v, rules in sorted(failing.items()))
        retry_system = (
            f"{gen.system}\n\nA previous answer broke these rules; follow them:\n{feedback}"
        )
        retry = _parse(paraphraser.ask(retry_system, user, gen.temperature))
        for v in failing:
            if not check_paraphrase(retry.get(v, ""), drafts[v], base, merchants):
                answer[v] = retry[v]
                broken[v] = []
    records = []
    for v in VARIANTS:
        ok = not broken[v]
        message = answer[v] if ok else drafts[v]
        if base.scenario.other_customer_id:
            message = message.replace(OTHER_ID, base.scenario.other_customer_id)
        if base.scenario.injection:
            message = message.replace(INJECTION, injection_text(base, v, gen, seed))
        records.append(
            CaseRecord(
                **base.model_dump(),
                case_id=f"{base.base_id}-{v.lower()}",
                variant=v,
                language="pt" if v == "pt-BR" else "es",
                message=message,
                message_source="llm" if ok else "template",
                versions=versions,
            )
        )
    return records
