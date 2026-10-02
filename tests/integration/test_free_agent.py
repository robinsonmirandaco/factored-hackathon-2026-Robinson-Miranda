"""Free agent baseline through the harness, against a scripted API (TRZ-44 CA2, CA3, CA4)."""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from app.core.config import Settings
from pipeline.harness import Staging, run_case, score
from pipeline.llm_replay import Budget, Pace, ReplayCache, ReplayTransport
from pipeline.simulated_client import load_templates
from tests.case_support import FixtureData, netflix_case

pytestmark = pytest.mark.integration

TEMPLATES = load_templates()


def _message(content: list[dict[str, Any]], stop: str = "tool_use") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-4-5-20251001",
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    )


def _use(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": tool_input}


class ScriptedApi(httpx2.BaseTransport):
    """Answers the free agent's requests in order and keeps every request body."""

    def __init__(self, script: list[httpx2.Response]) -> None:
        self.script = script
        self.bodies: list[dict[str, Any]] = []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.read()))
        return self.script[len(self.bodies) - 1]


def agent_staging(tmp_path: Path, api: ScriptedApi) -> Staging:
    settings = Settings(llm_enabled=True, anthropic_api_key="test-key", log_level="WARNING")
    replay = ReplayTransport(ReplayCache(tmp_path), 1, Budget(1.0), Pace(1000), api)
    return Staging(settings, FixtureData(), replay)


def test_the_free_agent_acts_through_the_same_tools_on_the_session_customer(
    tmp_path: Path,
) -> None:
    api = ScriptedApi(
        [
            _message([_use("list_transactions", {})]),
            _message([_use("show_charge", {"handle": "T2"})]),
            _message(
                [_use("request_confirmation", {"action": "register_dispute", "handle": "T2"})]
            ),
            _message(
                [_use("register_dispute", {"handle": "T2", "dispute_type": "unrecognized_charge"})]
            ),
            _message([{"type": "text", "text": "Listo."}], "end_turn"),
        ]
    )
    case = netflix_case()

    run = run_case(case, "free_agent", 1, agent_staging(tmp_path, api), TEMPLATES)

    assert run.error is None
    assert run.final.disputes == [("C1", "TX-1", "unrecognized_charge")]
    assert run.policy_violations == []
    assert score(run, case).correct is True
    listed = json.loads(api.bodies[1]["messages"][-1]["content"][0]["content"])
    # The model sees handles, never dataset ids, and only the session customer's charges.
    assert [t["handle"] for t in listed["transactions"]] == ["T1", "T2"]
    assert "TX-1" not in json.dumps(api.bodies)


def test_the_free_agent_acting_without_confirmation_is_recorded(tmp_path: Path) -> None:
    api = ScriptedApi(
        [
            _message([_use("list_transactions", {})]),
            _message(
                [_use("register_dispute", {"handle": "T2", "dispute_type": "unrecognized_charge"})]
            ),
            _message([{"type": "text", "text": "Listo."}], "end_turn"),
        ]
    )
    case = netflix_case()

    run = run_case(case, "free_agent", 1, agent_staging(tmp_path, api), TEMPLATES)

    assert run.policy_violations == ["register_dispute_without_confirmation"]
    assert score(run, case).correct is False


def test_the_free_agent_redacts_the_message_before_the_llm(tmp_path: Path) -> None:
    api = ScriptedApi([_message([{"type": "text", "text": "Hola."}], "end_turn")])
    case = netflix_case(message="Soy Test, mi correo es test@example.com y no reconozco un cargo.")

    run_case(case, "free_agent", 1, agent_staging(tmp_path, api), TEMPLATES)

    sent = json.dumps(api.bodies[0]["messages"])
    assert "test@example.com" not in sent
