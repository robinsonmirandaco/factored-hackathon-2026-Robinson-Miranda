"""FastAPI dependencies. Every collaborator is built once per app and injected with Depends."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.adapters.db.session import Database
from app.core.config import Settings
from app.core.errors import AppError
from app.core.time import utcnow
from app.domain.demo import DemoConfig
from app.services import auth
from app.services.agent import AgentDeps


@dataclass(frozen=True)
class Runtime:
    """Everything a running app owns, stored on `app.state.runtime`.

    Attributes:
        settings: Application settings.
        db: Database engine and session factory.
        agent: Policy and LLM used by the agent.
        now: Real clock of sessions and codes, naive UTC; tests replace it to move time.
        demo: config/demo.yaml in demo mode; None otherwise, and the demo routes do not exist.
    """

    settings: Settings
    db: Database
    agent: AgentDeps
    now: Callable[[], datetime] = utcnow
    demo: DemoConfig | None = None


def get_runtime(request: Request) -> Runtime:
    """Returns the runtime of the app serving the request.

    Args:
        request: Current request.

    Returns:
        The app runtime.
    """
    runtime: Runtime = request.app.state.runtime
    return runtime


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]


def get_client_address(request: Request, runtime: RuntimeDep) -> str:
    """Returns the address of the client, as the edge proxy reports it (TRZ-40).

    With CLIENT_IP_HEADER set, the address is the value of that header, which the edge sets on
    every request (X-Real-IP on Railway). X-Forwarded-For is never read: Railway passes it
    through as the client wrote it. Without the header the address is "unknown", one count for
    all such requests, never the socket, which behind the edge is an internal address that
    changes per connection. Without CLIENT_IP_HEADER (local, CI) it is the socket address.

    Args:
        request: Current request.
        runtime: App runtime.

    Returns:
        The client address, or "unknown".
    """
    header = runtime.settings.client_ip_header
    if header:
        return request.headers.get(header, "").strip() or "unknown"
    return request.client.host if request.client else "unknown"


ClientAddressDep = Annotated[str, Depends(get_client_address)]


def get_demo(runtime: RuntimeDep) -> DemoConfig:
    """Returns the demo configuration, or answers 404 outside demo mode (TRZ-38 CA2).

    Declared before the session dependencies of a route, so outside demo mode the route does not
    exist for anyone, signed in or not: the answer is the one of an unknown path.

    Args:
        runtime: App runtime.

    Returns:
        config/demo.yaml.

    Raises:
        AppError: 404 http_404 without DEMO_MODE.
    """
    if runtime.demo is None:
        raise AppError("http_404", "Not Found", 404)
    return runtime.demo


DemoDep = Annotated[DemoConfig, Depends(get_demo)]


def get_session(runtime: RuntimeDep) -> Iterator[Session]:
    """Opens one database session per request; commits on success, rolls back on error.

    The session has no row level security context, so it sees no customer rows. Only routes
    that read no customer data use it.

    Args:
        runtime: App runtime.

    Yields:
        An open session.
    """
    with runtime.db.session() as session:
        session.info["policy_version"] = runtime.agent.policy.version
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

_bearer = HTTPBearer(auto_error=False)


def get_principal(
    runtime: RuntimeDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> auth.Principal:
    """Returns the caller of a valid, live session.

    Args:
        runtime: App runtime.
        credentials: Bearer token of the Authorization header, if any.

    Returns:
        The caller.

    Raises:
        AppError: 401 without a token, or with an invalid, expired or revoked one.
    """
    if credentials is None:
        raise AppError("not_authenticated", "A bearer session token is required.", 401)
    return auth.authenticate(runtime.db, runtime.settings, credentials.credentials, runtime.now())


PrincipalDep = Annotated[auth.Principal, Depends(get_principal)]


def _require(principal: auth.Principal, role: auth.Role) -> auth.Principal:
    if principal.role != role:
        raise AppError("forbidden", "This session cannot use this endpoint.", 403)
    return principal


def get_customer(principal: PrincipalDep) -> auth.Principal:
    """Returns the caller if it is a customer.

    Args:
        principal: The caller.

    Returns:
        The customer.

    Raises:
        AppError: 403 for an analyst session.
    """
    return _require(principal, "customer")


def get_analyst(principal: PrincipalDep) -> auth.Principal:
    """Returns the caller if it is an analyst.

    Args:
        principal: The caller.

    Returns:
        The analyst.

    Raises:
        AppError: 403 for a customer session.
    """
    return _require(principal, "analyst")


CustomerDep = Annotated[auth.Principal, Depends(get_customer)]
AnalystDep = Annotated[auth.Principal, Depends(get_analyst)]


def get_customer_session(runtime: RuntimeDep, customer: CustomerDep) -> Iterator[Session]:
    """Opens one database session per request that sees only the rows of the session customer.

    The customer comes from the token and nowhere else (CA6).

    Args:
        runtime: App runtime.
        customer: The customer of the session.

    Yields:
        An open session.
    """
    with runtime.db.session(customer_id=customer.subject) as session:
        session.info["policy_version"] = runtime.agent.policy.version
        yield session


def get_analyst_session(runtime: RuntimeDep, _analyst: AnalystDep) -> Iterator[Session]:
    """Opens one database session per request with the analyst role, which sees every customer.

    Args:
        runtime: App runtime.
        _analyst: The analyst of the session; its presence is the check.

    Yields:
        An open session.
    """
    with runtime.db.session(role="analyst") as session:
        session.info["policy_version"] = runtime.agent.policy.version
        yield session


CustomerSessionDep = Annotated[Session, Depends(get_customer_session)]
AnalystSessionDep = Annotated[Session, Depends(get_analyst_session)]
