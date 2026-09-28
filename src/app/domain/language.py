"""Language of a customer message (TRZ-11): Spanish or Portuguese, its variant, and whether the
message mixes both.

The detector counts signals of each language: letters only one of them writes (ã, õ, ç against
ñ, ¿, ¡) and frequent words. It was measured on the development split before any statistical
detector was considered and kept because it was enough (docs/reports/idioma.md): it costs
nothing, adds no dependency and gives the same answer every time.

The variant (MX, CO, AR, BR) is not something a count of words can tell; it comes from the LLM,
or from the customer's country when the LLM did not answer.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from app.schemas.comprehension import LanguageVariant

Language = Literal["es", "pt"]
Source = Literal["detector", "llm", "previous", "default"]

DETECTOR_VERSION = "language-rules-1"
# Shorter messages ("sí", "sim, pode") carry too few words to count on (CA3).
MIN_WORDS = 4

PT_LETTERS = "ãõçÃÕÇ"
ES_LETTERS = "ñ¿¡Ñ"


def _any(*words: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b")


# Frequent words of each language, compared without accents. Some also occur in the other
# language ("no" is a Portuguese contraction), which is why the larger count decides.
PT_MARKERS = _any(
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
ES_MARKERS = _any(
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
# Words a native writer of the other language would not use: seeing both kinds in one message
# is what marks it as mixed (CA4).
PT_STRONG = _any(
    r"nao",
    r"voce",
    r"cartao",
    r"cobrancas?",
    r"reconheco",
    r"obrigad[oa]",
    r"gostaria",
    r"tambem",
    r"entao",
    r"fatura",
    r"ontem",
    r"meu",
    r"minha",
    r"estou",
    r"preciso",
)
ES_STRONG = _any(
    r"tarjeta",
    r"reconozco",
    r"gracias",
    r"necesito",
    r"quiero",
    r"entonces",
    r"ayer",
    r"cobraron",
    r"tengo",
    r"hice",
    r"yo",
    r"pero",
    r"mi",
)

VARIANT_BY_COUNTRY: dict[str, LanguageVariant] = {"MX": "es-MX", "CO": "es-CO", "AR": "es-AR"}


def _fold(text: str) -> str:
    out = []
    for ch in text:
        base = unicodedata.normalize("NFD", ch)[0].lower()
        out.append(base if len(base) == 1 else ch)
    return "".join(out)


@dataclass(frozen=True)
class Signals:
    """Counts of each language in one message.

    Attributes:
        pt: Portuguese letters and words.
        es: Spanish letters and words.
        pt_strong: Portuguese letters and strong words.
        es_strong: Spanish letters and strong words.
    """

    pt: int
    es: int
    pt_strong: int
    es_strong: int

    @property
    def predominant(self) -> Language | None:
        """The language with more signals, or None on a tie."""
        if self.pt == self.es:
            return None
        return "pt" if self.pt > self.es else "es"

    @property
    def mixed(self) -> bool:
        """Strong signals of both languages in the same message."""
        return self.pt_strong > 0 and self.es_strong > 0


def signals(message: str) -> Signals:
    """Counts the signals of Spanish and Portuguese in a message.

    Args:
        message: Customer message (redacted or not; placeholders carry no signal).

    Returns:
        The counts.
    """
    folded = _fold(message)
    pt_letters = sum(message.count(ch) for ch in PT_LETTERS)
    es_letters = sum(message.count(ch) for ch in ES_LETTERS)
    return Signals(
        pt=len(PT_MARKERS.findall(folded)) + pt_letters,
        es=len(ES_MARKERS.findall(folded)) + es_letters,
        pt_strong=len(PT_STRONG.findall(folded)) + pt_letters,
        es_strong=len(ES_STRONG.findall(folded)) + es_letters,
    )


@dataclass(frozen=True)
class LanguageDecision:
    """The language a turn is answered in, and where it came from.

    Attributes:
        language: es or pt.
        variant: es-MX, es-CO, es-AR or pt-BR.
        mixed: The message has strong signals of both languages; the answer uses the
            predominant one.
        source: detector, llm, previous (the language of the case so far) or default.
    """

    language: Language
    variant: LanguageVariant
    mixed: bool
    source: Source


def decide(
    message: str,
    llm_variant: LanguageVariant | None,
    previous: Language | None,
    country_code: str,
) -> LanguageDecision:
    """Decides the language and the variant of one turn.

    A message of 4 words or more takes the language with more signals (CA1, CA4). A shorter
    one, or a tie, takes the language of the LLM, else the language of the case so far (CA3),
    else whatever signal it has, else Spanish. The variant is the LLM's when it agrees with that
    language; otherwise the customer's country for Spanish and BR for Portuguese (CA2).

    Args:
        message: Customer message.
        llm_variant: Variant the LLM read, or None when the LLM did not answer.
        previous: Language of the case before this turn, or None for a new case.
        country_code: Country of the customer, MX, CO or AR.

    Returns:
        The decision.
    """
    found = signals(message)
    llm_language: Language | None = None
    if llm_variant is not None:
        llm_language = "pt" if llm_variant == "pt-BR" else "es"
    language: Language
    source: Source
    if len(message.split()) >= MIN_WORDS and found.predominant is not None:
        language, source = found.predominant, "detector"
    elif llm_language is not None:
        language, source = llm_language, "llm"
    elif previous is not None:
        language, source = previous, "previous"
    elif found.predominant is not None:
        language, source = found.predominant, "detector"
    else:
        language, source = "es", "default"
    variant: LanguageVariant
    if llm_variant is not None and llm_language == language:
        variant = llm_variant
    elif language == "pt":
        variant = "pt-BR"
    else:
        # es-MX when the country has no Spanish variant: Mexico is the largest country of the
        # cohort.
        variant = VARIANT_BY_COUNTRY.get(country_code, "es-MX")
    return LanguageDecision(language, variant, found.mixed, source)
