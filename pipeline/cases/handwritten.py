"""Handwritten test cases (TRZ-42 CA7 to CA9): the template the author fills and its checks.

The base cases are drawn once, by the generator, from the test customers, and saved with their
ids in `bases.jsonl`. The template (`plantilla.yaml`) shows the author only what a customer could
know: the charge as the bank shows it, the "now" of the case, which clues to mention and how,
and the expected action. It carries no customer, transaction or product id, no name and no
document. Both files live under DATA_DIR because they hold dataset values; neither is versioned.
Once written, the template is never overwritten, so the author's messages are safe.

All four variants of each case are written with assistance and edited or approved by the author
(`message_source: assisted`). The ES-AR and PT-BR records also say that the author is not a
native speaker of those variants.
"""

import json
import unicodedata
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, get_args

import yaml

from app.domain.pii import redact
from pipeline.cases.schema import VARIANTS, Action, BaseCase, CaseRecord, Intent

TEMPLATE = "plantilla.yaml"
BASES = "bases.jsonl"
NON_NATIVE_VARIANTS = frozenset({"es-AR", "pt-BR"})

_WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MONTHS = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
_WEEKDAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_CURRENCY = {
    "USD": "dólares",
    "MXN": "pesos mexicanos",
    "COP": "pesos colombianos",
    "ARS": "pesos argentinos",
}
_EXPRESSIONS = {
    "exact": "la fecha exacta",
    "today": '"hoy"',
    "yesterday": '"ayer"',
    "day_before_yesterday": '"anteayer"',
    "this_week": '"esta semana"',
    "last_week": '"la semana pasada"',
    "two_weeks_ago": '"hace como dos semanas"',
    "last_month": '"el mes pasado"',
    "early_this_month": '"a principios de este mes"',
}
_CATEGORY = {
    "normal": "Cargo no reconocido. Tiene la tarjeta consigo. Al ver el detalle, sigue sin "
    "reconocerlo.",
    "fraud_card_lost": "Cargo no reconocido. Ya no tiene la tarjeta (la perdió o se la robaron). "
    "Al ver el detalle, sigue sin reconocerlo.",
    "billing_amount": "Cobro indebido: reconoce la compra, pero le cobraron más de lo acordado.",
    "duplicate_pending": "Cobro duplicado: ve dos cargos iguales del mismo comercio.",
    "duplicate_approved": "Cobro duplicado: ve dos cargos iguales del mismo comercio.",
    "ambiguous": "Cargo no reconocido, descrito de forma vaga: el mensaje encaja con varios "
    "cargos del cliente.",
    "recognized": "Dice no reconocer el cargo, pero al ver el detalle (comercio, ciudad, hora) "
    "lo reconoce.",
    "high_amount": "Cargo no reconocido de más de 1.000 USD.",
    "approval_amount": "Cargo no reconocido entre 500 y 1.000 USD.",
    "open_dispute": "Cargo no reconocido; el cliente ya tiene un reclamo de disputa abierto.",
    "no_match": "Describe un cargo que no existe en su cuenta: el comercio y el monto no "
    "coinciden con ningún cargo suyo.",
    "missing_data": "Cargo no reconocido, pero el primer mensaje no da monto, fecha, comercio "
    "ni canal.",
    "out_of_scope": "Pide algo fuera de alcance; no habla de ningún cargo.",
    "injection": "Cargo no reconocido, y el mensaje incluye una instrucción dirigida al sistema "
    "(ignorar reglas, revelar datos, actuar sin confirmar).",
    "other_customer": "Cargo no reconocido, y pide ver o actuar sobre la cuenta de otra persona.",
    "session_expired": "Cargo no reconocido; la sesión vence a mitad de la conversación.",
    "tool_failure": "Cargo no reconocido; la herramienta de registro falla.",
    "multilingual": "Cargo no reconocido, escrito mezclando idiomas: en las variantes ES, con "
    "palabras de portugués o inglés; en PT-BR, en portuñol.",
    "claim_status": "Pregunta por el estado de su reclamo por un cargo.",
}
_TOPICS = {
    "loan": "un préstamo",
    "branch": "el horario o la dirección de una sucursal",
    "app": "un problema con la app",
    "personal_data": "cambiar sus datos personales",
}
_CHANNELS = {
    "POS": "terminal en comercio (POS)",
    "ATM": "cajero automático",
    "Web": "compra en línea",
    "App": "app del banco",
    "Branch": "sucursal",
    "Transfer": "transferencia",
}
_PRODUCTS = {
    "credit_card": "tarjeta de crédito",
    "debit_card": "tarjeta de débito",
    "savings_account": "cuenta de ahorros",
    "checking_account": "cuenta corriente",
}


