"""Pipeline settings, read from environment variables and an optional .env file."""

from datetime import datetime
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PipelineSettings(BaseSettings):
    """Knobs of the data pipeline. Field names match .env.example.

    AWS credentials never appear here: boto3 reads them from the named local profile.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Path("data")
    seed: int = 42
    # Simulated clock (TRZ-24); the quality report counts events that fall after it.
    trazo_now: datetime = datetime(2026, 6, 17, 23, 59)
    aws_profile: str = "factored"
    aws_region: str = "us-east-2"
    s3_bucket: str = "factored-datathon-2026-s3-157725502942-us-east-2-an"
    s3_prefix: str = "data/"
    # Earlier copy of the data in the same bucket, compared by make diff-backup (TRZ-08).
    s3_backup_prefix: str = "data_backup_20260831/"
    normalization_path: Path = Path("config/normalization.yaml")
    quality_report_path: Path = Path("docs/reports/calidad.md")
    demand_report_path: Path = Path("docs/reports/demanda.md")
    impact_report_path: Path = Path("docs/reports/impacto.md")
    versions_report_path: Path = Path("docs/reports/diferencias_versiones.md")
    # Serving cohort (TRZ-07): 5,000 customers, the size TRZ-01 kept (plan A).
    cohort_size: int = Field(default=5000, gt=0)
    cohort_report_path: Path = Path("docs/reports/cohorte.md")
    # Partitions of the last N days before the latest one are read again on every run.
    pipeline_reprocess_days: int = Field(default=7, ge=0)
    # Evaluation cases (TRZ-42): mix, periods and noise, and the versioned split hashes.
    cases_config_path: Path = Path("config/cases.yaml")
    cases_manifest_path: Path = Path("eval/splits/manifest.json")
    log_level: str = "INFO"
