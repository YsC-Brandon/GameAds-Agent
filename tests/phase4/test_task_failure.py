"""SQLite rollback and report publication failure on an actual unified task."""

import json
import os

import pytest

from app.phase3.store import BusinessError
from app.phase4.contracts import CREATE_ADAPTER, TaskApproval, TaskPublish, TaskSimulation
from app.phase4.ownership import CandidateOwner
from app.phase4.service import TaskService
from app.phase4.store import CandidateStore
from tests.phase4.test_owner_application_integration import budget

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows task owner")


def test_report_write_failure_keeps_simulated_state_and_seals_failure(
    tmp_path, csv_factory, record, monkeypatch
):
    data = csv_factory([record])
    with CandidateOwner(tmp_path / "candidate.sqlite3") as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        service = TaskService(
            candidate, data_path=data, reports_dir=tmp_path / "reports", agent=None
        )
        status, raw = service.execute(
            ("POST", "/api/v1/tasks", "create"),
            CREATE_ADAPTER.validate_python({"kind": "budget_review", "request": budget()}),
        )
        assert status == 201
        task = json.loads(raw)["data"]
        proposal = service.business.get_budget_proposal(task["proposal_id"])
        base = "/api/v1/tasks/" + task["id"]
        review = {
            "expected_version": 1,
            "proposal_sha256": proposal["proposal_sha256"],
            "expected_task_version": 3,
            "confirmed": True,
            "approver": "synthetic",
        }
        assert (
            service.execute(
                ("POST", base + "/approve", "approve"),
                TaskApproval(**review),
                task_id=task["id"],
                action="approve",
            )[0]
            == 200
        )
        assert (
            service.execute(
                ("POST", base + "/simulate", "simulate"),
                TaskSimulation(
                    expected_version=2,
                    expected_task_version=4,
                    proposal_sha256=proposal["proposal_sha256"],
                ),
                task_id=task["id"],
                action="simulate",
            )[0]
            == 200
        )
        original = service.store.save

        def fail_report(connection, identity, kind, value, **options):
            if kind == "report":
                raise OSError("synthetic full disk at report registration")
            return original(connection, identity, kind, value, **options)

        monkeypatch.setattr(service.store, "save", fail_report)
        identity = ("POST", base + "/report", "report-failed")
        request = TaskPublish(expected_task_version=5)
        result = service.execute(identity, request, task_id=task["id"], action="report")
        assert result[0] == 500 and json.loads(result[1])["code"] == "report_write_failed"
        assert service.execute(identity, request, task_id=task["id"], action="report") == result
        current = service.get(task["id"])["data"]
        assert current["status"] == "simulated" and current["version"] == 5
        assert current["report_id"] is None and current["active_operation_id"] is None
        assert not list((tmp_path / "reports").glob("*.md"))
        assert not list((tmp_path / "reports").glob("*.tmp"))
        monkeypatch.setattr(service.store, "save", original)
        success = service.execute(
            ("POST", base + "/report", "report-new"), request, task_id=task["id"], action="report"
        )
        assert success[0] == 200
        with candidate.snapshot() as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert (
                connection.execute("SELECT count(*) FROM documents WHERE kind='report'").fetchone()[
                    0
                ]
                == 1
            )


def test_future_version_refused_before_tool_or_file_side_effect(tmp_path):
    import sqlite3

    from app.phase3.contracts import BudgetRequest
    from app.phase3.service import BusinessService
    from app.phase3.store import Store

    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version=2")
    connection.close()
    before = database.read_bytes()
    business = BusinessService(Store(database), data_path=tmp_path / "absent.csv")
    with pytest.raises(BusinessError, match="schema_upgrade_required"):
        business.propose_budget_reallocation(BudgetRequest.model_validate(budget()))
    assert database.read_bytes() == before
