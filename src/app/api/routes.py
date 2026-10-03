"""HTTP routes. Thin: validate input, call one service, return a schema."""

import dataclasses
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response

from app.api.deps import (
    AnalystDep,
    AnalystSessionDep,
    ClientAddressDep,
    CustomerDep,
    CustomerSessionDep,
    DemoDep,
    PrincipalDep,
    RuntimeDep,
    SessionDep,
)
from app.domain.history import Lang
from app.domain.recognition import ChargeDetail
from app.schemas.api import (
    AnalystLoginIn,
    AutomationIn,
    AutomationOut,
    AutonomyOut,
    CaseOut,
    ChargeOut,
    ChatIn,
    ChatOut,
    ClarificationOut,
    ClueOut,
    DecisionOut,
    DemoResetOut,
    DemoStateOut,
    HealthOut,
    HistoryEntryOut,
    HumanDecisionIn,
    InfoReplyIn,
    InfoReplyOut,
    MeOut,
    MetricsOut,
    MovementsOut,
    NotificationOut,
    NotificationsOut,
    OtpRequestIn,
    OtpRequestOut,
    OtpVerifyIn,
    ProductOut,
    QueueFilter,
    QueueOut,
    TokenOut,
    TraceEventOut,
)
from app.schemas.dossier import Dossier
from app.services import (
    auth,
    automation,
    autonomy,
    cases,
    decisions,
    demo,
    dossier,
    info_requests,
    me,
    notifications,
)
from app.services.agent import handle_message, pending_detail

router = APIRouter()

_ERRORS = {
    400: {"description": "A field the decision needs is missing or not allowed"},
    401: {"description": "Missing, invalid, expired or revoked session"},
    403: {"description": "The session has another role"},
    404: {"description": "Not found"},
    409: {"description": "Conflict with the current state"},
    422: {"description": "Invalid input"},
    429: {"description": "Document locked, or too many requests for it or from the address"},
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
def request_code(
    body: OtpRequestIn, runtime: RuntimeDep, address: ClientAddressDep
) -> OtpRequestOut:
    """Sends a one-time code; the answer is the same whether or not a customer has the document."""
    s = runtime.settings
    auth.limit_address(runtime.db, s, "otp_request", address, runtime.now())
    auth.request_code(runtime.db, s, body.document_type, body.document_number, runtime.now())
    return OtpRequestOut(expires_in_seconds=s.otp_ttl_minutes * 60)


@router.post(
    "/auth/otp/verify",
    response_model=TokenOut,
    responses={401: _ERRORS[401], 422: _ERRORS[422], 429: _ERRORS[429]},
)
def verify_code(body: OtpVerifyIn, runtime: RuntimeDep, address: ClientAddressDep) -> TokenOut:
    """Exchanges a valid one-time code for a customer session token."""
    auth.limit_address(runtime.db, runtime.settings, "otp_verify", address, runtime.now())
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
    responses={401: _ERRORS[401], 422: _ERRORS[422], 429: _ERRORS[429]},
)
def analyst_login(body: AnalystLoginIn, runtime: RuntimeDep, address: ClientAddressDep) -> TokenOut:
    """Exchanges the analyst test credentials for an analyst session token."""
    auth.limit_address(runtime.db, runtime.settings, "analyst_login", address, runtime.now())
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
    return me.list_clarifications(
        session,
        a.clock,
        a.passages,
        a.calendars,
        customer.subject,
        a.policy.config.queue.sla_hours,
    )


@router.get(
    "/me/notifications",
    response_model=NotificationsOut,
    responses={**_AUTH, 503: _ERRORS[503]},
)
def get_notifications(customer: CustomerDep, session: CustomerSessionDep) -> NotificationsOut:
    """Returns the session customer's notifications, newest first, with the unread count."""
    return notifications.list_notifications(session, customer.subject)


@router.post(
    "/me/notifications/{notification_id}/read",
    response_model=NotificationOut,
    responses={**_AUTH, 404: _ERRORS[404], 422: _ERRORS[422], 503: _ERRORS[503]},
)
def read_notification(
    notification_id: int, customer: CustomerDep, session: CustomerSessionDep
) -> NotificationOut:
    """Marks one of the session customer's notifications as read; idempotent."""
    return notifications.mark_read(session, customer.subject, notification_id)


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


