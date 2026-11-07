"""All continuation state/action pairs, failure stages and owner-safe SQLite reads."""

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.agent.contracts import AgentResponse
from app.phase3.store import BusinessError, digest
from app.phase4.contracts import (
    CREATE_ADAPTER,
    TaskApproval,
    TaskPublish,
    TaskRejection,
    TaskSimulation,
)
from app.phase4.ownership import CandidateOwner
from app.phase4.service import TaskService
from app.phase4.store import CandidateStore
from tests.phase4.test_owner_application_integration import budget

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows candidate state matrix")


@pytest.fixture
def context(tmp_path, csv_factory, record):
    with CandidateOwner(tmp_path / "candidate.sqlite3") as owner:
        store = CandidateStore(owner)
        store.initialize_empty_candidate()
        service = TaskService(
            store, data_path=csv_factory([record]), reports_dir=tmp_path / "reports", agent=None
        )
        yield service, store


def create_budget(service):
    status, body = service.execute(
        ("POST", "/api/v1/tasks", "create"),
        CREATE_ADAPTER.validate_python({"kind": "budget_review", "request": budget()}),
    )
    assert status == 201
    task = json.loads(body)["data"]
    proposal = service.business.get_budget_proposal(task["proposal_id"])
    return task, proposal


def request(action, version=3, proposal_version=1, sha="a" * 64):
    review = {
        "expected_task_version": version,
        "expected_version": proposal_version,
        "proposal_sha256": sha,
    }
    return {
        "approve": lambda: TaskApproval(**review, confirmed=True, approver="synthetic"),
        "reject": lambda: TaskRejection(
            **review, confirmed=True, approver="synthetic", reason="no"
        ),
        "simulate": lambda: TaskSimulation(**review),
        "report": lambda: TaskPublish(expected_task_version=version),
    }[action]()


STATES = (
    "submitted",
    "running",
    "awaiting_approval",
    "approved",
    "simulated",
    "completed",
    "rejected",
    "failed",
    "needs_clarification",
    "unsupported",
    "insufficient_evidence",
    "interrupted",
)
ALLOWED = {
    ("awaiting_approval", "approve"),
    ("awaiting_approval", "reject"),
    ("approved", "simulate"),
    ("simulated", "report"),
}
DENIED = [
    (state, action)
    for state in STATES
    for action in ("approve", "reject", "simulate", "report")
    if (state, action) not in ALLOWED
]


@pytest.mark.parametrize("state,action", DENIED)
def test_all_denied_state_action_pairs_preserve_business_state(context, state, action):
    service, store = context
    operation, _ = store.reserve("POST", b"/api/v1/tasks", b"fixture", digest({}))
    kind = (
        "analysis"
        if state in {"needs_clarification", "unsupported", "insufficient_evidence"}
        else "budget_review"
    )
    task_id = store.create_submitted(
        operation["operation_id"], kind, validated_request=None if kind == "analysis" else {}
    )
    # Boundary fixture only: legal transitions are covered through the real service.
    with store.unit_of_work(operation["operation_id"]) as connection:
        connection.execute("UPDATE tasks SET status=?,version=3 WHERE id=?", (state, task_id))
        store.seal(connection, operation["operation_id"], 201, b"{}")
    before = service.get(task_id)
    identity = ("POST", f"/api/v1/tasks/{task_id}/{action}", "denied")
    result = service.execute(identity, request(action), task_id=task_id, action=action)
    assert result[0] == 409 and json.loads(result[1])["code"] == "invalid_task_transition"
    assert service.get(task_id) == before
    with store.snapshot() as connection:
        count = connection.execute("SELECT count(*) FROM task_events").fetchone()[0]
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert service.execute(identity, request(action), task_id=task_id, action=action) == result
    with store.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == count


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("version", 2, "stale_task_version"),
        ("proposal_version", 2, "stale_proposal_version"),
        ("sha", "b" * 64, "proposal_digest_mismatch"),
    ],
)
def test_failed_cas_and_digest_keep_awaiting_approval(context, field, value, code):
    service, store = context
    task, proposal = create_budget(service)
    options = {"sha": proposal["proposal_sha256"], field: value}
    body = request("approve", **options)
    result = service.execute(
        ("POST", f"/api/v1/tasks/{task['id']}/approve", "stale"),
        body,
        task_id=task["id"],
        action="approve",
    )
    assert result[0] == 409 and json.loads(result[1])["code"] == code
    assert service.get(task["id"])["data"] == task
    assert service.business.get_budget_proposal(proposal["id"]) == proposal


