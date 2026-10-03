"""Security signals in the text of a customer message (TRZ-46 follow-up; design 11.4).

Two signals stop a case as a security event before the message reaches the LLM:

- another customer: the message names a customer id other than the session's, or a charge or
  product id (whose ownership the service checks), or asks for another person's movements;
- an injected instruction: the message tells the system to drop its rules, change its role or
  skip the customer's confirmation.

Closed lists, matched without case or accents. A family member who used the card ("mi esposo
usó mi tarjeta") is a legitimate dispute and is not a signal: only a request to see or act on
someone else's data is.
"""

import re
import unicodedata
from dataclasses import dataclass

CUSTOMER_ID = re.compile(r"\bCLI-[A-Z0-9]{12}\b", re.IGNORECASE)
OWNED_ID = re.compile(r"\b(?:TRX-[A-Z0-9]{20}|PRD-[A-Z0-9]{12})\b", re.IGNORECASE)

_DATA = (
    r"(?:movimientos|cobros|consumos|compras|cargos|transacciones|cuentas?|tarjetas?|saldos?|"
    r"movimentacoes|movimentos|cobrancas|transacoes|contas?|cartoes|cartao|extrato)"
)
_SOMEONE_ELSE = (
    r"(?:otra persona|otro cliente|otra cuenta|otros clientes|outra pessoa|outro cliente|"
    r"outra conta|outros clientes|todos los clientes|todos os clientes)"
)
# A request to see or receive the data: "aparecen compras de otra persona" is a fraud report.
_ASK = (
    r"(?:muestrame|mostrame|ensename|pasame|dame|quiero ver|ver|consultar|revisar|"
    r"mostra|me mostra|me mostre|me passa|passa|quero ver|consulta)"
)
OTHER_PERSON = re.compile(
    rf"\b{_ASK}\b(?:\s+\w+){{0,2}}\s+{_DATA}\b(?:\s+\w+){{0,3}}\s+"
    rf"(?:de|del|da|do|dos|das)\s+{_SOMEONE_ELSE}\b"
)

INJECTION = tuple(
    re.compile(p)
    for p in (
        # Drop the rules or instructions.
        r"\b(?:ignora|ignore|ignorar|olvida|olvide|olvidate|esquece|esqueca|desconsidera)\s+"
        r"(?:todas\s+)?(?:tus|sus|las|suas|tuas|as)\s+(?:\w+\s+)?"
        r"(?:reglas|instrucciones|indicaciones|regras|instrucoes)\b",
        # Take another role.
        r"\b(?:ahora eres|ahora sos|agora voce e|agora es|actua como|atua como|aja como)\s+"
        r"(?:el |un |o |um )?(?:administrador|admin|sistema|desarrollador|desenvolvedor|root)\b",
        # Skip the customer's confirmation.
        r"\b(?:sin|sem)\s+(?:pedirme|preguntarme|me pedir|me perguntar)\s+"
        r"(?:confirmacion|confirmacao)\b",
        # Reach the data of the whole bank.
        r"\b(?:todas las cuentas|todas as contas)\s+(?:del|do)\s+banco\b",
        r"\b(?:system prompt|prompt del sistema|prompt do sistema|modo desarrollador|"
        r"modo desenvolvedor|developer mode|jailbreak)\b",
    )
)


@dataclass(frozen=True)
class TextSignals:
    """What the message says that points outside the session customer.

    Attributes:
        other_customer: A customer id other than the session's, or a request for another
            person's data.
        owned_ids: Charge and product ids named in the message; the service checks they are the
            session customer's.
        injection: An instruction aimed at the system.
    """

    other_customer: bool
    owned_ids: tuple[str, ...]
    injection: bool


def _plain(text: str) -> str:
    """Lowercase text without accents."""
    folded = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in folded if not unicodedata.combining(c))


def mask_ids(text: str) -> str:
    """The text with every customer, charge and product id replaced by `[ID]`.

    A security stop keeps what the customer wrote without another customer's ids.
    """
    return OWNED_ID.sub("[ID]", CUSTOMER_ID.sub("[ID]", text))


def read_signals(text: str, customer_id: str) -> TextSignals:
    """Reads the security signals of one raw message.

    Args:
        text: Raw customer message.
        customer_id: Customer of the session.

    Returns:
        The signals found.
    """
    plain = _plain(text)
    foreign = any(m.upper() != customer_id.upper() for m in CUSTOMER_ID.findall(text))
    return TextSignals(
        other_customer=foreign or OTHER_PERSON.search(plain) is not None,
        owned_ids=tuple(m.upper() for m in OWNED_ID.findall(text)),
        injection=any(p.search(plain) for p in INJECTION),
    )
