"""Email provider behind an interface (TRZ-33): Resend over HTTP, with an explicit timeout.

The outbox process retries a failed send on its own schedule, so the provider makes a single
call per attempt and raises EmailError on any failure. The outbox id is the idempotency key, so
an attempt retried after a timeout that did reach the provider is not delivered twice.
"""

from typing import Protocol

import httpx

RESEND_URL = "https://api.resend.com/emails"


class EmailError(Exception):
    """The provider did not accept the email."""


class EmailProvider(Protocol):
    """Sends one email."""

    def send(self, to: str, subject: str, body: str, idempotency_key: str) -> None:
        """Sends one plain-text email.

        Args:
            to: Recipient.
            subject: Subject line.
            body: Plain-text body.
            idempotency_key: Key the provider uses to deliver a retried email once.

        Raises:
            EmailError: If the provider did not accept it.
        """
        ...


class ResendProvider:
    """The Resend HTTP API.

    Args:
        api_key: Resend API key, read from the environment.
        sender: From address; onboarding@resend.dev delivers only to the account owner.
        timeout_seconds: Deadline of each call.
    """

    def __init__(self, api_key: str, sender: str, timeout_seconds: float) -> None:
        self._api_key = api_key
        self._sender = sender
        self._timeout = timeout_seconds

    def send(self, to: str, subject: str, body: str, idempotency_key: str) -> None:
        """Sends one plain-text email through Resend.

        Args:
            to: Recipient.
            subject: Subject line.
            body: Plain-text body.
            idempotency_key: Resend's Idempotency-Key header.

        Raises:
            EmailError: On a timeout, a network failure or a status other than 2xx.
        """
        try:
            r = httpx.post(
                RESEND_URL,
                headers={
                    "authorization": f"Bearer {self._api_key}",
                    "idempotency-key": idempotency_key,
                },
                json={"from": self._sender, "to": [to], "subject": subject, "text": body},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise EmailError(type(exc).__name__) from exc
        if not r.is_success:
            raise EmailError(f"http_{r.status_code}")
