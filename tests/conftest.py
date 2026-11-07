"""Small explicit datasets with independently calculable results."""

import csv
import os
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.services.campaign_service import CSV_COLUMNS
from tests.transport_support import InProcessTransport


@pytest.fixture(autouse=True)
def loopback_asgi_test_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy test clients use a real loopback identity, never a production exception."""
    original = TestClient.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        kwargs.setdefault("client", ("127.0.0.1", 50000))
        original(self, *args, **kwargs)
        # Keep client.app as the original FastAPI application so legacy tests
        # can still install dependency overrides and inspect application state.
        self._transport.app = InProcessTransport(self._transport.app)

    monkeypatch.setattr(TestClient, "__init__", init)


@pytest.fixture(autouse=True)
def historical_phase2_launch_adapter(monkeypatch, request):
    """Keep the frozen Phase 2 file/behavior assertions while retiring its command.

    Only that historical test is adapted. The new TCP launch tests execute
    default Uvicorn unaltered and assert its actual fail-closed behavior.
    """
    if request.node.path.name != "test_phase2_http_live.py":
        return
    original = subprocess.Popen

    def launch(args, *positional, **kwargs):
        if isinstance(args, list) and args[1:4] == ["-m", "uvicorn", "app.main:app"]:
            port = args[args.index("--port") + 1]
            args = [args[0], "-m", "app.server", "--port", port]
        return original(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)


@pytest.fixture(autouse=True)
def clean_gameads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.upper().startswith("GAMEADS_"):
            monkeypatch.delenv(name)


@pytest.fixture
def record() -> dict:
    return {
        "date": "2026-10-01",
        "campaign_id": "A",
        "campaign_name": "Synthetic test A",
        "platform": "DemoNetwork",
        "game_genre": "Puzzle",
        "impressions": 1000,
        "clicks": 100,
        "conversions": 10,
        "spend": "50.00",
        "revenue": "100.00",
        "currency": "USD",
    }


@pytest.fixture
def csv_factory(tmp_path: Path):
    def write(rows: list[dict], name: str = "campaigns.csv") -> Path:
        path = tmp_path / name
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        return path

    return write
