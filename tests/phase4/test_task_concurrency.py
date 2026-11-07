"""Deterministic real-thread task occupancy, sealing and retry proof."""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.agent.contracts import AgentResponse
from app.phase3.store import BusinessError
from app.phase4.contracts import CREATE_ADAPTER
from app.phase4.ownership import CandidateOwner
from app.phase4.service import TaskService
from app.phase4.store import CandidateStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ownership")


def test_lookup_visible_during_agent_call_and_same_key_never_runs_twice(tmp_path):
    entered, finish = threading.Event(), threading.Event()

    class OfflineAgent:
        calls = 0

        def run(self, request):
            self.calls += 1
            entered.set()
            assert finish.wait(5)
            return AgentResponse(
                request_id="synthetic", status="unsupported", final_answer="Offline"
            )

    with CandidateOwner(tmp_path / "candidate.sqlite3") as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        agent = OfflineAgent()
        service = TaskService(
            candidate,
            data_path=tmp_path / "unused.csv",
            reports_dir=tmp_path / "reports",
            agent=agent,
        )
        body = CREATE_ADAPTER.validate_python(
            {"kind": "analysis", "request": {"query": "private synthetic query"}}
        )
        identity = ("POST", "/api/v1/tasks", "same-key")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.execute, identity, body)
            assert entered.wait(5)
            lookup = service.lookup(*identity)["data"]
            assert lookup["phase"] == "in_progress"
            task = service.get(lookup["task_id"])["data"]
            assert task["status"] == "running"
            assert task["active_operation_id"] == lookup["operation_id"]
            status, raw = service.execute(identity, body)
            assert status == 409 and json.loads(raw)["code"] == "operation_in_progress"
            finish.set()
            result = first.result(timeout=5)
        assert result[0] == 422
        assert service.execute(identity, body) == result
        assert agent.calls == 1
        assert service.lookup(*identity)["data"]["phase"] == "completed"
        task = service.get(task["id"])["data"]
        assert task["status"] == "unsupported" and task["active_operation_id"] is None
        with candidate.snapshot() as connection:
            assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 3
            assert "private synthetic query" not in "".join(
                str(tuple(row)) for row in connection.execute("SELECT * FROM tasks")
            )
        changed = CREATE_ADAPTER.validate_python(
            {"kind": "analysis", "request": {"query": "changed"}}
        )
        with pytest.raises(BusinessError, match="idempotency_key_reused"):
            service.execute(identity, changed)
