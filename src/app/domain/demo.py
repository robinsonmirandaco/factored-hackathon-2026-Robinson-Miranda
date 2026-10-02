"""Configuration of the demo state (TRZ-38): config/demo.yaml, validated at load.

The file says how the demo people are chosen and which turns the seed and the reset send to the
agent; it never holds a customer, a charge or any other value of the dataset. Pure: no database,
no I/O besides reading the file.
"""

import string
from collections.abc import Collection
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.recognition import Choice

Country = Literal["AR", "CO", "MX"]
ChargeType = Literal["Purchase", "Payment", "Withdrawal"]
# The case status a script must leave, as the agent writes it.
Status = Literal["registered_verified", "pending_analyst_approval", "escalated"]
# Invented document numbers carry this prefix, which no document of the dataset has.
DOCUMENT_PREFIX = "DEMO-"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChargeRule(_Strict):
    """A disputable charge a role needs, inside the dispute window of the policy.

    Attributes:
        type: Transaction type.
        card: The charge is on an active credit or debit card.
        usd: USD amount above the first value and up to the second; None has no upper bound.
        same_merchant: Exactly this many charges of the same merchant on that product in the
            window; None does not look at the merchant.
        currency: Currency of the charge; None takes any.
    """

    type: ChargeType
    card: bool = False
    usd: tuple[float, float | None]
    same_merchant: int | None = Field(default=None, ge=1)
    currency: str | None = None


class Turn(_Strict):
    """One customer turn of a script: a message, or a button pressed with its label.

    Attributes:
        say: The text the customer writes, or the label of the button.
        button: Key of the charge whose "No lo reconozco" button is pressed.
        option: Position of the option chosen among those shown.
        recognition: Answer to the recognition step.
        confirm: Confirms the pending action the previous turn offered.
    """

    say: str = Field(min_length=1)
    button: str | None = None
    option: int | None = Field(default=None, ge=0)
    recognition: Choice | None = None
    confirm: bool = False

    @model_validator(mode="after")
    def _one_kind(self) -> "Turn":
        kinds = [self.button, self.option, self.recognition, self.confirm or None]
        if sum(k is not None for k in kinds) > 1:
            raise ValueError("a turn presses at most one button")
        return self

    def merchants(self) -> set[str]:
        """Keys of the charges whose merchant the text names."""
        return {f.split(".")[0] for _, f, _, _ in string.Formatter().parse(self.say) if f}


class Script(_Strict):
    """Turns sent in order on one case, and the status the case must end in."""

    turns: list[Turn] = Field(min_length=1)
    expect: Status


class Document(_Strict):
    """An invented identity document: the customer's own type and a number of the demo."""

    type: str = Field(min_length=1)
    number: str

    @model_validator(mode="after")
    def _publishable(self) -> "Document":
        if not self.number.startswith(DOCUMENT_PREFIX):
            raise ValueError(f"a demo document number starts with {DOCUMENT_PREFIX}")
        return self


class Persona(_Strict):
    """A person the jury signs in as, with an invented document.

    Attributes:
        role: Name of the role.
        country: Country of the customer.
        document: Type (the customer's own) and invented number.
        walkthrough: Steps of design 10.3 the persona makes possible.
        charges: Charges the persona needs, by key.
        rehearsals: Scripts the customer must pass, each rolled back.
    """

    role: str
    country: Country
    document: Document
    walkthrough: str
    charges: dict[str, ChargeRule]
    rehearsals: list[Script] = Field(min_length=1)

    @model_validator(mode="after")
    def _keys(self) -> "Persona":
        _check_keys(self.role, self.charges, self.rehearsals)
        return self


class Seeded(_Strict):
    """A case of the starting queue, created at every seed and reset."""

    role: str
    country: Country
    charges: dict[str, ChargeRule]
    script: Script

    @model_validator(mode="after")
    def _keys(self) -> "Seeded":
        _check_keys(self.role, self.charges, [self.script])
        # A reset reads only the charge id of a seeded role, not its merchant.
        if any(t.merchants() for t in self.script.turns):
            raise ValueError(f"{self.role}: a seeded script does not name a merchant")
        if len(self.charges) > 1:
            raise ValueError(f"{self.role}: a seeded role uses at most one charge")
        return self


class Reject(_Strict):
    """A reversal and its reason of the closed list."""

    reject: str


class Cell(Seeded):
    """The cases of one autonomy cell and the analyst decision taken on each, in order."""

    decisions: list[Literal["approve"] | Reject] = Field(min_length=1)

    @property
    def reviews(self) -> int:
        """Cases of the cell, one per decision."""
        return len(self.decisions)

    @property
    def reversals(self) -> int:
        """Decisions that reverse the system."""
        return sum(isinstance(d, Reject) for d in self.decisions)


class DemoConfig(_Strict):
    """config/demo.yaml."""

    version: str
    order_seed: str = Field(min_length=1)
    complaint_lookback_days: int = Field(ge=0)
    max_candidates: int = Field(ge=1)
    seed_analyst: str = Field(min_length=1)
    personas: list[Persona]
    seeded: list[Seeded]
    pt_cell: Cell

    @model_validator(mode="after")
    def _unique(self) -> "DemoConfig":
        roles = [p.role for p in self.personas] + [s.role for s in self.seeded]
        roles.append(self.pt_cell.role)
        if len(roles) != len(set(roles)):
            raise ValueError("every role has its own name")
        numbers = [p.document.number for p in self.personas]
        if len(numbers) != len(set(numbers)):
            raise ValueError("every persona has its own document")
        return self

    def check_reasons(self, allowed: Collection[str]) -> None:
        """Checks every reversal against the closed list of the policy.

        Args:
            allowed: `autonomy.reversal_reasons`.

        Raises:
            ValueError: For a reason outside the list.
        """
        for d in self.pt_cell.decisions:
            if isinstance(d, Reject) and d.reject not in allowed:
                raise ValueError(f"reversal reason {d.reject} is not in the policy list")


def _check_keys(role: str, charges: dict[str, ChargeRule], scripts: list[Script]) -> None:
    for script in scripts:
        for turn in script.turns:
            named = turn.merchants() | ({turn.button} if turn.button else set())
            if missing := named - set(charges):
                raise ValueError(f"{role} names charges it does not ask for: {sorted(missing)}")


def load_demo(path: str | Path) -> DemoConfig:
    """Reads and validates config/demo.yaml.

    Args:
        path: The file.

    Returns:
        The configuration.

    Raises:
        pydantic.ValidationError: When the file breaks a rule above.
    """
    with open(path, encoding="utf-8") as f:
        return DemoConfig.model_validate(yaml.safe_load(f))
