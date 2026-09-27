"""Rules baseline of the comprehension step (TRZ-13, design 6.1).

Keywords per intent and language, regular expressions for amounts and relative dates, and fixed
phrases for card possession and channel. It returns the same `Comprehension` as the LLM, so both
are measured with the same harness, and it is what the system uses when the LLM fails.

Patterns run on a folded copy of the message (lowercase, accents removed) that keeps the length
of the original, so every match is cut from the original message and its evidence is literal.
"""

import re
import unicodedata

from app.domain.clock import SimulatedClock
from app.schemas.comprehension import (
    AmountClue,
    ChannelClue,
    Comprehension,
    ComprehensionContext,
    DateClue,
    Intent,
    LanguageVariant,
    PossessionClue,
    TextClue,
)

RULES_VERSION = "rules-1"


def _fold(text: str) -> str:
    # One output character per input character, so spans map back onto the original text.
    out = []
    for ch in text:
        base = unicodedata.normalize("NFD", ch)[0].lower()
        out.append(base if len(base) == 1 else ch)
    return "".join(out)


def _any(*phrases: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(phrases) + r")\b")


# Intents, checked in this order: a status question may mention the charge it is about, and a
# duplicate or a wrong amount is also a charge the customer "does not recognize" as billed. A
# customer who writes "mi reclamo" refers to one already filed; a new dispute is "un reclamo".
_INTENT_PATTERNS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (
        "claim_status",
        _any(
            r"(?:estado|estatus|status|seguimiento|avance|como va|en que va|que paso con"
            r"|novedad(?:es)?|respuesta|andamento|situacao|acompanhar|novidades?|como esta)"
            r"\b.{0,40}\b(?:reclamo|reclamacion|queja|disputa|solicitud|aclaracion|radicado"
            r"|ticket|contracargo|reclamacao|contestacao|protocolo|chamado|solicitacao)",
            r"(?:mi|minha|meu)\s+(?:reclamo|reclamacion|queja|disputa|aclaracion|reclamacao"
            r"|contestacao|protocolo|chamado)",
            r"(?:el|la|a|o)\s+(?:reclamo|reclamacion|queja|disputa|aclaracion|reclamacao"
            r"|contestacao)\s+(?:que\s+)?(?:abri|puse|hice|radique|presente|levante|fiz"
            r"|registrei)",
            r"numero\s+(?:de|del|do)\s+(?:reclamo|caso|radicado|folio|protocolo)",
        ),
    ),
    (
        "billing_error_duplicate",
        _any(
            r"dos veces",
            r"duas vezes",
            r"(?:cobro|cargo|cobranca|debito)s?\s+(?:doble|duplicad[oa]s?|dupla|repetid[oa]s?)",
            r"(?:doble|dupla)\s+(?:cobro|cargo|cobranca)",
            r"duplicad[oa]s?",
            r"repetid[oa]s?",
            r"em dobro",
            r"(?:dos|dois|duas)\s+(?:cargos|cobros|cobrancas|debitos|compras)\s+(?:iguales"
            r"|identic[oa]s|iguais|del mismo|do mesmo|da mesma)",
            r"(?:cobraron|cobraram)\s+(?:otra vez|de nuevo|de novo)",
        ),
    ),
    (
        "billing_error_amount",
        _any(
            r"(?:cobraron|cobro|cobrado|cargaron|cobraram|cobrou|debitaron|debitaram)"
            r"\s+(?:de mas|a mais|mas de lo|mais do que|un monto|um valor|otro monto"
            r"|outro valor)",
            r"(?:monto|valor|precio|importe|preco)\s+(?:diferente|distinto|equivocado"
            r"|incorrecto|errado|erroneo|incorreto|mayor|maior|a mais)",
            r"no\s+(?:es|era|coincide con|corresponde a)\s+(?:el|lo)\s+(?:monto|precio|valor"
            r"|que)",
            r"nao\s+(?:bate|confere|corresponde)",
            r"(?:deberia|debia|tenia que|deveria|devia)\s+(?:ser|haber sido|costar|salir|ter sido)",
            r"(?:precio|monto|valor|preco)\s+(?:acordado|pactado|combinado|anunciado|de la"
            r" etiqueta)",
            r"cobro de mas",
            r"cobrado a mais",
            r"(?:valor|monto|precio|preco|importe)\s+(?:correcto|correto|certo|real|original)",
            r"(?:o|lo)\s+certo\s+era",
            r"(?:la compra|a compra|el cargo|o valor|el valor|el precio|o preco)\s+era(?:\s+de)?",
            r"(?:fui|fue|foi)\s+cobrad[oa]\s+(?:errado|mal|a mais|de mas)",
            r"(?:cobraron|cobraram|cobrou|cobro|cobrado)\s+(?:mal|errado)",
            r"cobro mal hecho",
            r"(?:tenia|tinha)\s+que\s+(?:ser|pagar|costar|sair)",
            r"(?:habia|havia)\s+pagado",
        ),
    ),
    (
        "unrecognized_charge",
        _any(
            r"no\s+(?:lo\s+|la\s+|los\s+|las\s+)?reconozco",
            r"desconozco",
            r"no fui yo",
            r"yo no (?:fui|hice|compre|autorice|realice)",
            r"no\s+(?:la|lo)\s+hice",
            r"no\s+(?:hice|realice|autorice|compre|reconoce)",
            r"nunca\s+(?:compre|hice|autorice|realice|use|he comprado)",
            r"sin\s+(?:mi\s+)?(?:autorizacion|permiso|consentimiento)",
            r"fraude",
            r"fraudulent[oa]s?",
            r"clonad[oa]s?",
            r"me clonaron",
            r"alguien\s+(?:uso|hizo|compro|mas)",
            r"no\s+(?:es|son)\s+mi(?:o|a|os|as)",
            r"(?:cargo|cobro|compra|movimiento)\s+(?:extran[oa]|rar[oa]|desconocid[oa]|que no)",
            r"nao\s+(?:fui eu|reconheco|fiz|realizei|autorizei|comprei)",
            r"eu nao\s+(?:fiz|comprei|autorizei|reconheco)",
            r"desconheco",
            r"nunca\s+(?:comprei|fiz|usei|autorizei)",
            r"sem\s+(?:minha\s+)?(?:autorizacao|permissao)",
            r"alguem\s+(?:usou|fez|comprou)",
            r"nao\s+(?:e|sao)\s+(?:meu|minha|meus|minhas)",
            r"(?:compra|cobranca|lancamento|debito)\s+(?:estranh[oa]|desconhecid[oa]|que nao)",
        ),
    ),
)

