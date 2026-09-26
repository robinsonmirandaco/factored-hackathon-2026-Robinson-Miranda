"""Pipeline settings, read from environment variables and an optional .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class PipelineSettings(BaseSettings):
    """Knobs of the data pipeline. Field names match .env.example.

    AWS credentials never appear here: boto3 reads them from the named local profile.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Path("data")
    seed: int = 42
    aws_profile: str = "factored"
    aws_region: str = "us-east-2"
    s3_bucket: str = "factored-datathon-2026-s3-157725502942-us-east-2-an"
    s3_prefix: str = "data/"
    normalization_path: Path = Path("config/normalization.yaml")
    log_level: str = "INFO"