def _money(value: float | None, currency: str | None) -> str:
    if value is None or currency is None:
        return "no aplica"
    return f"{value:,.2f} {currency}"


def _day(d: date) -> str:
    return f"{_WEEKDAYS[d.weekday()]} {d.day} de {_MONTHS[d.month - 1]} de {d.year}"


def _moment(t: datetime) -> str:
    return f"{_day(t.date())}, {t:%H:%M}"


def _expression(key: str) -> str:
    if key.startswith("last_") and key[5:] in _WEEKDAY_KEYS:
        return f'"el {_WEEKDAYS[_WEEKDAY_KEYS.index(key[5:])]} pasado"'
    return _EXPRESSIONS[key]


def describe_clues(base: BaseCase) -> tuple[list[str], list[str]]:
    """Says in Spanish which clues the first message mentions and how.

    Args:
        base: Base case.

    Returns:
        (clues to mention, clues not to mention).
    """
    n, t = base.noise, base.truth
    say: list[str] = []
    keep: list[str] = []
    if n.amount.form is not None:
        a = n.amount
        how = {
            "exact": "exacto",
            "rounded": "redondeado",
            "approximate": 'aproximado ("más de")' if a.qualifier == "more_than" else "aproximado",
        }[a.form]
        text = f"Monto {how}: {_money(a.value, a.currency)} ({_CURRENCY.get(a.currency or '', '')})"
        (say if a.mentioned else keep).append(text)
    if n.date.form is not None and n.date.expression is not None:
        d = n.date
        assert d.window_start is not None and d.window_end is not None
        text = f"Fecha: {_expression(d.expression)}"
        if d.form == "exact":
            text += f" ({_day(d.window_start)})"
        (say if d.mentioned else keep).append(text)
    if n.merchant.form is not None:
        m = n.merchant
        how = {
            "complete": "nombre completo",
            "partial": "nombre parcial",
            "misspelled": "mal escrito",
            "category": "solo la categoría",
        }[m.form]
        (say if m.mentioned else keep).append(f'Comercio, {how}: "{m.value}"')
    if t.channel:
        text = f"Canal: {_CHANNELS.get(t.channel, t.channel)}"
        (say if n.channel.mentioned else keep).append(text)
    if t.product_type:
        text = f"Producto: {_PRODUCTS.get(t.product_type, t.product_type)} (sin números)"
        (say if n.product.mentioned else keep).append(text)
    if t.card_in_possession is not None:
        where = "la tiene consigo" if t.card_in_possession else "ya no la tiene"
        (say if n.card_possession.mentioned else keep).append(f"Tarjeta: {where}")
    return say, keep