# Requests the service does not handle (design 3.1). Checked after the dispute keywords, so a
# dispute that mentions the app or a branch is still a dispute.
_OUT_OF_SCOPE = _any(
    r"prestamos?",
    r"emprestimos?",
    r"financiamento",
    r"credito\s+(?:personal|hipotecario|de vivienda|pessoal|imobiliario)",
    r"hipoteca",
    r"sucursal(?:es)?",
    r"oficina",
    r"agencia",
    r"horarios?",
    r"contrasena",
    r"senha",
    r"(?:clave|pin)\s+(?:de|del|da|do)",
    r"(?:actualizar|cambiar|modificar)\s+(?:mis\s+|mi\s+)?(?:datos|direccion|correo|telefono"
    r"|numero|domicilio|email)",
    r"(?:atualizar|mudar|alterar)\s+(?:meus\s+|meu\s+|minha\s+)?(?:dados|endereco|email"
    r"|telefone|numero)",
    r"datos personales",
    r"dados pessoais",
    r"(?:la\s+)?(?:app|aplicacion)\s+(?:no|se)\s+(?:funciona|abre|carga|cierra|traba|cae)",
    r"(?:o\s+)?(?:app|aplicativo)\s+(?:nao|trava|fecha)",
    r"no puedo\s+(?:entrar|ingresar|acceder)",
    r"nao consigo\s+(?:entrar|acessar)",
    r"abrir\s+una?\s+cuenta",
    r"abrir\s+uma\s+conta",
    r"(?:un|el|mi|um|o|meu)\s+seguro\s+(?:de|del|do)",
    r"inversion(?:es)?",
    r"investimentos?",
    r"(?:limite|cupo)\s+de\s+credito",
    r"aumento de cupo",
)

