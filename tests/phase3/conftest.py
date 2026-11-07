"""Isolated real local business services; no real keys, databases, or report directories."""

import socket

import pytest

from app.phase3.contracts import BudgetRequest
from app.phase3.reports import ReportService
from app.phase3.service import BusinessService
from app.phase3.store import Store


@pytest.fixture(autouse=True)
def prohibit_external_connections(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("Phase 3 regression must not contact external hosts.")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def business(csv_factory, record, tmp_path):
    path = csv_factory(
        [record, {**record, "campaign_id": "B", "campaign_name": "Synthetic B", "revenue": "50.00"}]
    )
    return BusinessService(Store(tmp_path / "state.sqlite3"), data_path=path)


@pytest.fixture
def budget_request():
    return BudgetRequest(
        start_date="2026-10-01",
        end_date="2026-10-01",
        budget_period={"start_date": "2026-11-01", "end_date": "2026-11-07"},
        campaigns=[
            {
                "campaign_id": name,
                "current_budget": "100.00",
                "min_budget": "0.00",
                "max_budget": "200.00",
            }
            for name in ("A", "B")
        ],
    )


@pytest.fixture
def reports(business, tmp_path):
    return ReportService(business, tmp_path / "reports")
