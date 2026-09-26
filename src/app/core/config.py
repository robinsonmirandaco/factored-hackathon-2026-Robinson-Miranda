"""Application settings, read from environment variables and an optional .env file."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every runtime knob of the service.

    Values come from environment variables (case-insensitive) or `.env`. Secrets such as the
    Anthropic key are only ever read from the environment.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "bankagent"
    env: str = "dev"

    # Postgres in compose, SQLite for local runs and tests.
    database_url: str = "sqlite:///./bankagent.db"

    # anthropic: Claude through the official SDK. local: an OpenAI-compatible server such as
    # Docker Model Runner or Ollama, so the agent can run without a paid key.
    llm_provider: Literal["anthropic", "local"] = "anthropic"
    llm_base_url: str = ""
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-4-5"
    llm_timeout_seconds: float = 5.0
    llm_max_retries: int = 1
    # False forces the deterministic fallback path (CI and golden cases).
    llm_enabled: bool = True

    scorer_backend: str = "random"  # random | logistic | xgboost
    model_path: str = "data/models/scorer.joblib"

    policy_path: str = "config/policy.yaml"

    log_level: str = "INFO"