_CHARGE_WORDS = _any(
    r"cargos?",
    r"cobros?",
    r"compras?",
    r"transacci(?:on|ones)",
    r"movimientos?",
    r"debitos?",
    r"retiros?",
    r"consumos?",
    r"cobranca?s?",
    r"lancamentos?",
    r"saques?",
    r"transac(?:ao|oes)",
    r"cobraron",
    r"cobraram",
    r"cobrad[oa]",
)

# Card possession. "Has it" is checked first so a negated loss ("no la perdí") is not a loss.
_NOT = r"(?<!\bno\s)(?<!\bnao\s)"
_HAS_CARD = _any(
    _NOT + r"(?:tengo|conservo|sigo teniendo|aun tengo|todavia tengo)\s+(?:mi\s+|la\s+|el\s+)?"
    r"tarjeta",
    r"(?:la\s+)?tarjeta\s+(?:la\s+tengo|esta|sigue)\s+(?:conmigo|en mi poder|aqui|aca)",
    _NOT + r"(?:la\s+)?tengo\s+(?:conmigo|aqui|aca|en mi poder)",
    r"no\s+(?:la\s+)?(?:he\s+)?perdi(?:do)?",
    r"nunca\s+(?:la\s+)?(?:he\s+)?perdi(?:do)?",
    r"no me (?:la\s+)?(?:han\s+)?robad[oa]",
    r"no me (?:la\s+)?robaron",
    _NOT + r"tenho\s+(?:o\s+|meu\s+)?cartao",
    r"(?:o\s+)?cartao\s+(?:esta|ta|continua|segue|fica)\s+comigo",
    r"(?:esta|ta|continua|segue)\s+(?:conmigo|comigo)",
    r"(?:nao|nunca)\s+(?:o\s+)?perdi",
    r"(?:estou|fiquei)\s+com\s+(?:o\s+|meu\s+)?cartao",
)
_LOST_CARD = _any(
    r"me\s+(?:la\s+|lo\s+)?robaron",
    r"robaron\s+(?:mi|la|el)",
    r"me\s+(?:la\s+)?(?:han\s+)?robado",
    r"robo\s+de\s+(?:mi|la)\s+(?:tarjeta|cartera|billetera)",
    r"perdi\s+(?:mi|la)\s+(?:tarjeta|cartera|billetera)",
    r"se me perdio",
    r"(?:la|lo)\s+perdi",
    r"extravie",
    r"extraviad[oa]",
    r"no\s+(?:tengo|encuentro)\s+(?:mi|la|el)\s+tarjeta",
    r"me\s+(?:la\s+)?(?:quitaron|hurtaron|asaltaron)",
    r"roubaram",
    r"(?:foi|fui)\s+roubad[oa]",
    r"furtaram",
    r"(?:foi|fui)\s+furtad[oa]",
    r"perdi\s+(?:o|meu)\s+cartao",
    r"extraviei",
    r"nao\s+(?:tenho|encontro|acho)\s+(?:o|meu)\s+cartao",
    r"(?:o\s+|meu\s+)?cartao\s+sumiu",
    r"sumiu\s+(?:o|meu)\s+cartao",
    r"levaram\s+(?:o|meu)\s+cartao",
    r"(?:fui\s+)?assaltad[oa]",
)

# Channel of the charge; the values are those of the transactions.
_CHANNELS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ATM",
        _any(
            r"cajeros?(?:\s+automaticos?)?",
            r"atm",
            r"caixa(?:\s+eletronico|\s+24\s*h(?:oras)?)",
            r"retiro\s+(?:de\s+)?(?:efectivo|dinero)",
            r"saque",
            r"sacaram",
            r"retiraron",
        ),
    ),
    (
        "App",
        _any(
            r"(?:la\s+|una\s+|o\s+|um\s+)?app(?:\s+(?:del|do)\s+banco)?",
            r"aplicacion",
            r"aplicativo",
        ),
    ),
    (
        "Web",
        _any(
            r"(?:por|en|pela|na)\s+internet",
            r"en\s+linea",
            r"on-?line",
            r"(?:pagina|sitio|site)\s+web",
            r"(?:pelo|no)\s+site",
            r"(?:compra|tienda|loja)\s+virtual",
            r"e-?commerce",
            r"web",
        ),
    ),
    (
        "POS",
        _any(
            r"presencial(?:mente)?",
            r"en persona",
            r"pessoalmente",
            r"datafono",
            r"posnet",
            r"(?:terminal|punto)\s+de\s+venta",
            r"pos",
            r"maquinit[ao]",
            r"maquininha",
            r"(?:pase|pasaron|passei|passaram)\s+(?:la|mi|o|meu)\s+(?:tarjeta|cartao)",
            r"(?:tienda|local|loja)\s+fisic[oa]",
            r"en\s+(?:la|una)\s+(?:tienda|caja)(?!\s+(?:en linea|online|virtual|web))",
            r"(?:numa|na|em uma)\s+loja(?!\s+(?:online|virtual))",
        ),
    ),
)

