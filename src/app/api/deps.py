"""FastAPI dependencies. Every collaborator is built once per app and injected with Depends."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.adapters.db.session import Database
from app.core.config import Settings
from app.services.agent import AgentDeps


@dataclass(frozen=True)
class Runtime:
    """Everything a running app owns, stored on `app.state.runtime`.

    Attributes:
        settings: Application settings.
        db: Database engine and session factory.
        agent: Policy, LLM and scorer used by the agent.
    """

    settings: Settings
    db: Database
    agent: AgentDeps


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


def get_session(runtime: RuntimeDep) -> Iterator[Session]:
    """Opens one database session per request; commits on success, rolls back on error.

    Args:
        runtime: App runtime.

    Yields:
        An open session.
    """
    with runtime.db.session() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
