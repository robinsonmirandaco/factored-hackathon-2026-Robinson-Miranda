"""PII redaction: what must be redacted, what must survive for charge identification, and what
reaches the LLM. Every value here is made up; the check digits are computed, not copied."""

import json
from datetime import datetime

import httpx
import pytest

from app.adapters.llm import LLMClient
from app.core.config import Settings
from app.domain.pii import ACCOUNT, CARD, DOCUMENT, EMAIL, NAME, PHONE, redact
from app.schemas.comprehension import ComprehensionContext

# (text, value that must disappear, placeholder that must replace it)
POSITIVES: list[tuple[str, str, str]] = [
    # CPF
    ("meu CPF é 529.982.247-25", "529.982.247-25", DOCUMENT),
    ("cpf 529 982 247 25 por favor", "529 982 247 25", DOCUMENT),
    ("o documento 52998224725 é meu", "52998224725", DOCUMENT),
    # CNPJ
    ("CNPJ 11.222.333/0001-81", "11.222.333/0001-81", DOCUMENT),
    ("a empresa 11222333000181 cobrou", "11222333000181", DOCUMENT),
    ("cnpj: 11 222 333 0001-81", "11 222 333 0001-81", DOCUMENT),
    # Colombian cédula: needs a keyword
    ("mi cédula es 1.023.456.789", "1.023.456.789", DOCUMENT),
    ("C.C. 79 123 456 de Bogotá", "79 123 456", DOCUMENT),
    ("cedula de ciudadania: 1023456789", "1023456789", DOCUMENT),
    # CURP
    ("mi CURP es GODE561231HDFRRN09", "GODE561231HDFRRN09", DOCUMENT),
    ("curp gode561231hdfrrn09.", "gode561231hdfrrn09", DOCUMENT),
    ("CURP: MAGA800101MDFRRL02, gracias", "MAGA800101MDFRRL02", DOCUMENT),
    # RUT
    ("mi RUT es 12.345.678-5", "12.345.678-5", DOCUMENT),
    ("rut 12345678-5", "12345678-5", DOCUMENT),
    ("RUN 7.654.321-6 del titular", "7.654.321-6", DOCUMENT),
    # Argentine DNI: needs a keyword
    ("DNI 30.123.456", "30.123.456", DOCUMENT),
    ("mi dni nº 30123456", "30123456", DOCUMENT),
    ("documento de identidad 30-123-456", "30-123-456", DOCUMENT),
    # Card, Luhn valid
    ("mi tarjeta 4111 1111 1111 1111 fue clonada", "4111 1111 1111 1111", CARD),
    ("cartão 5500-0000-0000-0004", "5500-0000-0000-0004", CARD),
    ("la amex 3782 822463 10005", "3782 822463 10005", CARD),
    ("tarjeta 4111111111111111", "4111111111111111", CARD),
    ("la visa 4111.1111.1111.1111", "4111.1111.1111.1111", CARD),
    # Account
    ("cuenta de ahorros 123-456789-01", "123-456789-01", ACCOUNT),
    ("minha conta 12345-6 no banco", "12345-6", ACCOUNT),
    ("CLABE 032180000118359719", "032180000118359719", ACCOUNT),
    ("CBU 2850590940090418135201", "2850590940090418135201", ACCOUNT),
    # Email
    ("escríbanme a juan.perez+banco@mail.com", "juan.perez+banco@mail.com", EMAIL),
    ("correo: ana_b@empresa.com.co.", "ana_b@empresa.com.co", EMAIL),
    ("EMAIL JP@MAIL.COM.BR", "JP@MAIL.COM.BR", EMAIL),
    # Phone, Mexico
    ("llámame al +52 55 1234 5678", "+52 55 1234 5678", PHONE),
    ("mi número (55) 1234-5678", "(55) 1234-5678", PHONE),
    ("celular 5512345678", "5512345678", PHONE),
    # Phone, Colombia
    ("+57 300 123 4567", "+57 300 123 4567", PHONE),
    ("cel. 300.123.4567", "300.123.4567", PHONE),
    ("mi celu es 3001234567", "3001234567", PHONE),
    # Phone, Argentina
    ("+54 9 11 1234-5678", "+54 9 11 1234-5678", PHONE),
    ("tel 11 1234-5678", "11 1234-5678", PHONE),
    ("whatsapp: 0054 11 4444 5555", "0054 11 4444 5555", PHONE),
    # Phone, Brazil
    ("meu celular é +55 (11) 91234-5678", "+55 (11) 91234-5678", PHONE),
    ("liga no (11) 91234-5678", "(11) 91234-5678", PHONE),
    ("telefone 11912345678", "11912345678", PHONE),
]

# Clues the charge identification reads; none of them may change.
NEGATIVES: list[str] = [
    "me cobraron 1,800",
    "fueron 1.800 pesos",
    "un cargo de 1800",
    "R$ 500 no mercado",
    "como 390 lucas",
    "me cobraron 1.800.000 en mi cuenta",
    "$ 1,250,000.00 en Walmart",
    "R$ 1.234,56 no dia 12",
    "el 12/06",
    "el 12 de junio",
    "el 2026-06-12",
    "el 12/06/2026 pagué 45.000 en Exito",
    "la tarjeta terminada en 4821",
    "cartão final 4821",
    "la que termina en 4821 por 1.800",
    "mi folio es DSP-2026-09417",
    "el caso DSP-2026-09417 del 12 de junio por 390 lucas",
    "DNI terminado en 4821",
]

