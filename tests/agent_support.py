"""A simulated LLM and the agent dependencies for integration tests of full customer turns."""

import json
from collections.abc import Callable
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.adapters.llm import TRANSLATE_SYSTEM, LLMClient
from app.core.config import Settings
from app.domain.business_days import load_calendars
from app.domain.clock import SimulatedClock
from app.domain.policy import PolicyEngine, initial_autonomy
from app.domain.policy_passages import load_passages
from app.main import load_identification
from app.services.agent import AgentDeps


def llm_settings(database_url: str, **overrides: Any) -> Settings:
    """Settings of a service whose LLM is the simulated one below."""
    values: dict[str, Any] = {
        "database_url": database_url,
        "llm_enabled": True,
        "llm_provider": "local",
        "llm_base_url": "http://llm.test/v1",
        "llm_model_primary": "test-model",
        "anthropic_api_key": "",
    }
    values.update(overrides)
    return Settings(**values)


def reading(intent: str, language: str = "es-CO", **clues: dict[str, Any] | None) -> dict:
    """A comprehension reading as the LLM returns it; every clue needs literal evidence."""
    fields = ("amount", "date", "merchant_hint", "channel_hint", "card_in_possession")
    return {
        "intent": intent,
        **{name: clues.get(name) for name in fields},
        "language": language,
    }


def fake_llm(
    settings: Settings,
    answer: dict | Callable[[str], dict],
    sent: list[str] | None = None,
    reply: str | Callable[[dict[str, Any]], str] = "Revisaremos el cargo.",
    translation: str = "Traducción simulada.",
) -> LLMClient:
    """An LLM that reads every message as `answer`, or as what `answer` reads from the message,
    writes `reply`, or what `reply` writes from the facts it is given, and translates any
    message as `translation`."""
    client: LLMClient

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if sent is not None:
            sent.append(json.dumps(body, ensure_ascii=False))
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        if system.startswith(client.comprehension_prompt.system[:40]):
            message = user.split("Message: ", 1)[1]
            content = json.dumps(answer(message) if callable(answer) else answer)
        elif system == TRANSLATE_SYSTEM:
            content = translation
        elif callable(reply):
            content = reply(json.loads(user.split("Facts (JSON): ", 1)[1]))
        else:
            content = reply
        usage = {"prompt_tokens": 120, "completion_tokens": 30}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}], "usage": usage}
        )

    http = httpx.Client(base_url=settings.llm_base_url, transport=httpx.MockTransport(handler))
    client = LLMClient(settings, http_client=http)
    return client


def agent_deps(settings: Settings, llm: LLMClient) -> AgentDeps:
    """The agent dependencies the service builds, with the given LLM."""
    policy = PolicyEngine.from_file(settings.policy_path)
    return AgentDeps(
        policy=policy,
        llm=llm,
        clock=SimulatedClock(settings.trazo_now),
        identification=load_identification(settings, policy),
        autonomy=initial_autonomy(policy.config),
        passages=load_passages(settings.policy_passages_path, policy.config.dispute_window_days),
        calendars=load_calendars(settings.holidays_path),
    )


def still_not_recognized(
    client: TestClient, case_id: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Presses "Sigo sin reconocerlo" on the recognition step of a case through /chat."""
    r = client.post(
        "/chat",
        json={
            "message": "Sigo sin reconocerlo",
            "case_id": case_id,
            "recognition": "not_recognized",
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()
