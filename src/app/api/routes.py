"""HTTP routes. Thin: validate input, call one service, return a schema."""

import dataclasses
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import (
    AnalystSessionDep,
    CustomerDep,
    CustomerSessionDep,
    PrincipalDep,
    RuntimeDep,
    SessionDep,
)
from app.domain.history import Lang
from app.domain.recognition import ChargeDetail
from app.schemas.api import (
    AnalystLoginIn,
    CaseOut,
    ChargeOut,
    ChatIn,
    ChatOut,
    ClarificationOut,
    ClueOut,
    HealthOut,
    HistoryEntryOut,
    HumanDecisionIn,
    MeOut,
    MetricsOut,
    MovementsOut,
    OtpRequestIn,
    OtpRequestOut,
    OtpVerifyIn,
    ProductOut,
    TokenOut,
    TraceEventOut,
)
from app.schemas.dossier import Dossier
from app.services import auth, cases, dossier, me
from app.services.agent import handle_message, pending_detail

router = APIRouter()

_ERRORS = {
    401: {"description": "Missing, invalid, expired or revoked session"},
    403: {"description": "The session has another role"},
    404: {"description": "Not found"},
    409: {"description": "Conflict with the current state"},
    422: {"description": "Invalid input"},
    429: {"description": "Document locked, or too many code requests for it"},
    503: {"description": "A dependency is unavailable"},
}
_AUTH = {401: _ERRORS[401], 403: _ERRORS[403]}


@router.get("/health", response_model=HealthOut, responses={503: _ERRORS[503]})
def health(session: SessionDep, runtime: RuntimeDep) -> HealthOut:
    """Reports liveness, database reachability and the active configuration."""
    cases.check_database(session)
    s = runtime.settings
    return HealthOut(
        status="ok",
        app_env=s.app_env,
        db="ok",
        llm_provider=runtime.agent.llm.provider,
        llm_available=runtime.agent.llm.available,
    )


@router.post(
    "/auth/otp/request",
    response_model=OtpRequestOut,
    status_code=202,
    responses={422: _ERRORS[422], 429: _ERRORS[429]},
)
def request_code(body: OtpRequestIn, runtime: RuntimeDep) -> OtpRequestOut:
    """Sends a one-time code; the answer is the same whether or not a customer has the document."""
    s = runtime.settings
    auth.request_code(runtime.db, s, body.document_type, body.document_number, runtime.now())
    return OtpRequestOut(expires_in_seconds=s.otp_ttl_minutes * 60)


@router.post(
    "/auth/otp/verify",
    response_model=TokenOut,
    responses={401: _ERRORS[401], 422: _ERRORS[422], 429: _ERRORS[429]},
)
def verify_code(body: OtpVerifyIn, runtime: RuntimeDep) -> TokenOut:
    """Exchanges a valid one-time code for a customer session token."""
    issued = auth.verify_code(
        runtime.db,
        runtime.settings,
        body.document_type,
        body.document_number,
        body.code,
        runtime.now(),
    )
    return _token(issued, runtime)


@router.post(
    "/auth/analyst/login",
    response_model=TokenOut,
    responses={401: _ERRORS[401], 422: _ERRORS[422]},
)
def analyst_login(body: AnalystLoginIn, runtime: RuntimeDep) -> TokenOut:
    """Exchanges the analyst test credentials for an analyst session token."""
    issued = auth.analyst_login(
        runtime.db, runtime.settings, body.username, body.password, runtime.now()
    )
    return _token(issued, runtime)


@router.post("/auth/logout", status_code=204, responses={401: _ERRORS[401]})
def logout(principal: PrincipalDep, runtime: RuntimeDep) -> Response:
    """Closes the caller's session; its token stops working at once."""
    auth.logout(runtime.db, principal, runtime.now())
    return Response(status_code=204)


def _token(issued: auth.IssuedToken, runtime: RuntimeDep) -> TokenOut:
    return TokenOut(
        access_token=issued.token,
        role=issued.role,
        expires_in_seconds=issued.expires_in_seconds,
        idle_timeout_seconds=runtime.settings.session_idle_minutes * 60,
    )


@router.post(
    "/chat",
    response_model=ChatOut,
    responses={**_AUTH, 404: _ERRORS[404], 422: _ERRORS[422]},
)
def chat(
    body: ChatIn, customer: CustomerDep, session: CustomerSessionDep, runtime: RuntimeDep
) -> ChatOut:
    """Handles one customer turn: understand, decide, act or escalate, reply."""
    # The body's customer_id never selects data. Naming someone else is an attempt to reach
    # another customer's data, which the policy stops as a security event.
    foreign = body.customer_id is not None and body.customer_id != customer.subject
    r = handle_message(
        session,
        runtime.agent,
        customer.subject,
        body.message,
        body.confirm_action_id,
        body.case_id,
        security_event=foreign,
        recognition=body.recognition,
        option=body.option,
        transaction_id=body.transaction_id,
        decline_action_id=body.decline_action_id,
    )
    pending = r.facts.get("pending_action")
    auth.remember_case(session, customer, r.case_id)
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
        charge=_charge_out(r.facts["charge"]) if "charge" in r.facts else None,
        choices=r.facts.get("choices", []),
        options=r.facts.get("options", []),
        claims=r.facts.get("claims", []),
        pending_action=(
            {**pending, **pending_detail(session, pending["action_id"])} if pending else None
        ),
        dispute_folio=(r.facts.get("dispute") or {}).get("folio"),
        clues=[ClueOut(**dataclasses.asdict(c)) for c in r.clues],
    )


