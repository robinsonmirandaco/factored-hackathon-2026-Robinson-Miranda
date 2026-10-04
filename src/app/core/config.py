"""Application settings, read from environment variables and an optional .env file."""

from datetime import datetime
from pathlib import Path

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
    # Static customer and analyst web, served by the API from the same origin (TRZ-34).
    web_dir: Path = Path("web")

    # Required: postgresql+psycopg://trazo_app:... There is no default, so a missing value fails
    # at startup. The API connects only as trazo_app, the role row level security applies to.
    database_url: str
    # Owner of the schema. Only migrations, seeds and test fixtures use it, never the API.
    admin_database_url: str | None = None
    # Key of the HMAC that replaces identity document numbers; required to load customers.
    document_hash_key: str = ""

    # Claude through the official SDK. Without a key, every turn uses the deterministic fallback.
    anthropic_api_key: str = ""
    llm_model_primary: str = "claude-haiku-4-5-20251001"
    # Deadline of each attempt, and retries per customer turn across all LLM calls (TRZ-36).
    llm_timeout_seconds: float = 5.0
    llm_max_retries: int = 1
    llm_retry_wait_seconds: float = 0.5
    # Threads that run LLM calls, and how long a call may wait for one before the turn falls back
    # to the rules; the deadline of an attempt starts when a thread runs it.
    llm_pool_size: int = 8
    llm_queue_wait_seconds: float = 5.0
    # One comprehension call at startup, in the background, so the first turn is not cold.
    llm_warm_up: bool = True
    # False forces the deterministic fallback path (CI and golden cases).
    llm_enabled: bool = True
    # Versioned prompt of the comprehension step (TRZ-12); its `version` goes into every call log.
    llm_comprehension_prompt_path: Path = Path("config/prompts/comprehension.yaml")
    # USD per million tokens of the primary model, for the cost of each call (Haiku 4.5 list price).
    llm_price_input_per_mtok: float = 1.0
    llm_price_output_per_mtok: float = 5.0
    # False turns the fact checker of replies into an observer (ablation, TRZ-20 CA6): it still
    # counts unsupported claims in the audit log, but the LLM reply reaches the customer.
    fact_check_enabled: bool = True

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
    # Requests per client address to each login endpoint in each window (TRZ-40).
    ip_request_limit: int = 30
    ip_request_window_minutes: int = 15
    # Header the edge proxy sets with the client address: empty reads the socket address
    # (local, CI); x-real-ip behind the edge of Railway, which passes X-Forwarded-For through as
    # the client wrote it.
    client_ip_header: str = ""
    # The fixed demo code works only with demo_mode; otherwise every code is random.
    demo_mode: bool = False
    demo_otp_code: str = "482913"
    # Test credentials of the demo analyst; an empty password lets no analyst in.
    analyst_demo_user: str = "analista.demo"
    analyst_demo_password: str = ""
    # Who the demo people are and which cases a reset creates (TRZ-38); read only in demo_mode.
    demo_config_path: Path = Path("config/demo.yaml")

    # Transactional email (TRZ-33), off by default. When on, every notification also goes by
    # email, and only ever to the test inbox, which must be in the allowlist (comma separated).
    # The Resend key is a secret of the environment.
    email_enabled: bool = False
    email_test_inbox: str = ""
    email_allowlist: str = ""
    email_from: str = "onboarding@resend.dev"
    resend_api_key: str = ""
    email_timeout_seconds: float = 5.0
    # Attempts per email, and minutes to wait after each failed one but the last.
    email_max_attempts: int = 5
    email_retry_minutes: list[int] = [1, 2, 4, 8]

    # Days the redacted conversation text is kept before the purge replaces it (TRZ-41).
    conversation_retention_days: int = 90

    policy_path: str = "config/policy.yaml"
    # Demo policy passages and bank holidays: the response deadline of a dispute (TRZ-21).
    policy_passages_path: Path = Path("config/policy_passages.yaml")
    holidays_path: Path = Path("config/holidays.yaml")
    # Fitted weights, temperature and q-hat of the identification (TRZ-15), per comprehension.
    identification_path: Path = Path("config/identification.yaml")

    log_level: str = "INFO"