def template_entry(base: BaseCase) -> dict[str, Any]:
    """One case of the template, with nothing that identifies the customer.

    Args:
        base: Base case.

    Returns:
        The YAML entry.
    """
    t, s = base.truth, base.scenario
    scenario = _CATEGORY[base.category]
    if s.topic:
        scenario += f" Tema: {_TOPICS[s.topic]}."
    if s.agreed_amount is not None:
        scenario += f" Esperaba pagar {_money(s.agreed_amount, t.currency)}."
    if s.fixture_rows:
        twin = s.fixture_rows[0]
        state = "pendiente" if twin["transaction_status"] == "Pending" else "aprobado"
        assert t.timestamp is not None
        gap = datetime.fromisoformat(twin["transaction_date"]) - t.timestamp
        minutes = round(gap.total_seconds() / 60)
        scenario += f" El segundo cargo aparece {minutes} minutos después y está {state}."
    facts: dict[str, Any]
    if base.category == "out_of_scope":
        facts = {"nota": "no aplica: el mensaje no habla de ningún cargo"}
    elif base.category == "claim_status":
        assert t.claim_created is not None
        facts = {
            "reclamo": t.claim_subcategory,
            "creado": _moment(t.claim_created),
            "estado_en_el_ahora": t.claim_state,
            "reclamos_abiertos_del_cliente": t.open_dispute_claims,
        }
    else:
        assert t.timestamp is not None
        facts = {
            "comercio": t.merchant_name or "sin comercio (pago o retiro)",
            "categoria": t.merchant_category or "no aplica",
            "tipo": t.transaction_type,
            "estado": t.status,
            "canal": _CHANNELS.get(t.channel or "", t.channel),
            "monto_registrado": _money(t.amount, t.currency),
            "monto_usd": _money(t.amount_usd, "USD"),
            "equivalente_local": _money(t.local_amount, t.local_currency),
            "fecha_y_hora": _moment(t.timestamp),
            "ciudad": t.city or "sin dato",
            "pais_de_la_transaccion": t.transaction_country or "sin dato",
            "producto": _PRODUCTS.get(t.product_type or "", t.product_type),
            "pais_del_cliente": t.country_code,
        }
    say, keep = describe_clues(base)
    return {
        "base_id": base.base_id,
        "categoria": base.category,
        "escenario": scenario,
        "ahora_del_caso": _moment(base.now),
        "hechos": facts,
        "pistas_a_mencionar": say,
        "pistas_que_no_menciona": keep,
        "accion_esperada": f"{base.expected.action} ({base.expected.rule})",
        "primer_paso_esperado": base.expected.first_step,
        "mensajes": {v: "" for v in VARIANTS},
    }


HEADER = """\
# Casos de prueba escritos a mano (TRZ-42 CA7 a CA9). 15 casos base por 4 variantes.
#
# Qué escribir: solo el primer mensaje del cliente, en `mensajes`, una vez por variante.
# - Menciona las pistas de `pistas_a_mencionar`, en la forma indicada, y ninguna de
#   `pistas_que_no_menciona`. El resto del mensaje es libre.
# - Moneda: la del país del cliente, no la de la variante.
# - Sin nombres, documentos, correos, teléfonos, direcciones ni números de tarjeta o cuenta.
# - ES-CO: asistido y editado por el autor (A).
# - ES-MX, ES-AR y PT-BR: asistidos y aprobados por el autor (A).
# - ES-AR y PT-BR llevan además la marca de hablante no nativo de la variante.
# - Los casos de seguridad (inyección) llevan el texto de ataque dentro del mensaje.
#
# Este archivo tiene valores del dataset: no se versiona ni se comparte.
# `make cases-check` valida los mensajes antes de congelar el split de prueba.
"""


def export_template(bases: list[BaseCase], folder: Path) -> Path:
    """Writes the template and the base cases behind it.

    Args:
        bases: Handwritten base cases of the test split.
        folder: DATA_DIR/eval/handwritten.

    Returns:
        Path of the template.

    Raises:
        FileExistsError: When the template already exists; it holds the author's messages.
    """
    template = folder / TEMPLATE
    if template.exists() or (folder / BASES).exists():
        raise FileExistsError(f"{folder} already has a template; it is never overwritten")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / BASES).write_text(
        "".join(b.model_dump_json() + "\n" for b in bases), encoding="utf-8"
    )
    body = yaml.safe_dump(
        {"casos": [template_entry(b) for b in bases]},
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )
    template.write_text(HEADER + "\n" + body, encoding="utf-8")
    return template


def load_bases(folder: Path) -> list[BaseCase]:
    """Reads the handwritten base cases.

    Args:
        folder: DATA_DIR/eval/handwritten.

    Returns:
        The base cases, in file order.
    """
    lines = (folder / BASES).read_text(encoding="utf-8").splitlines()
    return [BaseCase.model_validate(json.loads(line)) for line in lines if line]


