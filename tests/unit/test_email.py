"""Rules and provider of the transactional email (TRZ-33): allowlist, retry schedule, the text of
the email and the Resend adapter with its timeout and idempotency key."""

import ast
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.adapters import email as adapter
from app.adapters.email import EmailError, ResendProvider
from app.domain.email import EmailConfig, allowed, compose_email, next_attempt, parse_allowlist

NOW = datetime(2026, 10, 3, 12, 0)
CONFIG = EmailConfig(test_inbox="qa@example.test", allowlist=frozenset({"qa@example.test"}))


def test_the_allowlist_is_read_trimmed_and_in_lower_case() -> None:
    assert parse_allowlist(" QA@Example.test, other@example.test ,") == frozenset(
        {"qa@example.test", "other@example.test"}
    )


def test_only_an_address_of_the_allowlist_may_receive_email() -> None:
    assert allowed("QA@example.test", CONFIG.allowlist)
    assert not allowed("someone@example.test", CONFIG.allowlist)
    # An empty list allows nothing.
    assert not allowed("qa@example.test", frozenset())


def test_retries_wait_1_2_4_and_8_minutes_and_the_fifth_failure_is_final() -> None:
    waits = [next_attempt(n, NOW, CONFIG) for n in range(1, 6)]
    assert waits[:4] == [NOW + timedelta(minutes=m) for m in (1, 2, 4, 8)]
    assert waits[4] is None


def test_the_schedule_is_configurable() -> None:
    config = EmailConfig("qa@example.test", frozenset(), max_attempts=3, retry_minutes=(10,))
    assert next_attempt(1, NOW, config) == NOW + timedelta(minutes=10)
    assert next_attempt(2, NOW, config) == NOW + timedelta(minutes=10)
    assert next_attempt(3, NOW, config) is None


@pytest.mark.parametrize(
    ("language", "subject", "footer"),
    [
        ("es", "TRAZO [simulado]: novedades en tu aclaración", "[simulado] Correo de prueba"),
        ("pt", "TRAZO [simulado]: novidades na sua contestação", "[simulado] E-mail de teste"),
    ],
)
def test_the_email_repeats_the_notification_with_its_case_and_says_it_is_a_test(
    language: str, subject: str, footer: str
) -> None:
    s, body = compose_email("Una analista aprobó tu aclaración.", "CASE-1", language)
    assert s == subject
    assert body.startswith("Una analista aprobó tu aclaración.\n\nCaso CASE-1.")
    assert footer in body


class _Calls:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def __call__(self, url: str, **kwargs: Any) -> Any:
        self.kwargs = {"url": url, **kwargs}
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _provider(monkeypatch: pytest.MonkeyPatch, response: Any) -> tuple[ResendProvider, _Calls]:
    calls = _Calls(response)
    monkeypatch.setattr(adapter.httpx, "post", calls)
    return ResendProvider("re_test", "onboarding@resend.dev", 5.0), calls


def test_resend_gets_one_call_with_its_timeout_and_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, calls = _provider(monkeypatch, httpx.Response(200, json={"id": "e1"}))
    provider.send("qa@example.test", "S", "B", "email-outbox-7")
    assert calls.kwargs["url"] == "https://api.resend.com/emails"
    assert calls.kwargs["timeout"] == 5.0
    assert calls.kwargs["headers"]["idempotency-key"] == "email-outbox-7"
    assert calls.kwargs["json"] == {
        "from": "onboarding@resend.dev",
        "to": ["qa@example.test"],
        "subject": "S",
        "text": "B",
    }


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(500), "http_500"),
        (httpx.Response(422), "http_422"),
        (httpx.ReadTimeout("slow"), "ReadTimeout"),
        (httpx.ConnectError("down"), "ConnectError"),
    ],
)
def test_any_failure_of_resend_is_an_email_error(
    monkeypatch: pytest.MonkeyPatch, response: Any, error: str
) -> None:
    provider, _ = _provider(monkeypatch, response)
    with pytest.raises(EmailError, match=error):
        provider.send("qa@example.test", "S", "B", "k")


def test_every_notification_of_the_code_can_go_by_email() -> None:
    # Robinson's decision D4: the five kinds of notification also go by email with the flag on.
    calls = [
        node
        for path in (Path(__file__).resolve().parents[2] / "src").rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "notify"
    ]
    kinds = {c.args[2].value for c in calls if isinstance(c.args[2], ast.Constant)}
    assert kinds == {"approved", "rejected", "info_requested", "audit_reversed", "info_expired"}
    assert all("email" in {k.arg for k in c.keywords} for c in calls)