@pytest.mark.parametrize(
    "state,http,code",
    [
        ("needs_clarification", 422, "needs_clarification"),
        ("unsupported", 422, "unsupported"),
        ("insufficient_evidence", 422, "insufficient_evidence"),
        ("model_unavailable", 503, "model_not_configured"),
        ("failed", 502, "agent_failed"),
    ],
)
def test_analysis_outcomes_and_explicit_retry(context, state, http, code):
    service, store = context

    class Offline:
        calls = 0

        def run(self, body):
            self.calls += 1
            return AgentResponse(request_id="offline", status=state, final_answer="synthetic")

    service.agent = Offline()
    body = CREATE_ADAPTER.validate_python({"kind": "analysis", "request": {"query": "secret-test"}})
    identity = ("POST", "/api/v1/tasks", "outcome")
    result = service.execute(identity, body)
    assert result[0] == http and json.loads(result[1])["code"] == code
    task_id = json.loads(result[1])["task_id"]
    assert service.execute(identity, body) == result and service.agent.calls == 1
    retry = body.model_copy(update={"retry_of": task_id})
    second = service.execute(("POST", "/api/v1/tasks", "retry"), retry)
    second_id = json.loads(second[1])["task_id"]
    assert second_id != task_id
    assert service.get(second_id)["data"]["retry_of"] == task_id
    with store.snapshot() as connection:
        assert "secret-test" not in str(
            [tuple(row) for row in connection.execute("SELECT * FROM tasks")]
        )


def test_lookup_404_does_not_authorize_or_consume_reservation(context):
    service, store = context
    with pytest.raises(BusinessError, match="not_found"):
        service.lookup("POST", "/api/v1/tasks", "create")
    task, _ = create_budget(service)
    lookup = service.lookup("POST", "/api/v1/tasks", "create")["data"]
    assert lookup["task_id"] == task["id"] and lookup["phase"] == "completed"


def test_two_readers_then_concurrent_approve_have_one_winner(context):
    service, store = context
    task, proposal = create_budget(service)
    body = request("approve", sha=proposal["proposal_sha256"])

    def approve(key):
        try:
            return service.execute(
                ("POST", f"/api/v1/tasks/{task['id']}/approve", key),
                body,
                task_id=task["id"],
                action="approve",
            )[0]
        except BusinessError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, ("race-one", "race-two")))
    assert sorted(results) == [200, 409]
    assert service.get(task["id"])["data"]["version"] == 4
    with store.snapshot() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_real_sqlite_busy_rollback_and_wal_shm_cleanup(context):
    service, store = context
    with sqlite3.connect(store.path, isolation_level=None) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.reserve("POST", b"/api/v1/tasks", b"blocked", digest({}))
        blocker.rollback()
    blocker.close()
    assert store.lookup("POST", b"/api/v1/tasks", b"blocked") is None
    create_budget(service)
    assert store.checkpoint()[0] == 0
    assert store.owner.active_connections == 0
    # Business leases are drained; the owner retains its explicit WAL read
    # anchor until shutdown. Validate handle release at that actual boundary.
    store.owner.close()
    temporary = store.path.with_suffix(".probe")
    try:
        store.path.rename(temporary)
        temporary.rename(store.path)
    finally:
        store.owner.acquire()