# Amounts: "1,800", "1.800", "1800", "1.800,50", "1.8 mil", "como 390 lucas", "R$ 500".
_APPROX = (
    r"como|unos|unas|uns|umas|alrededor de|cerca de|aproximadamente|aprox\.?|mas o menos"
    r"|mais ou menos|mas de|mais de|por volta de|algo asi como|casi|quase|arriba de|acima de"
    r"|tipo"
)
_AMOUNT = re.compile(
    rf"(?:(?P<approx>\b(?:{_APPROX}))\s+)?"
    r"(?:(?P<pre>r\$|us\$|u\$s|\$|\b(?:usd|mxn|cop|ars|brl))\s?)?"
    r"(?<![\w.,])(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?)(?!\d)"
    r"(?:\s?(?P<mult>millones|millon|milhoes|milhao|mil|k|luquitas|lucas|luca|palos|palo)\b)?"
    r"(?:\s?(?:de\s+)?(?P<suf>pesos\s+(?:argentinos|colombianos|mexicanos)|pesitos|pesos|peso"
    r"|reais|real|dolares|dolar|dls|varos|usd|mxn|cop|ars|brl)\b)?"
)
_MULTIPLIERS = {
    "mil": 1e3,
    "k": 1e3,
    "luca": 1e3,
    "lucas": 1e3,
    "luquitas": 1e3,
    "palo": 1e6,
    "palos": 1e6,
    "millon": 1e6,
    "millones": 1e6,
    "milhao": 1e6,
    "milhoes": 1e6,
}
_LOCAL_WORDS = {
    "$",
    "pesos",
    "peso",
    "pesitos",
    "varos",
    "luca",
    "lucas",
    "luquitas",
    "palo",
    "palos",
}
_CURRENCY_WORDS = {
    "pesos argentinos": "ARS",
    "pesos colombianos": "COP",
    "pesos mexicanos": "MXN",
    "r$": "BRL",
    "reais": "BRL",
    "real": "BRL",
    "us$": "USD",
    "u$s": "USD",
    "dolares": "USD",
    "dolar": "USD",
    "dls": "USD",
}
_MONTHS = (
    "enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre"
    "|diciembre|janeiro|fevereiro|marco|maio|junho|julho|setembro|outubro|novembro|dezembro"
)
# A bare number followed by a time unit, a month or a count noun is not an amount.
_NOT_AMOUNT_AFTER = re.compile(
    r"\s*(?:%|/|:|dias?\b|semanas?\b|mes(?:es)?\b|anos?\b|horas?\b|hs\b|h\b|veces\b|vez(?:es)?\b"
    r"|cargos?\b|cobros?\b|compras?\b|cobrancas?\b|debitos?\b|digitos\b|de\s+(?:" + _MONTHS + r"))"
)
_NOT_AMOUNT_BEFORE = re.compile(
    r"(?:\bhace|\bha|\bfaz|\bel|\bdia|\bdel|\bterminad[ao] en|\btermina en|\bfinal|\bterminacao)"
    r"\s*$"
)