def _charge_out(d: ChargeDetail) -> ChargeOut:
    return ChargeOut(
        transaction_id=d.transaction_id,
        transaction_type=d.transaction_type,
        merchant=d.merchant,
        amount=d.amount.amount,
        currency=d.amount.currency,
        converted_amount=d.amount.converted_amount,
        converted_currency=d.amount.converted_currency,
        converted_label=d.amount.label,
        at=d.at,
        channel=d.channel,
        city=d.city,
        product_type=d.product_type,
        last4=d.last4,
        status=d.status,
        twin={"at": d.twin.at, "status": d.twin.status} if d.twin else None,
        earlier_months=[m.strftime("%Y-%m") for m in d.earlier_months],
    )


@router.get("/me", response_model=MeOut, responses={**_AUTH, 404: _ERRORS[404], 503: _ERRORS[503]})
def get_me(customer: CustomerDep, session: CustomerSessionDep, runtime: RuntimeDep) -> MeOut:
    """Returns the session customer and the simulated now the screens count dates from."""
    return me.get_me(session, runtime.agent.clock, customer.subject, runtime.settings.demo_mode)


@router.get("/me/products", response_model=list[ProductOut], responses={**_AUTH, 503: _ERRORS[503]})
def get_products(customer: CustomerDep, session: CustomerSessionDep) -> list[ProductOut]:
    """Returns the products of the session customer."""
    return me.list_products(session, customer.subject)


@router.get(
    "/me/transactions",
    response_model=MovementsOut,
    responses={**_AUTH, 404: _ERRORS[404], 422: _ERRORS[422], 503: _ERRORS[503]},
)
def get_transactions(
    customer: CustomerDep,
    session: CustomerSessionDep,
    runtime: RuntimeDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    before: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> MovementsOut:
    """Returns one page of the session customer's transactions, newest first."""
    return me.list_movements(
        session,
        runtime.agent.clock,
        customer.subject,
        runtime.agent.policy.config.dispute_window_days,
        limit,
        before,
    )


@router.get(
    "/me/clarifications",
    response_model=list[ClarificationOut],
    responses={**_AUTH, 404: _ERRORS[404], 503: _ERRORS[503]},
)
def get_clarifications(
    customer: CustomerDep, session: CustomerSessionDep, runtime: RuntimeDep
) -> list[ClarificationOut]:
    """Returns the session customer's clarifications with their status and deadline."""
    a = runtime.agent
    return me.list_clarifications(session, a.clock, a.passages, a.calendars, customer.subject)


@router.get("/cases/{case_id}", response_model=CaseOut, responses={**_AUTH, 404: _ERRORS[404]})
def get_case(case_id: str, session: AnalystSessionDep) -> CaseOut:
    """Returns one case."""
    return cases.get_case(session, case_id)


@router.get("/cases/{case_id}/trace", response_model=list[TraceEventOut], responses=_AUTH)
def get_trace(case_id: str, session: AnalystSessionDep) -> list[TraceEventOut]:
    """Returns every audit row of a case in write order."""
    return cases.get_trace(session, case_id)


@router.get(
    "/cases/{case_id}/history",
    response_model=list[HistoryEntryOut],
    responses={**_AUTH, 404: _ERRORS[404], 422: _ERRORS[422], 503: _ERRORS[503]},
)
def get_history(
    case_id: str, session: AnalystSessionDep, lang: Annotated[Lang, Query()] = "es"
) -> list[HistoryEntryOut]:
    """Returns the steps of a case in plain language, in Spanish or Portuguese."""
    return cases.get_history(session, case_id, lang)


@router.get(
    "/cases/{case_id}/dossier",
    response_model=Dossier,
    responses={**_AUTH, 404: _ERRORS[404], 409: _ERRORS[409], 422: _ERRORS[422], 503: _ERRORS[503]},
)
def get_dossier(
    case_id: str,
    session: AnalystSessionDep,
    runtime: RuntimeDep,
    lang: Annotated[Lang, Query()] = "es",
) -> Dossier:
    """Returns the dossier of a case handed to a person, with every fact and its source."""
    return dossier.get_dossier(session, runtime.agent.llm, case_id, lang)


@router.get("/queue", response_model=list[CaseOut], responses=_AUTH)
def queue(session: AnalystSessionDep) -> list[CaseOut]:
    """Returns the escalated cases waiting for a human, oldest first."""
    return cases.list_queue(session)


@router.post(
    "/cases/{case_id}/decision",
    response_model=CaseOut,
    responses={**_AUTH, 404: _ERRORS[404], 409: _ERRORS[409], 422: _ERRORS[422]},
)
def human_decision(case_id: str, body: HumanDecisionIn, session: AnalystSessionDep) -> CaseOut:
    """Records an operator decision on an escalated case."""
    return cases.record_decision(session, case_id, body)


@router.get("/metrics", response_model=MetricsOut, responses=_AUTH)
def metrics(session: AnalystSessionDep) -> MetricsOut:
    """Returns operational counters from the cases table and the audit log."""
    return cases.get_metrics(session)
