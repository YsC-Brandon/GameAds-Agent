"""Controlled real-thread schedules and real SQLite checkpoint contention."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.agent.contracts import AgentResponse
from app.phase3.store import BusinessError
from app.phase4.contracts import CREATE_ADAPTER
from tests.phase4.test_acceptance_state import context, create_budget, request
from tests.phase4.test_owner_application_integration import budget

__all__ = ["context"]


def test_delayed_post_after_lookup_404_cannot_authorize_second_execution(context):
    service, store = context
    waiting, accept = threading.Event(), threading.Event()
    identity = ("POST", "/api/v1/tasks", "delayed")
    body = CREATE_ADAPTER.validate_python({"kind": "budget_review", "request": budget()})

    def delayed():
        waiting.set()
        assert accept.wait(5)
        return service.execute(identity, body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(delayed)
        try:
            assert waiting.wait(5)
            with pytest.raises(BusinessError, match="not_found"):
                service.lookup(*identity)
            # The original request is still deliverable after that negative lookup.
            control = service.execute(identity, body)
            accept.set()
            assert pending.result(timeout=5) == control
        finally:
            accept.set()
    assert control[0] == 201
    with store.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


def test_approve_reject_race_has_one_terminal_decision(context):
    service, store = context
    task, proposal = create_budget(service)
    barrier = threading.Barrier(2)

    def act(action):
        barrier.wait(timeout=5)
        try:
            return action, service.execute(
                ("POST", f"/api/v1/tasks/{task['id']}/{action}", action),
                request(action, sha=proposal["proposal_sha256"]),
                task_id=task["id"],
                action=action,
            )
        except BusinessError as exc:
            return action, (exc.status, b"{}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(act, ("approve", "reject")))
    assert sorted(result[0] for _, result in results) == [200, 409]
    winner = next(action for action, result in results if result[0] == 200)
    current = service.get(task["id"])["data"]
    assert current["status"] == {"approve": "approved", "reject": "rejected"}[winner]
    assert current["version"] == 4 and current["active_operation_id"] is None
    with store.snapshot() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert (
            connection.execute(
                "SELECT count(*) FROM task_events WHERE to_status IN "
                "('approved','rejected') AND from_status='awaiting_approval'"
            ).fetchone()[0]
            == 1
        )


def test_real_wal_reader_blocks_checkpoint_without_losing_committed_result(context):
    service, store = context
    first, _ = create_budget(service)
    reader = sqlite3.connect(store.path, isolation_level=None)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        body = CREATE_ADAPTER.validate_python(
            {"kind": "report", "request": {"start_date": "2026-10-01", "end_date": "2026-10-01"}}
        )
        identity = ("POST", "/api/v1/tasks", "after-reader")
        result = service.execute(identity, body)
        assert result[0] == 201
        with pytest.raises(BusinessError, match="database_busy"):
            store.checkpoint()
        assert service.execute(identity, body) == result
        assert service.get(json.loads(result[1])["data"]["id"])["data"]["status"] == "completed"
        assert reader.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert service.get(first["id"])["data"] == first
    finally:
        reader.close()
    assert store.checkpoint() == (0, 0, 0)
    assert store.owner.active_connections == 0


def test_shutdown_drains_accepted_action_and_rejects_new_admission(context):
    service, store = context
    entered, release, closing = threading.Event(), threading.Event(), threading.Event()

    class Offline:
        calls = 0

        def run(self, body):
            self.calls += 1
            entered.set()
            assert release.wait(5)
            return AgentResponse(
                request_id="synthetic", status="unsupported", final_answer="Offline"
            )

    service.agent = Offline()
    body = CREATE_ADAPTER.validate_python({"kind": "analysis", "request": {"query": "synthetic"}})

    def shutdown():
        closing.set()
        store.owner.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = pool.submit(service.execute, ("POST", "/api/v1/tasks", "accepted"), body)
            assert entered.wait(5)
            stopped = pool.submit(shutdown)
            assert closing.wait(5)
            assert store.owner._closing.wait(5)
            with pytest.raises(BusinessError, match="database_busy"):
                service.execute(("POST", "/api/v1/tasks", "new-after-drain"), body)
            assert not stopped.done()
            release.set()
            result = accepted.result(timeout=5)
            assert result[0] == 422
            stopped.result(timeout=5)
        assert service.agent.calls == 1 and store.owner.active_connections == 0
    finally:
        release.set()
        if store.owner._handle is None:
            store.owner.acquire()