# Relative dates, tried in this order; the first pattern that matches wins, so "la semana
# pasada" is not read as "esta semana" and a discovery "hoy" loses to the date of the charge.
_COUNT = (
    r"(?P<n>\d{1,2}|un par de|un|una|uno|um|uma|dos|dois|duas|tres|cuatro|quatro|cinco|seis"
    r"|siete|sete|ocho|oito|nueve|nove|diez|dez|quince|quinze)"
)
_NUMBER_WORDS = {
    "un": 1,
    "una": 1,
    "uno": 1,
    "um": 1,
    "uma": 1,
    "un par de": 2,
    "dos": 2,
    "dois": 2,
    "duas": 2,
    "tres": 3,
    "cuatro": 4,
    "quatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "sete": 7,
    "ocho": 8,
    "oito": 8,
    "nueve": 9,
    "nove": 9,
    "diez": 10,
    "dez": 10,
    "quince": 15,
    "quinze": 15,
}
_UNIT = r"(?P<unit>dias?|semanas?|mes(?:es)?)"
_HEDGE = (
    r"(?:(?:unos|unas|uns|umas|como|cerca de|mas o menos|mais ou menos|aproximadamente"
    r"|alrededor de|cosa de|por volta de)\s+)?"
)
_ES_DAYS = {
    "lunes": "monday",
    "martes": "tuesday",
    "miercoles": "wednesday",
    "jueves": "thursday",
    "viernes": "friday",
    "sabado": "saturday",
    "domingo": "sunday",
}
_PT_DAYS = {
    "segunda": "monday",
    "terca": "tuesday",
    "quarta": "wednesday",
    "quinta": "thursday",
    "sexta": "friday",
}
_DATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "day_before_yesterday",
        _any(r"anteayer", r"antier", r"antes de ayer", r"anteontem", r"antes de ontem"),
    ),
    ("ago", _any(rf"hace\s+{_HEDGE}{_COUNT}\s+{_UNIT}(?:\s+atras)?")),
    ("ago", _any(rf"(?:ha|faz)\s+{_HEDGE}{_COUNT}\s+{_UNIT}(?:\s+atras)?")),
    ("ago", _any(rf"{_COUNT}\s+{_UNIT}\s+atras")),
    (
        "few_days",
        _any(
            r"hace\s+(?:unos|algunos|pocos)\s+dias",
            r"(?:ha|faz)\s+(?:uns|alguns|poucos)\s+dias",
            r"(?:unos|algunos|uns|alguns|poucos)\s+dias\s+atras",
        ),
    ),
    ("last_month", _any(r"(?:el\s+|no\s+)?mes\s+(?:pasado|anterior|passado)")),
    (
        "early_this_month",
        _any(
            r"(?:a\s+)?(?:principios|inicios|comienzos|principio|inicio|comienzo)\s+de(?:l|\s+este)?"
            r"\s+mes",
            r"(?:no\s+)?(?:comeco|inicio|principio)\s+d(?:o|este|esse)\s+mes",
        ),
    ),
    ("last_week", _any(r"(?:la\s+|na\s+)?semana\s+(?:pasada|anterior|passada)")),
    (
        "weekend",
        _any(r"(?:el\s+|este\s+|no\s+|neste\s+|nesse\s+|ultimo\s+)?(?:fin|fim)\s+de\s+semana"),
    ),
    ("this_week", _any(r"(?:de\s+)?(?:esta|essa|nesta|nessa|desta|dessa)\s+semana")),
    ("recently", _any(r"hace poco", r"recientemente", r"(?:ha|faz) pouco", r"recentemente")),
    (
        "weekday",
        _any(
            r"(?P<art>(?:el|este|del|na|no|neste|nesse|desde)\s+)?(?P<pre>(?:pas{1,2}ad|ultim)[oa]\s+)?"
            r"(?P<es>lunes|martes|miercoles|jueves|viernes|sabado|domingo)"
            r"(?P<post>\s+pas{1,2}ad[oa])?",
        ),
    ),
    (
        "weekday",
        _any(
            r"(?P<art>(?:na|no|nesta|neste|nessa|nesse|desde)\s+)?(?P<pre>(?:ultim|passad)[ao]\s+)?"
            r"(?P<pt>segunda|terca|quarta|quinta|sexta)(?P<feira>-feira|\s+feira)?"
            r"(?P<post>\s+passad[ao])?",
        ),
    ),
    ("yesterday", _any(r"ayer", r"ontem")),
    ("today", _any(r"hoy", r"hoje")),
)

