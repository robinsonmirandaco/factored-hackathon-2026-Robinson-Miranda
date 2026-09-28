"""Application settings, read from environment variables and an optional .env file."""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every runtime knob of the service.

    Values come from environment variables (case-insensitive) or `.env`. Secrets such as the
    Anthropic key are only ever read from the environment. Field names match .env.example.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "bankagent"
    app_env: str = "local"  # local | ci | prod

    # The dataset ends on 2026-06-17, so every data window is anchored to this date, not today.
    trazo_now: datetime = datetime(2026, 6, 17, 23, 59)
    seed: int = 42
    data_dir: Path = Path("data")

    # Required: postgresql+psycopg://trazo_app:... There is no default, so a missing value fails
    # at startup. The API connects only as trazo_app, the role row level security applies to.
    database_url: str
    # Owner of the schema. Only migrations, seeds and test fixtures use it, never the API.
    admin_database_url: str | None = None
    # Key of the HMAC that replaces identity document numbers; required to load customers.
    document_hash_key: str = ""

    # anthropic: Claude through the official SDK. local: an OpenAI-compatible server such as
    # Docker Model Runner or Ollama, so the agent can run without a paid key.
    llm_provider: Literal["anthropic", "local"] = "anthropic"
    llm_base_url: str = ""
    anthropic_api_key: str = ""
    llm_model_primary: str = "claude-haiku-4-5-20251001"
    llm_timeout_seconds: float = 5.0
    llm_max_retries: int = 1
    # False forces the deterministic fallback path (CI and golden cases).
    llm_enabled: bool = True
    # Versioned prompt of the comprehension step (TRZ-12); its `version` goes into every call log.
    llm_comprehension_prompt_path: Path = Path("config/prompts/comprehension.yaml")
    # USD per million tokens of the primary model, for the cost of each call (Haiku 4.5 list price).
    llm_price_input_per_mtok: float = 1.0
    llm_price_output_per_mtok: float = 5.0

    # Sessions (TRZ-09). Every time here is the real clock, never trazo_now (design 5.1).
    # No usable default for the secret: the API refuses to start without a generated one.
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    session_idle_minutes: int = 15
    session_max_minutes: int = 120
    otp_ttl_minutes: int = 5
    otp_max_attempts: int = 3
    otp_lock_minutes: int = 15
    # Code requests allowed per document in each window.
    otp_request_limit: int = 5
    otp_request_window_minutes: int = 15
    # The fixed demo code works only with demo_mode; otherwise every code is random.
    demo_mode: bool = False
    demo_otp_code: str = "482913"
    # Test credentials of the demo analyst; an empty password lets no analyst in.
    analyst_demo_user: str = "analista.demo"
    analyst_demo_password: str = ""

    policy_path: str = "config/policy.yaml"
    # Fitted weights, temperature and q-hat of the identification (TRZ-15), per comprehension.
    identification_path: Path = Path("config/identification.yaml")

    log_level: str = "INFO"