@router.get(
    "/queue",
    response_model=QueueOut,
    responses={**_AUTH, 422: _ERRORS[422], 503: _ERRORS[503]},
)
def queue(
    session: AnalystSessionDep,
    runtime: RuntimeDep,
    filter: Annotated[QueueFilter | None, Query()] = None,
) -> QueueOut:
    """Returns the open rows of case_queue, most urgent first, with a counter per filter."""
    policy = runtime.agent.policy.config
    return cases.list_queue(
        session, policy.amount_usd.human_review_above, filter, policy.autonomy.audit_sample_rate
    )


@router.post(
    "/cases/{case_id}/decision",
    response_model=DecisionOut,
    responses={
        **_AUTH,
        400: _ERRORS[400],
        404: _ERRORS[404],
        409: _ERRORS[409],
        422: _ERRORS[422],
        503: _ERRORS[503],
    },
)
def human_decision(
    case_id: str,
    body: HumanDecisionIn,
    analyst: AnalystDep,
    session: AnalystSessionDep,
    runtime: RuntimeDep,
) -> DecisionOut:
    """Records an analyst decision: approve, reject with a reason, or ask the customer."""
    return decisions.record_decision(session, runtime.agent, analyst.subject, case_id, body)


@router.post(
    "/me/clarifications/{case_id}/reply",
    response_model=InfoReplyOut,
    responses={**_AUTH, 404: _ERRORS[404], 409: _ERRORS[409], 422: _ERRORS[422], 503: _ERRORS[503]},
)
def reply_to_analyst(
    case_id: str,
    body: InfoReplyIn,
    customer: CustomerDep,
    session: CustomerSessionDep,
    runtime: RuntimeDep,
) -> InfoReplyOut:
    """Stores the customer's answer to the analyst's question and puts the case back in queue."""
    sla = runtime.agent.policy.config.queue.sla_hours
    return info_requests.reply(session, customer.subject, case_id, body.text, dict(sla))


@router.get("/automation", response_model=AutomationOut, responses={**_AUTH, 503: _ERRORS[503]})
def get_automation(session: AnalystSessionDep) -> AutomationOut:
    """Returns the global automation switch."""
    return automation.get_switch(session)


@router.put(
    "/automation",
    response_model=AutomationOut,
    responses={**_AUTH, 422: _ERRORS[422], 503: _ERRORS[503]},
)
def put_automation(
    body: AutomationIn, analyst: AnalystDep, session: AnalystSessionDep
) -> AutomationOut:
    """Turns the global automation switch on or off; sending the current value changes nothing."""
    return automation.set_switch(session, analyst.subject, body.all_to_human)


@router.get("/autonomy", response_model=AutonomyOut, responses={**_AUTH, 503: _ERRORS[503]})
def get_autonomy(session: AnalystSessionDep, runtime: RuntimeDep) -> AutonomyOut:
    """Returns the autonomy of every cell with its thresholds and reversed cases (TRZ-31)."""
    policy = runtime.agent.policy.config
    return autonomy.autonomy_status(
        session, policy.autonomy, policy.dispute_intents, runtime.settings.demo_mode
    )


@router.get("/metrics", response_model=MetricsOut, responses={**_AUTH, 503: _ERRORS[503]})
def metrics(session: AnalystSessionDep, runtime: RuntimeDep) -> MetricsOut:
    """Returns containment, handoffs, latency, cost and the autonomy cells, from the audit log."""
    return cases.get_metrics(session, runtime.agent.policy.config.autonomy.initial_level)


@router.get("/demo", response_model=DemoStateOut, responses={**_AUTH, 404: _ERRORS[404]})
def demo_state(demo_config: DemoDep, session: AnalystSessionDep) -> DemoStateOut:
    """Says whether this database can be reset; the route exists only in demo mode."""
    return DemoStateOut(seeded=demo.is_seeded(session), demo_version=demo_config.version)


@router.post(
    "/demo/reset",
    response_model=DemoResetOut,
    responses={**_AUTH, 404: _ERRORS[404], 409: _ERRORS[409], 422: _ERRORS[422], 503: _ERRORS[503]},
)
def demo_reset(
    demo_config: DemoDep,
    analyst: AnalystDep,
    runtime: RuntimeDep,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> DemoResetOut:
    """Brings the demo back to its seeded state; the route exists only in demo mode (TRZ-38)."""
    return demo.reset(
        runtime.db,
        demo.offline(runtime.agent, runtime.settings),
        demo_config,
        analyst.subject,
        idempotency_key,
    )