_PT_MARKERS = _any(
    r"nao",
    r"voce",
    r"cartao",
    r"cobrancas?",
    r"meu",
    r"minha",
    r"ontem",
    r"reconheco",
    r"fiz",
    r"um",
    r"uma",
    r"com",
    r"isso",
    r"essa",
    r"esse",
    r"tambem",
    r"entao",
    r"pelo",
    r"pela",
    r"fatura",
    r"reais",
    r"cobraram",
    r"vezes",
    r"tenho",
    r"preciso",
    r"gostaria",
    r"estou",
    r"obrigad[oa]",
    r"eu",
)
_ES_MARKERS = _any(
    r"no",
    r"mi",
    r"tarjeta",
    r"cobros?",
    r"cobraron",
    r"ayer",
    r"reconozco",
    r"hice",
    r"una",
    r"con",
    r"eso",
    r"tambien",
    r"entonces",
    r"tengo",
    r"necesito",
    r"quiero",
    r"gracias",
    r"yo",
    r"pero",
    r"cuenta",
    r"el",
    r"los",
    r"las",
)
# Voseo is the one variant signal a rule can trust; other regional words are too sparse.
_VOSEO = re.compile(r"\b(?:vos|sos|ten[eé]s|pod[eé]s|quer[eé]s|decime|necesit[aá]s)\b", re.I)
_VARIANT_BY_COUNTRY: dict[str, LanguageVariant] = {"MX": "es-MX", "CO": "es-CO", "AR": "es-AR"}

# Merchant: capitalized words after a preposition or article ("en MercaYa", "el Uber").
_CAPITAL_WORD = r"[A-ZÁÉÍÓÚÑÇ0-9][\w'&.-]*"
_MERCHANT = re.compile(
    r"(?i:\b(?:en|de|del|al|el|la|con|na|no|em|da|do|com|pelo|pela|a)\s+)"
    rf"(?P<name>{_CAPITAL_WORD}(?:\s+(?:(?:de|del|la|y|e|&)\s+)?{_CAPITAL_WORD})*)"
)
_NOT_MERCHANT = {
    *(_ES_DAYS),
    *(_PT_DAYS),
    *(m for m in _MONTHS.split("|")),
    "usd",
    "mxn",
    "cop",
    "ars",
    "brl",
    "us",
    "r",
    "visa",
    "mastercard",
    "amex",
    "pix",
    "web",
    "app",
    "atm",
    "banco",
    "mexico",
    "colombia",
    "argentina",
    "brasil",
}


def comprehend_rules(message: str, context: ComprehensionContext) -> Comprehension:
    """Reads intent and clues from a redacted message with fixed rules.

    Args:
        message: Customer message, PII already replaced by placeholders.
        context: Simulated "now" and the customer's country and currency.

    Returns:
        The comprehension; every clue carries a literal fragment of the message.
    """
    folded = _fold(message)
    possession = _card_possession(message, folded)
    amount = _amount(message, folded, context)
    merchant = _merchant(message)
    return Comprehension(
        intent=_intent(folded, has_charge=amount is not None or merchant is not None),
        amount=amount,
        date=_date(message, folded, context),
        merchant_hint=merchant,
        channel_hint=_channel(message, folded),
        card_in_possession=possession,
        language=_language(message, folded, context),
    )


def _intent(folded: str, has_charge: bool) -> Intent:
    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(folded):
            return intent
    if _OUT_OF_SCOPE.search(folded):
        return "out_of_scope"
    # A lost or stolen card with no charge in sight is a blocking request, not a dispute; the
    # policy redirects it to the bank's blocking channel (TRZ-17 CA9).
    if has_charge or _CHARGE_WORDS.search(folded):
        return "unrecognized_charge"
    return "out_of_scope"


def _cut(message: str, match: re.Match[str], group: int | str = 0) -> str:
    return message[match.start(group) : match.end(group)]


def _card_possession(message: str, folded: str) -> PossessionClue | None:
    for value, pattern in ((True, _HAS_CARD), (False, _LOST_CARD)):
        match = pattern.search(folded)
        if match:
            return PossessionClue(value=value, evidence=_cut(message, match))
    return None


def _channel(message: str, folded: str) -> ChannelClue | None:
    found = [(m.start(), name, m) for name, p in _CHANNELS if (m := p.search(folded))]
    if not found:
        return None
    _, name, match = min(found, key=lambda f: f[0])
    return ChannelClue(value=name, evidence=_cut(message, match))  # type: ignore[arg-type]


