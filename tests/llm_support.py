"""A simulated Anthropic Messages API: the real SDK of the LLM client talks to it over httpx2, the
httpx fork the SDK uses, so tests exercise the client's own timeout, retry budget and typed
errors."""

import json
from collections.abc import Callable
from typing import Any

import httpx2 as httpx

from app.core.config import Settings

Handler = Callable[[httpx.Request], httpx.Response]


def llm_test_settings(**overrides: Any) -> Settings:
    """Settings of an LLM client under test.

    Explicit so CI's LLM_ENABLED=false does not disable it. The LLM client never connects to the
    database; the URL only satisfies Settings. The retry does not wait, to keep tests fast.
    """
    values: dict[str, Any] = {
        "database_url": "postgresql+psycopg://unused@localhost:1/unused",
        "llm_enabled": True,
        "anthropic_api_key": "test-key",
        "llm_model_primary": "test-model",
        "llm_max_retries": 1,
        "llm_retry_wait_seconds": 0.0,
    }
    values.update(overrides)
    return Settings(**values)


def request_parts(request: httpx.Request) -> tuple[str, str, dict[str, Any]]:
    """The system prompt, the user turn and the whole body of a Messages API request."""
    body = json.loads(request.content)
    system = "".join(block["text"] for block in body.get("system", []))
    return system, body["messages"][0]["content"], body


def message(text: str, input_tokens: int = 120, output_tokens: int = 30) -> httpx.Response:
    """A successful Messages API response with one text block."""
    return httpx.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "test-model",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    )


def api_error(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    """An error response of the Messages API with the given status."""
    return httpx.Response(
        status,
        headers=headers,
        json={"type": "error", "error": {"type": "api_error", "message": "simulated"}},
    )


def anthropic_http(handler: Handler) -> httpx.Client:
    """An httpx client for the SDK whose every request is answered by `handler`."""
    return httpx.Client(transport=httpx.MockTransport(handler))
