"""Application factory. Run with: uvicorn app.main:create_app --factory"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import yaml
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
from app.domain.identification import MAX_OPTIONS, Params, load_params
from app.domain.policy import PolicyEngine, PolicyError, initial_autonomy
from app.services.agent import AgentDeps
from app.services.auth import check_secrets

log = get_logger("api")


def load_identification(settings: Settings, policy: PolicyEngine) -> dict[str, Params]:
    """Loads the identification parameters and checks they fit the policy.

    q-hat was computed at one alpha and the decision table uses one option limit; a policy that
    says otherwise would promise a coverage the parameters do not give.

    Args:
        settings: Application settings.
        policy: The loaded policy.

    Returns:
        Parameters by comprehension, rules and llm.

    Raises:
        PolicyError: If the policy's alpha or option limit differ from the identification's.
    """
    with open(settings.identification_path, encoding="utf-8") as f:
        fitted_alpha = float(yaml.safe_load(f)["alpha"])
    conformal = policy.config.conformal
    if conformal.alpha != fitted_alpha:
        raise PolicyError(
            f"policy conformal.alpha {conformal.alpha} differs from the {fitted_alpha} "
            f"{settings.identification_path} was fitted with"
        )
    if conformal.max_options_shown != MAX_OPTIONS:
        raise PolicyError(
            f"policy conformal.max_options_shown {conformal.max_options_shown} differs from "
            f"the identification decision table ({MAX_OPTIONS})"
        )
    return {name: load_params(settings.identification_path, name) for name in ("rules", "llm")}


def build_runtime(settings: Settings) -> Runtime:
    """Builds every long-lived collaborator from settings.

    An invalid policy file stops here, before the service accepts any request.

    Args:
        settings: Application settings.

    Returns:
        The runtime: database, policy, LLM client, simulated clock and identification.

    Raises:
        PolicyError: If config/policy.yaml is invalid or does not fit the identification.
        ValueError: If JWT_SECRET is missing or too short, or DOCUMENT_HASH_KEY is empty.
    """
    check_secrets(settings)
    policy = PolicyEngine.from_file(settings.policy_path)
    return Runtime(
        settings=settings,
        db=Database(settings.database_url),
        agent=AgentDeps(
            policy=policy,
            llm=LLMClient(settings),
            clock=SimulatedClock(settings.trazo_now),
            identification=load_identification(settings, policy),
            autonomy=initial_autonomy(policy.config),
        ),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Creates a fully wired FastAPI application.

    Args:
        settings: Settings to use; read from the environment when omitted.

    Returns:
        The application. On startup it refuses to run if its database role could bypass row
        level security; connections are closed on shutdown.
    """
    settings = settings or Settings()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            runtime.db.assert_unprivileged()
        except Exception:
            runtime.db.dispose()
            raise
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
