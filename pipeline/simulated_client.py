"""Simulated client of the evaluation harness (TRZ-43, design 13.2).

Deterministic and without an LLM (CA1). It opens with the first message of the case and then
answers what the system asks with the ground truth of the case: a clue with the form and value the
noise model drew (CA2), the true charge among the options or "none" (CA3), "still not recognized"
or "I recognize it now" after the detail (CA4), and "yes" to a confirmation (CA5). It reads what is
asked from the structured turn of the system, never from the text, so TRAZO and the free agent are
answered alike. A conversation has at most 8 customer turns (CA6).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from pipeline.cases.render import amount_text
from pipeline.cases.schema import CaseRecord

TEMPLATES = Path("eval/templates/simulated_client.yaml")
MAX_TURNS = 8
WEEKDAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# A request for one more detail (TRAZO asks for "the amount or the approximate date") is answered
# with the clues of the first group not yet given; when both are, with the next one of the rest.
DETAIL_FIRST = ("amount", "date")
DETAIL_REST = ("merchant", "channel", "product")

Asked = Literal["ask_detail", "show_options", "recognition", "confirm", "text_question", "done"]


@dataclass(frozen=True)
class SystemTurn:
    """What a system asked the customer at the end of a turn, as structure.

    Attributes:
        asked: The kind of request; `done` when the system expects nothing.
        options: Ids shown to choose from (`show_options`): transactions, or claims.
        action_id: Pending action to confirm (`confirm`).
    """

    asked: Asked
    options: tuple[str, ...] = ()
    action_id: str | None = None


@dataclass(frozen=True)
class ClientTurn:
    """One customer turn: the message and, for a button, its value.

    Attributes:
        message: Text the customer writes.
        option: Chosen transaction id, or "none".
        recognition: not_recognized or recognized.
        confirm_action_id: Action the customer confirms.
        clues: Clues the message gives, for the trace.
    """

    message: str
    option: str | None = None
    recognition: Literal["not_recognized", "recognized"] | None = None
    confirm_action_id: str | None = None
    clues: tuple[str, ...] = ()


def load_templates(path: Path = TEMPLATES) -> dict[str, Any]:
    """Reads the answer templates.

    Args:
        path: YAML file.

    Returns:
        The parsed templates, with their version.
    """
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


class SimulatedClient:
    """Answers one case, turn by turn."""

    def __init__(self, case: CaseRecord, templates: dict[str, Any]) -> None:
        """Starts the conversation of a case.

        Args:
            case: The case in one variant.
            templates: Parsed answer templates.
        """
        self.case = case
        self.vb: dict[str, Any] = templates["variants"][case.variant]
        n = case.noise
        self.given = {
            name
            for name, mentioned in (
                ("amount", n.amount.mentioned),
                ("date", n.date.mentioned),
                ("merchant", n.merchant.mentioned),
                ("channel", n.channel.mentioned),
                ("product", n.product.mentioned),
            )
            if mentioned
        }
        self.turns = 0

    def first(self) -> ClientTurn:
        """The first message of the case."""
        self.turns = 1
        return ClientTurn(message=self.case.message, clues=tuple(sorted(self.given)))

    def answer(self, turn: SystemTurn) -> ClientTurn | None:
        """The next customer turn, or None when the conversation ends.

        Args:
            turn: What the system asked.

        Returns:
            The answer; None when the system expects nothing or the turn cap is reached.
        """
        if turn.asked == "done" or self.turns >= MAX_TURNS:
            return None
        self.turns += 1
        vb, truth = self.vb, self.case.truth
        if turn.asked == "show_options":
            # A claim status case chooses its complaint among the claims shown.
            for true_id in (truth.transaction_id, truth.complaint_id):
                if true_id is not None and true_id in turn.options:
                    return ClientTurn(vb["pick_option"], option=true_id)
            return ClientTurn(vb["no_option"], option="none")
        if turn.asked == "recognition":
            if truth.recognizes_after_detail:
                return ClientTurn(vb["recognized"], recognition="recognized")
            return ClientTurn(vb["not_recognized"], recognition="not_recognized")
        if turn.asked == "confirm":
            return ClientTurn(vb["confirm"], confirm_action_id=turn.action_id)
        if turn.asked == "text_question":
            return self._all_clues()
        return self._detail()

    def _detail(self) -> ClientTurn:
        texts = self._clue_texts()
        wanted = [c for c in DETAIL_FIRST if c in texts and c not in self.given]
        if not wanted:
            wanted = [c for c in DETAIL_REST if c in texts and c not in self.given][:1]
        if not wanted:
            return ClientTurn(self.vb["nothing_more"])
        self.given.update(wanted)
        return ClientTurn(
            _sentence(self.vb["detail"], [texts[c] for c in wanted]), clues=tuple(wanted)
        )

    def _all_clues(self) -> ClientTurn:
        texts = self._clue_texts()
        order = [*DETAIL_FIRST, *DETAIL_REST]
        names = [c for c in order if c in texts]
        self.given.update(names)
        message = _sentence(self.vb["all_clues"], [texts[c] for c in names])
        card = self.case.truth.card_in_possession
        if card is not None:
            message = f"{message} {self.vb['card'][str(card).lower()]}"
        return ClientTurn(message, clues=tuple(names))

    def _clue_texts(self) -> dict[str, str]:
        """Every clue the customer can state, in the variant and with the drawn form."""
        n, t, vb = self.case.noise, self.case.truth, self.vb
        out: dict[str, str] = {}
        if n.amount.value is not None and n.amount.currency and n.amount.form:
            out["amount"] = amount_text(n.amount, vb, t.local_currency)
        d = n.date
        if d.expression and d.window_start:
            if d.expression == "exact":
                out["date"] = vb["date"]["exact"].format(
                    day=d.window_start.day, month=vb["months"][d.window_start.month - 1]
                )
            elif d.expression.startswith("last_") and d.expression[5:] in WEEKDAY_KEYS:
                index = WEEKDAY_KEYS.index(d.expression[5:])
                # Portuguese: sábado and domingo are masculine, the other weekdays feminine.
                form = (
                    "last_weekend_day"
                    if index >= 5 and "last_weekend_day" in vb["date"]
                    else "last_weekday"
                )
                out["date"] = vb["date"][form].format(weekday=vb["weekdays"][index])
            else:
                out["date"] = vb["date"][d.expression]
        if n.merchant.value:
            if n.merchant.form == "category":
                out["merchant"] = vb["category"].format(category=vb["categories"][n.merchant.value])
            else:
                out["merchant"] = vb["merchant"].format(merchant=n.merchant.value)
        if t.channel in vb["channel"]:
            out["channel"] = vb["channel"][t.channel]
        if t.product_type in vb["product"]:
            out["product"] = vb["product"][t.product_type]
        return out


def _sentence(template: str, parts: list[str]) -> str:
    text = template.format(clues=", ".join(parts))
    return text[0].upper() + text[1:]
