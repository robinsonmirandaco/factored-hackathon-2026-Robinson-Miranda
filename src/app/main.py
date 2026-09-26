"""Application factory. Run with: uvicorn app.main:create_app --factory"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.db.session import Database
from app.adapters.llm import LLMClient
from app.api.deps import Runtime
from app.api.routes import router
from app.core.config import Settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import install_trace_middleware
from app.domain.clock import SimulatedClock
from app.domain.policy import PolicyEngine
from app.services.agent import AgentDeps

log = get_logger("api")


def build_runtime(settings: Settings) -> Runtime:
    """Builds every long-lived collaborator from settings.

    Args:
        settings: Application settings.

    Returns:
        The runtime: database, policy, LLM client and simulated clock.
    """
    return Runtime(
        settings=settings,
        db=Database(settings.database_url),
        agent=AgentDeps(
            policy=PolicyEngine.from_file(settings.policy_path),
            llm=LLMClient(settings),
            clock=SimulatedClock(settings.trazo_now),
        ),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Creates a fully wired FastAPI application.

    Args:
        settings: Settings to use; read from the environment when omitted.

    Returns:
        The application. Tables are created on startup and connections closed on shutdown.
    """
    settings = settings or Settings()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        runtime.db.create_all()
        log.info(
            "startup",
            app_env=settings.app_env,
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model_primary,
            llm_available=runtime.agent.llm.available,
        )
        yield
        runtime.db.dispose()
        log.info("shutdown")

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime
    register_error_handlers(app)
    install_trace_middleware(app)
    app.include_router(router)
    return app
