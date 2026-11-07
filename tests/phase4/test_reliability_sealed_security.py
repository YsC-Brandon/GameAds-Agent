"""Real TCP early-rejection controls for distinct persisted sealed outcomes."""

import json

import pytest

from app.agent.contracts import AgentResponse
from tests.phase4.test_acceptance_security import live, raw, snapshot
from tests.phase4.test_owner_application_integration import DATES, budget

__all__ = ["live"]


@pytest.mark.parametrize(
    "outcome", ["budget-success", "report-success", "unsupported", "model-failure"]
)
def test_sealed_outcome_never_bypasses_prevalidation(
    live, tmp_path, monkeypatch, outcome, caplog, record_property
):
    client, candidate, counters, port, _ = live
    if outcome in {"unsupported", "model-failure"}:
        status = "unsupported" if outcome == "unsupported" else "model_unavailable"
        monkeypatch.setattr(
            "app.agent.service.AgentService.run",
            lambda self, body: AgentResponse(
                request_id="synthetic", status=status, final_answer="Offline"
            ),
        )
        body = {"kind": "analysis", "request": {"query": "private-sealed-sentinel"}}
        expected = 422 if outcome == "unsupported" else 503
    else:
        kind = "budget_review" if outcome == "budget-success" else "report"
        body = {"kind": kind, "request": budget() if kind == "budget_review" else DATES}
        expected = 201
    response = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "sealed"})
    assert response.status_code == expected
    before = snapshot(candidate, tmp_path)
    host = (b"Host", f"127.0.0.1:{port}".encode())
    vectors = [
        (b"/api/v1/tasks/", [host, (b"Idempotency-Key", b"sealed")], b"{}", 422),
        (
            b"/api/v1/tasks",
            [host, (b"Idempotency-Key", b"sealed"), (b"Idempotency-Key", b"sealed")],
            b"{}",
            422,
        ),
        (
            b"/api/v1/tasks",
            [host, (b"Idempotency-Key", b"sealed"), (b"Content-Type", b"text/plain")],
            b"{}",
            415,
        ),
        (
            b"/api/v1/tasks",
            [host, (b"Idempotency-Key", b"sealed"), (b"Content-Type", b"application/json")],
            b"{",
            400,
        ),
        (
            b"/api/v1/tasks",
            [host, (b"Idempotency-Key", b"sealed"), (b"Origin", b"null")],
            b"{}",
            403,
        ),
    ]
    records = []
    for path, headers, payload, expected_code in vectors:
        counters.update(router=0, body=0)
        code, wire = raw(port, "POST", path, headers, payload)
        assert code == expected_code, wire
        if code != 400:  # JSON syntax is validated by FastAPI after safe routing.
            assert counters == {"router": 0, "body": 0}
        records.append({"path": path.decode(), "status": code, "dispatch": dict(counters)})
        assert snapshot(candidate, tmp_path) == before
    replay = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "sealed"})
    assert (replay.status_code, replay.content) == (response.status_code, response.content)
    assert snapshot(candidate, tmp_path) == before
    assert "private-sealed-sentinel" not in caplog.text + response.text
    record_property(
        "sealed_security_controls",
        json.dumps(
            {"outcome": outcome, "positive_status": response.status_code, "vectors": records}
        ),
    )
