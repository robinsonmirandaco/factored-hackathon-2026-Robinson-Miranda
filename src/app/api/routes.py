"""HTTP routes. Thin: validate input, call one service, return a schema."""

from fastapi import APIRouter

from app.api.deps import RuntimeDep, SessionDep
from app.schemas.api import (
    CaseOut,
    ChatIn,
    ChatOut,
    HealthOut,
    HumanDecisionIn,
    MetricsOut,
    TraceEventOut,
)
from app.services import cases
from app.services.agent import handle_message

router = APIRouter()

_ERRORS = {
    404: {"description": "Not found"},
    409: {"description": "Conflict with the current state"},
    422: {"description": "Invalid input"},
    503: {"description": "A dependency is unavailable"},
}


@router.get("/health", response_model=HealthOut, responses={503: _ERRORS[503]})
def health(session: SessionDep, runtime: RuntimeDep) -> HealthOut:
    """Reports liveness, database reachability and the active configuration."""
    cases.check_database(session)
    s = runtime.settings
    return HealthOut(
        status="ok",
        app_env=s.app_env,
        db="ok",
        llm_provider=s.llm_provider,
        llm_available=runtime.agent.llm.available,
    )


@router.post("/chat", response_model=ChatOut, responses={404: _ERRORS[404], 422: _ERRORS[422]})
def chat(body: ChatIn, session: SessionDep, runtime: RuntimeDep) -> ChatOut:
    """Handles one customer turn: understand, decide, act or escalate, reply."""
    r = handle_message(
        session, runtime.agent, body.customer_id, body.message, body.confirm, body.case_id
    )
    return ChatOut(
        case_id=r.case_id,
        trace_id=r.trace_id,
        intent=r.intent,
        reply=r.reply,
        outcome=r.outcome,
        autonomy_level=r.autonomy_level,
        actions_taken=r.actions_taken,
        llm_fallback=r.llm_fallback,
        tokens=r.tokens,
        latency_ms=r.latency_ms,
    )


@router.get("/cases/{case_id}", response_model=CaseOut, responses={404: _ERRORS[404]})
def get_case(case_id: str, session: SessionDep) -> CaseOut:
    """Returns one case."""
    return cases.get_case(session, case_id)


@router.get("/cases/{case_id}/trace", response_model=list[TraceEventOut])
def get_trace(case_id: str, session: SessionDep) -> list[TraceEventOut]:
    """Returns every audit row of a case in write order."""
    return cases.get_trace(session, case_id)


@router.get("/queue", response_model=list[CaseOut])
def queue(session: SessionDep) -> list[CaseOut]:
    """Returns the escalated cases waiting for a human, oldest first."""
    return cases.list_queue(session)


@router.post(
    "/cases/{case_id}/decision",
    response_model=CaseOut,
    responses={404: _ERRORS[404], 409: _ERRORS[409], 422: _ERRORS[422]},
)
def human_decision(case_id: str, body: HumanDecisionIn, session: SessionDep) -> CaseOut:
    """Records an operator decision on an escalated case."""
    return cases.record_decision(session, case_id, body)


@router.get("/metrics", response_model=MetricsOut)
def metrics(session: SessionDep) -> MetricsOut:
    """Returns operational counters from the cases table and the audit log."""
    return cases.get_metrics(session)
