"""Where to send a request this channel does not serve (TRZ-23, design 3.1).

The intent `out_of_scope` comes from comprehension. This module only picks which fixed reply
names the right place to go, by keywords, in Spanish and Portuguese. It never decides whether a
request is out of scope, so a missed keyword costs a generic reply, never a wrong action.
"""

import re
import unicodedata
from typing import Literal

Topic = Literal["loan", "investment", "branch", "personal_data", "app", "other"]

# Checked in this order: "actualizar mis datos en la app" is about personal data, not the app.
_TOPICS: tuple[tuple[Topic, re.Pattern[str]], ...] = (
    (
        "loan",
        re.compile(
            r"\b(?:prestamos?|creditos?\s+(?:personal|hipotecario|de\s+libre)|hipotec\w*|"
            r"emprestimos?|financiamentos?|consignado)\b"
        ),
    ),
    (
        "investment",
        re.compile(
            r"\b(?:invert\w*|inversion\w*|investir|investimentos?|cdt|cdb|plazo\s+fijo|"
            r"acciones|fondos?\s+de\s+inversion|tesouro\s+direto|poupanca|renda\s+fixa)\b"
        ),
    ),
    (
        "branch",
        re.compile(r"\b(?:sucursal\w*|oficinas?|agencias?|horarios?)\b"),
    ),
    (
        "personal_data",
        re.compile(
            r"\b(?:mis\s+datos|meus\s+dados|correo|e-?mail|telefono|telefone|celular|"
            r"direccion|domicilio|endereco|nome\s+no\s+cadastro|cadastro)\b"
        ),
    ),
    (
        "app",
        re.compile(
            r"\b(?:app|aplicacion|aplicativo|banca\s+(?:en\s+linea|movil)|"
            r"no\s+puedo\s+entrar|nao\s+consigo\s+entrar|iniciar\s+sesion|login)\b"
        ),
    ),
)


def _fold(text: str) -> str:
    """Lowercases a text and strips its accents, so keywords match however they are written.

    Args:
        text: Any text.

    Returns:
        The folded text.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def topic(text: str) -> Topic:
    """The topic of an out of scope request, which picks where the reply sends the customer.

    Args:
        text: Redacted customer message.

    Returns:
        The first topic whose keywords appear, or "other".
    """
    folded = _fold(text)
    return next((name for name, pattern in _TOPICS if pattern.search(folded)), "other")
