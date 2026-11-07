"""Cross-kind retries and per-action digest/CAS errors preserve state and exact seals."""

import json

import pytest

from app.phase4.contracts import CREATE_ADAPTER, TaskError
from tests.phase4.test_acceptance_state import context, create_budget, request
from tests.phase4.test_owner_application_integration import DATES, budget
from tests.phase4.test_reliability_faults import simulated

__all__ = ["context"]


@pytest.mark.parametrize("kind", ["analysis", "budget_review", "report"])
@pytest.mark.parametrize("previous", ["missing", "active", "different-kind"])
def test_retry_relationship_errors_create_no_task_or_business_documents(context, kind, previous):
    service, store = context
    if previous == "missing":
        prior = "f" * 32
    else:
        task, _ = create_budget(service)
        prior = task["id"]
        if previous == "different-kind":
            with store._transaction() as connection:
                connection.execute("UPDATE tasks SET status='failed' WHERE id=?", (prior,))
            if kind == "budget_review":
                # A completed report is a different-kind predecessor for budget.
                result = service.execute(
                    ("POST", "/api/v1/tasks", "prior-report"),
                    CREATE_ADAPTER.validate_python({"kind": "report", "request": DATES}),
                )
                assert result[0] == 201
                prior = json.loads(result[1])["data"]["id"]
    payload = {
        "analysis": {"query": "private retry sentinel"},
        "budget_review": budget(),
        "report": DATES,
    }[kind]
    body = CREATE_ADAPTER.validate_python({"kind": kind, "request": payload, "retry_of": prior})
    with store.snapshot() as connection:
        before = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("tasks", "documents", "task_documents", "task_events", "events")
        }
    identity = ("POST", "/api/v1/tasks", "retry-denied")
    result = service.execute(identity, body)
    assert result[0] == (404 if previous == "missing" else 409)
    error = TaskError.model_validate_json(result[1])
    assert error.task_id is None and not error.retryable
    assert error.code == ("not_found" if previous == "missing" else "invalid_retry_of")
    assert service.execute(identity, body) == result
    with store.snapshot() as connection:
        after = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in before
        }
        assert before == after
        row = connection.execute("SELECT * FROM operations WHERE key='retry-denied'").fetchone()
        assert row["response"] == result[1] and row["sealed_http_status"] == result[0]
        assert row["task_id"] is None and row["phase"] == "completed"


@pytest.mark.parametrize("action", ["approve", "reject", "simulate", "report"])
@pytest.mark.parametrize("fault", ["task-version", "proposal-version", "digest"])
def test_each_continuation_cas_error_has_consistent_seal_and_no_state_regression(
    context, action, fault
):
    service, store = context
    if action == "report":
        if fault != "task-version":
            # Report has no caller-supplied proposal version/digest. Exercise
            # independently stale task versions, not invented request fields.
            version = {"proposal-version": 4, "digest": 6}[fault]
        else:
            version = 1
        task_id = simulated(service)
        body = request(action, version)
        expected = "stale_task_version"
    else:
        task, proposal = create_budget(service)
        task_id = task["id"]
        version, proposal_version = 3, 1
        if action == "simulate":
            approved = service.execute(
                ("POST", f"/api/v1/tasks/{task_id}/approve", "approved"),
                request("approve", sha=proposal["proposal_sha256"]),
                task_id=task_id,
                action="approve",
            )
            assert approved[0] == 200
            version, proposal_version = 4, 2
        sha = proposal["proposal_sha256"]
        if fault == "task-version":
            version -= 1
        elif fault == "proposal-version":
            proposal_version += 1
        else:
            sha = "f" * 64
        body = request(action, version, proposal_version, sha)
        expected = {
            "task-version": "stale_task_version",
            "proposal-version": "stale_proposal_version",
            "digest": "proposal_digest_mismatch",
        }[fault]
    before = service.get(task_id)
    identity = ("POST", f"/api/v1/tasks/{task_id}/{action}", "denied")
    result = service.execute(identity, body, task_id=task_id, action=action)
    assert result[0] == 409 and json.loads(result[1])["code"] == expected
    assert service.get(task_id) == before
    assert service.execute(identity, body, task_id=task_id, action=action) == result
    with store.snapshot() as connection:
        row = connection.execute("SELECT * FROM operations WHERE key='denied'").fetchone()
        assert row["response"] == result[1] and row["sealed_http_status"] == 409
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