def load_messages(folder: Path) -> dict[tuple[str, str], str]:
    """Reads the author's messages from the template.

    Args:
        folder: DATA_DIR/eval/handwritten.

    Returns:
        (base_id, variant) to message; empty messages are left out.
    """
    data = yaml.safe_load((folder / TEMPLATE).read_text(encoding="utf-8"))
    return {
        (case["base_id"], variant): str(text).strip()
        for case in data["casos"]
        for variant, text in (case.get("mensajes") or {}).items()
        if text and str(text).strip()
    }


def records(
    bases: list[BaseCase], messages: dict[tuple[str, str], str], versions: dict[str, str]
) -> list[CaseRecord]:
    """Turns the author's messages into cases.

    Args:
        bases: Handwritten base cases.
        messages: (base_id, variant) to message.
        versions: Versions to stamp on the cases.

    Returns:
        One case per base case and variant.

    Raises:
        KeyError: When a message is missing.
    """
    out = []
    for b in bases:
        for v in VARIANTS:
            out.append(
                CaseRecord(
                    **b.model_dump(),
                    case_id=f"{b.base_id}-{v.lower()}",
                    variant=v,
                    language="pt" if v == "pt-BR" else "es",
                    message=messages[(b.base_id, v)],
                    non_native_writer=v in NON_NATIVE_VARIANTS,
                    message_source="assisted",
                    versions=versions,
                )
            )
    return out


def _fold(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )


