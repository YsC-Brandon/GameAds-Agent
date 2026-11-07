"""Validated local configuration; loading settings has no external side effects."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AnomalyThresholds(BaseModel):
    """Illustrative screening rules, not calibrated business recommendations."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always"
    )

    ctr_drop_fraction: float = Field(default=0.30, ge=0, le=1)
    cpa_increase_fraction: float = Field(default=0.30, ge=0)
    roas_floor: float = Field(default=1.0, ge=0)
    min_impressions: int = Field(default=1000, ge=1)
    min_clicks: int = Field(default=30, ge=1)
    min_conversions: int = Field(default=5, ge=1)


class Settings(BaseSettings):
    """Read process environment first, then the project-root UTF-8 .env file."""

    model_config = SettingsConfigDict(
        env_prefix="GAMEADS_",
        env_nested_delimiter="__",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    llm_base_url: HttpUrl = HttpUrl("https://api.openai.com/v1")
    llm_model: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: float = Field(default=30.0, gt=0, le=300, allow_inf_nan=False)
    agent_max_rounds: int = Field(default=6, ge=1, le=12)
    agent_max_tool_calls: int = Field(default=12, ge=1, le=24)
    agent_max_context_bytes: int = Field(default=500_000, ge=10_000, le=2_000_000)
    anomaly: AnomalyThresholds = Field(default_factory=AnomalyThresholds)