def _parse_number(text: str) -> float:
    separators = [i for i, ch in enumerate(text) if ch in ".,"]
    if not separators:
        return float(text)
    last = separators[-1]
    decimals = len(text) - last - 1
    mixed = len({text[i] for i in separators}) > 1
    # "1,800" and "1.800" group thousands; "1,5", "1.8" and the last mark of "1.800,50" are
    # decimal points.
    if mixed or (len(separators) == 1 and decimals != 3):
        return float(re.sub(r"[.,]", "", text[:last]) + "." + text[last + 1 :])
    return float(re.sub(r"[.,]", "", text))


def _amount(message: str, folded: str, context: ComprehensionContext) -> AmountClue | None:
    marked, bare = [], []
    for match in _AMOUNT.finditer(folded):
        approx, pre, mult, suf = (match.group(g) for g in ("approx", "pre", "mult", "suf"))
        num = match.group("num")
        if pre or mult or suf:
            marked.append(match)
        elif not (
            _NOT_AMOUNT_AFTER.match(folded, match.end())
            or _NOT_AMOUNT_BEFORE.search(folded, 0, match.start("num"))
            or re.fullmatch(r"20[0-3]\d", num)
        ):
            bare.append(match)
    match = (marked or bare or [None])[0]
    if match is None:
        return None
    value = _parse_number(match.group("num")) * _MULTIPLIERS.get(match.group("mult") or "", 1)
    if value <= 0:
        return None
    return AmountClue(
        value=round(value, 2),
        currency=_currency(match, context),
        approximate=match.group("approx") is not None,
        evidence=_cut(message, match),
    )


def _currency(match: re.Match[str], context: ComprehensionContext) -> str | None:
    for word in (match.group("pre"), match.group("suf"), match.group("mult")):
        if not word:
            continue
        word = " ".join(word.split())
        if word in _CURRENCY_WORDS:
            return _CURRENCY_WORDS[word]
        if word in _LOCAL_WORDS:
            return context.local_currency
        if word in {"usd", "mxn", "cop", "ars", "brl"}:
            return word.upper()
    return None


def _date(message: str, folded: str, context: ComprehensionContext) -> DateClue | None:
    clock = SimulatedClock(context.now)
    for kind, pattern in _DATE_PATTERNS:
        for match in pattern.finditer(folded):
            window = _window(clock, kind, match)
            if window is not None:
                evidence = _cut(message, match)
                return DateClue(
                    expression=evidence,
                    resolved_from=clock.today(),
                    window_days=window,
                    evidence=evidence,
                )
    return None


def _window(clock: SimulatedClock, kind: str, match: re.Match[str]) -> tuple[int, int] | None:
    groups = match.groupdict()
    if kind == "ago":
        n = groups["n"]
        count = int(n) if n.isdigit() else _NUMBER_WORDS[n]
        unit = groups["unit"]
        key = (
            "days_ago"
            if unit.startswith("dia")
            else ("weeks_ago" if unit.startswith("semana") else "months_ago")
        )
        return clock.relative_window(key, count)
    if kind == "weekday":
        if groups.get("pt"):
            # "segunda", "quinta" alone also mean "second", "fifth"; only a day with context.
            if not (groups["feira"] or groups["art"] or groups["pre"] or groups["post"]):
                return None
            day = _PT_DAYS[groups["pt"]]
        else:
            day = _ES_DAYS[groups["es"]]
        past = bool(groups["pre"] or groups["post"])
        return clock.relative_window(f"last_{day}" if past else day)
    return clock.relative_window(kind)


def _merchant(message: str) -> TextClue | None:
    for match in _MERCHANT.finditer(message):
        name = match.group("name").rstrip(".-'")
        if not any(ch.isalpha() for ch in name):
            continue
        if any(_fold(word).strip(".") in _NOT_MERCHANT for word in name.split()):
            continue
        start = match.start("name")
        return TextClue(value=name, evidence=message[start : start + len(name)])
    return None


def _language(message: str, folded: str, context: ComprehensionContext) -> LanguageVariant:
    pt = len(_PT_MARKERS.findall(folded)) + sum(message.count(ch) for ch in "ãõçÃÕÇ")
    es = len(_ES_MARKERS.findall(folded)) + sum(message.count(ch) for ch in "ñ¿¡Ñ")
    if pt > es:
        return "pt-BR"
    if _VOSEO.search(message):
        return "es-AR"
    # es-MX when the country has no Spanish variant: Mexico is the largest country of the cohort.
    return _VARIANT_BY_COUNTRY.get(context.country_code, "es-MX")