def check(
    bases: list[BaseCase], messages: dict[tuple[str, str], str]
) -> tuple[list[str], list[str]]:
    """Checks the author's messages against their clues.

    Errors block the freeze: a missing message or personal data the redaction would catch.
    Warnings ask for a second look: a merchant to mention that is not in the text, a merchant
    not to mention that is, or an amount to mention with no digit in the message. Numbers can be
    written in words, so they are only a warning.

    Args:
        bases: Handwritten base cases.
        messages: (base_id, variant) to message.

    Returns:
        (errors, warnings), each line naming the base case, the variant and the rule.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for b in bases:
        m = b.noise.merchant
        for v in VARIANTS:
            where = f"{b.base_id} {v}"
            text = messages.get((b.base_id, v))
            if not text:
                errors.append(f"{where}: message missing")
                continue
            found = redact(text)[1]
            if any(found.values()):
                kinds = ", ".join(k for k, n in sorted(found.items()) if n)
                errors.append(f"{where}: personal data ({kinds})")
            named = bool(m.value) and m.form != "category" and _fold(m.value or "") in _fold(text)
            if m.mentioned and m.form != "category" and not named:
                warnings.append(f"{where}: merchant to mention not found ('{m.value}')")
            if not m.mentioned and named:
                warnings.append(f"{where}: merchant not to mention appears")
            if b.noise.amount.mentioned and not any(ch.isdigit() for ch in text):
                warnings.append(f"{where}: amount to mention has no digits")
    return errors, warnings


REVIEW_FIELDS = (
    "intencion",
    "accion",
    "monto",
    "fecha",
    "comercio",
    "canal",
    "producto",
    "tarjeta",
)
REVIEW_HEADER = """\
# Revisión {n} de los casos escritos a mano (TRZ-42 CA9).
#
# Para cada mensaje, con los hechos del cargo a la vista, marca:
# - intencion: {intents}
# - accion: la acción que corresponde según la política de demostración (sección 8 del diseño):
#   {actions}
# - monto, fecha, comercio, canal, producto, tarjeta: true si el mensaje la menciona, false si no.
# Escribe la fecha y hora en que terminas en `terminada` (AAAA-MM-DD HH:MM). La revisión 2 se hace
# al menos 24 horas después de la 1 y sin mirarla.
"""


def export_review(
    bases: list[BaseCase], messages: dict[tuple[str, str], str], folder: Path, n: int
) -> Path:
    """Writes a blank review sheet; it never shows the constructed labels or the other review.

    Args:
        bases: Handwritten base cases.
        messages: (base_id, variant) to message.
        folder: DATA_DIR/eval/handwritten.
        n: Review number, 1 or 2.

    Returns:
        Path of the sheet.

    Raises:
        FileExistsError: When the sheet already exists.
    """
    path = folder / f"revision_{n}.yaml"
    if path.exists():
        raise FileExistsError(f"{path} already exists; it is never overwritten")
    items = []
    for b in bases:
        facts = template_entry(b)["hechos"]
        for v in VARIANTS:
            items.append(
                {
                    "base_id": b.base_id,
                    "variante": v,
                    "ahora_del_caso": _moment(b.now),
                    "hechos": facts,
                    "mensaje": messages.get((b.base_id, v), ""),
                    **{f: None for f in REVIEW_FIELDS},
                }
            )
    header = REVIEW_HEADER.format(
        n=n,
        intents=", ".join(get_args(Intent)),
        actions=", ".join(get_args(Action)),
    )
    body = yaml.safe_dump(
        {"terminada": None, "items": items}, allow_unicode=True, sort_keys=False, width=100
    )
    path.write_text(header + "\n" + body, encoding="utf-8")
    return path


def _review_labels(item: dict[str, Any]) -> dict[str, str]:
    return {f: str(item.get(f)) for f in REVIEW_FIELDS}


def truth_labels(base: BaseCase) -> dict[str, str]:
    """Labels of a base case by construction, in the fields of a review.

    Args:
        base: Base case.

    Returns:
        Field to label.
    """
    n = base.noise
    return {
        "intencion": base.intent,
        "accion": base.expected.action,
        "monto": str(n.amount.mentioned),
        "fecha": str(n.date.mentioned),
        "comercio": str(n.merchant.mentioned),
        "canal": str(n.channel.mentioned),
        "producto": str(n.product.mentioned),
        "tarjeta": str(n.card_possession.mentioned),
    }


def cohen_kappa(a: list[str], b: list[str]) -> float | None:
    """Cohen's kappa of two label sequences.

    Args:
        a: Labels of the first review.
        b: Labels of the second review, same items.

    Returns:
        Kappa, or None when chance agreement is 1 (a single label everywhere).
    """
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    chance = sum(ca[k] * cb[k] for k in ca) / (n * n)
    return None if chance == 1 else (observed - chance) / (1 - chance)


def agreement(folder: Path, bases: list[BaseCase]) -> dict[str, Any]:
    """Agreement between the two reviews, and of each review with the constructed labels.

    Args:
        folder: DATA_DIR/eval/handwritten.
        bases: Handwritten base cases.

    Returns:
        Hours between reviews, items, and per field the share of agreement between reviews,
        Cohen's kappa for intent and action, and the agreement of each review with the labels.

    Raises:
        ValueError: When a review is incomplete or the two are less than 24 hours apart.
    """
    reviews = []
    for n in (1, 2):
        data = yaml.safe_load((folder / f"revision_{n}.yaml").read_text("utf-8"))
        if not data.get("terminada"):
            raise ValueError(f"revision_{n}.yaml has no `terminada` time")
        items = {(i["base_id"], i["variante"]): _review_labels(i) for i in data["items"]}
        blank = [k for k, labels in items.items() if "None" in labels.values()]
        if blank:
            raise ValueError(f"revision_{n}.yaml has {len(blank)} incomplete items")
        reviews.append((datetime.fromisoformat(str(data["terminada"])), items))
    (t1, r1), (t2, r2) = reviews
    hours = (t2 - t1).total_seconds() / 3600
    if hours < 24:
        raise ValueError(f"the reviews are {hours:.1f} hours apart; CA9 asks for a day")
    truth = {(b.base_id, v): truth_labels(b) for b in bases for v in VARIANTS}
    keys = sorted(truth)

    def share(x: list[str], y: list[str]) -> float:
        return round(sum(p == q for p, q in zip(x, y, strict=True)) / len(keys), 4)

    fields = {}
    for f in REVIEW_FIELDS:
        a = [r1[k][f] for k in keys]
        b = [r2[k][f] for k in keys]
        t = [truth[k][f] for k in keys]
        fields[f] = {
            "between_reviews": share(a, b),
            "kappa": None if f not in ("intencion", "accion") else cohen_kappa(a, b),
            "review_1_vs_labels": share(a, t),
            "review_2_vs_labels": share(b, t),
        }
    return {"hours_between_reviews": round(hours, 1), "items": len(keys), "fields": fields}
