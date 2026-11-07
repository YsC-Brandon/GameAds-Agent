"""Configuration acceptance checks that require neither a model nor a network."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import PROJECT_ROOT, Settings


@pytest.fixture(autouse=True)
def clean_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.upper().startswith("GAMEADS_"):
            monkeypatch.delenv(name)


def test_example_loads_without_model_credentials() -> None:
    settings = Settings(_env_file=PROJECT_ROOT / ".env.example")
    assert settings.env == "development"
    assert settings.llm_api_key.get_secret_value() == ""


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("GAMEADS_ENV=test\nGAMEADS_LOG_LEVEL=WARNING\n", encoding="utf-8")
    monkeypatch.setenv("GAMEADS_LOG_LEVEL", "ERROR")
    settings = Settings(_env_file=dotenv)
    assert settings.env == "test"
    assert settings.log_level == "ERROR"


def test_secret_is_masked_in_representation(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "synthetic-test-secret"
    monkeypatch.setenv("GAMEADS_LLM_API_KEY", secret)
    settings = Settings(_env_file=None)
    assert secret not in repr(settings)
    assert secret not in settings.model_dump_json()


def test_invalid_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_base_url="not-a-url")


@pytest.mark.parametrize("timeout", [0, -1, 301, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_timeout_seconds=timeout)


def test_cli_works_outside_project_without_dotenv(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "configuration loaded successfully" in result.stdout


def test_cli_validation_error_does_not_expose_input(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "synthetic-invalid-config-value"
    monkeypatch.setenv("GAMEADS_LLM_BASE_URL", secret)
    result = subprocess.run(
        [sys.executable, "-m", "app"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "Configuration check failed" in result.stdout
    assert secret not in result.stdout + result.stderr