# 16 digits that fail Luhn: amounts, folios or typos, never a card.
NOT_LUHN: list[str] = [
    "1234 5678 9012 3456",
    "1234-5678-9012-3456",
    "1234.5678.9012.3456",
    "1234567890123456",
    "4111 1111 1111 1112",
]

SECRETS = [secret for _, secret, _ in POSITIVES]


@pytest.mark.parametrize(("text", "secret", "tag"), POSITIVES)
def test_pii_is_replaced_by_its_placeholder(text: str, secret: str, tag: str) -> None:
    out, counts = redact(text)
    assert secret not in out
    assert tag in out
    assert counts == {tag: 1}


@pytest.mark.parametrize("text", NEGATIVES)
def test_identification_clues_are_left_intact(text: str) -> None:
    assert redact(text) == (text, {})


@pytest.mark.parametrize("text", NOT_LUHN)
def test_sixteen_digits_failing_luhn_are_not_a_card(text: str) -> None:
    assert redact(f"cargo {text}") == (f"cargo {text}", {})


def test_bare_document_without_keyword_is_left_as_possible_amount() -> None:
    # "12.345.678" is also how an amount is written, so without "DNI" or "cédula" it stays.
    assert redact("fueron 12.345.678") == ("fueron 12.345.678", {})
    assert redact("DNI 12.345.678")[0] == "DNI [DOCUMENT]"


def test_full_card_keeps_no_digits_and_clues_survive_in_one_message() -> None:
    text = (
        "No reconozco el cargo de 1.800 del 12 de junio en la tarjeta 4111 1111 1111 1111, "
        "la terminada en 4821, folio DSP-2026-09417. Mi correo es ana@mail.com y mi cel "
        "+57 300 123 4567."
    )
    out, counts = redact(text)
    assert out == (
        "No reconozco el cargo de 1.800 del 12 de junio en la tarjeta [CARD], "
        "la terminada en 4821, folio DSP-2026-09417. Mi correo es [EMAIL] y mi cel "
        "[PHONE]."
    )
    assert counts == {CARD: 1, EMAIL: 1, PHONE: 1}


# (text, first name of the customer in session, expected text)
NAMES: list[tuple[str, str, str]] = [
    ("Hola, soy Valentina", "Valentina", "Hola, soy [NAME]"),
    ("habla VALENTINA de nuevo", "Valentina", "habla [NAME] de nuevo"),
    ("soy jose, José Pérez", "José", "soy [NAME], [NAME] Pérez"),
    ("aqui Joao, o titular", "João", "aqui [NAME], o titular"),
    ("MARÍA y maria", "Maria", "[NAME] y [NAME]"),
]


@pytest.mark.parametrize(("text", "name", "expected"), NAMES)
def test_customer_first_name_is_replaced_ignoring_case_and_accents(
    text: str, name: str, expected: str
) -> None:
    out, counts = redact(text, name=name)
    assert out == expected
    assert counts == {NAME: expected.count(NAME)}


def test_name_is_replaced_only_as_a_whole_word() -> None:
    assert redact("Valentinas y Anastasia", name="Ana") == ("Valentinas y Anastasia", {})
    assert redact("compré en Tienda Ana", name="Ana")[0] == "compré en Tienda [NAME]"


def test_name_inside_an_email_goes_with_the_email() -> None:
    assert redact("valentina.r@mail.com, Valentina", name="Valentina") == (
        "[EMAIL], [NAME]",
        {EMAIL: 1, NAME: 1},
    )


def test_no_name_leaves_text_as_the_patterns_do() -> None:
    assert redact("soy Valentina", name=None) == ("soy Valentina", {})
    assert redact("soy Valentina", name="  ") == ("soy Valentina", {})


def test_every_type_has_at_least_three_cases() -> None:
    per_tag: dict[str, int] = {}
    for _, _, tag in POSITIVES:
        per_tag[tag] = per_tag.get(tag, 0) + 1
    assert set(per_tag) == {CARD, EMAIL, PHONE, DOCUMENT, ACCOUNT}
    assert min(per_tag.values()) >= 3


def test_prompts_sent_to_a_simulated_llm_carry_no_pii() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(json.dumps(body, ensure_ascii=False))
        system = body["messages"][0]["content"]
        if system.startswith(llm.comprehension_prompt.system[:40]):
            content = json.dumps(
                {
                    "intent": "unrecognized_charge",
                    "amount": None,
                    "date": None,
                    "merchant_hint": None,
                    "channel_hint": None,
                    "card_in_possession": None,
                    "language": "es-MX",
                }
            )
        else:
            content = "Bloqueamos tu tarjeta y abrimos la disputa."
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost:1/unused",
        llm_enabled=True,
        llm_provider="local",
        llm_base_url="http://llm.test/v1",
        llm_model_primary="test-model",
        anthropic_api_key="",
    )
    http = httpx.Client(base_url="http://llm.test/v1", transport=httpx.MockTransport(handler))
    llm = LLMClient(settings, http_client=http)

    message = " ".join(text for text, _, _ in POSITIVES)
    redacted, _ = redact(message)
    context = ComprehensionContext(
        now=datetime(2026, 6, 17, 10, 0), country_code="MX", local_currency="MXN"
    )
    _, stats = llm.comprehend(redacted, context)
    llm.compose(redacted, {"outcome": "escalated"}, "es")

    assert not stats.fallback
    assert len(sent) == 2  # comprehend, compose
    for body in sent:
        for secret in SECRETS:
            assert secret not in body
