"""Unified task E2E over real TCP and real SQLite, using synthetic data only."""

import json
import os
import subprocess
import sys

import pytest

from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
from tests.phase4.test_owner_application_integration import DATES, budget, server

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows task ownership")


def test_real_agent_graph_offline_analysis_task_persists_structured_evidence(
    tmp_path, csv_factory, record
):
    data = csv_factory([record])
    database = tmp_path / "candidate.sqlite3"
    with CandidateOwner(database) as owner:
        CandidateStore(owner).initialize_empty_candidate()
    with server(tmp_path, database, data, bound=True, offline_analysis=True) as client:
        response = client.post(
            "/api/v1/tasks",
            json={
                "kind": "analysis",
                "request": {"query": "private-synthetic: summarize 2026-10-01 to 2026-10-01"},
            },
            headers={"Idempotency-Key": "offline-agent"},
        )
        assert response.status_code == 201, response.text
        task = response.json()["data"]
        assert task["status"] == "completed"
        result = task["agent_result"]
        summary = next(
            call
            for call in result["tool_calls"]
            if call["tool_name"] == "summarize_campaign_performance"
        )
        assert result["tool_results"][summary["call_id"]]["overall"]["campaign_count"] == 1
        assert task["agent_result"]["evidence"]
        assert task["agent_result"]["status"] == "completed"
        assert client.get("/api/v1/tasks/" + task["id"]).json()["data"] == task
        assert "private-synthetic" not in response.text


def test_tcp_unified_budget_task_to_evidence_and_restart(tmp_path, csv_factory, record):
    data = csv_factory([record])
    database = tmp_path / "candidate.sqlite3"
    with CandidateOwner(database) as owner:
        CandidateStore(owner).initialize_empty_candidate()
    request = {"kind": "budget_review", "request": budget()}
    with server(tmp_path, database, data, bound=True) as client:

        def cli(*arguments, stdin=None):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.task_cli",
                    "--base-url",
                    str(client.base_url),
                    *arguments,
                ],
                input=stdin,
                capture_output=True,
                text=True,
                cwd=tmp_path,
                timeout=15,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert not result.stderr
            return json.loads(result.stdout)

        created = client.post("/api/v1/tasks", json=request, headers={"Idempotency-Key": "create"})
        assert created.status_code == 201, created.text
        task = created.json()["data"]
        assert task["status"] == "awaiting_approval" and task["version"] == 3
        assert task["active_operation_id"] is None
        assert cli("get", task["id"])["data"] == task
        assert cli("list")["items"][0] == task
        task_id = task["id"]
        base = "/api/v1/tasks/" + task_id
        replay = client.post("/api/v1/tasks", json=request, headers={"Idempotency-Key": "create"})
        assert replay.content == created.content
        operation = client.get(
            "/api/v1/tasks/operations/lookup",
            params={"method": "POST", "path": "/api/v1/tasks"},
            headers={"Idempotency-Key": "create"},
        )
        assert operation.status_code == 200
        assert operation.json()["data"]["phase"] == "completed"
        assert operation.headers["cache-control"] == "no-store"
        assert (
            cli(
                "operation-get",
                "--method",
                "POST",
                "--path",
                "/api/v1/tasks",
                "--idempotency-key",
                "create",
            )["data"]
            == operation.json()["data"]
        )
        proposal = client.get("/api/v1/budget-proposals/" + task["proposal_id"]).json()["data"]
        review = {
            "expected_version": 1,
            "proposal_sha256": proposal["proposal_sha256"],
            "expected_task_version": 3,
            "confirmed": True,
            "approver": "synthetic-reviewer",
        }
        bypass = client.post(
            "/api/v1/budget-proposals/" + task["proposal_id"] + "/approve",
            json={k: v for k, v in review.items() if k != "expected_task_version"},
        )
        assert bypass.status_code == 409 and bypass.json()["code"] == "task_managed_resource"
        approved = client.post(
            base + "/approve", json=review, headers={"Idempotency-Key": "approve"}
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["data"]["version"] == 4
        simulated = client.post(
            base + "/simulate",
            json={
                "expected_task_version": 4,
                "expected_version": 2,
                "proposal_sha256": proposal["proposal_sha256"],
            },
            headers={"Idempotency-Key": "simulate"},
        )
        assert simulated.status_code == 200, simulated.text
        assert simulated.json()["data"]["version"] == 5
        published = client.post(
            base + "/report",
            json={"expected_task_version": 5},
            headers={"Idempotency-Key": "report"},
        )
        assert published.status_code == 200, published.text
        completed = published.json()["data"]
        assert completed["status"] == "completed" and completed["version"] == 6
        assert client.get(base).json()["data"] == completed
        events = client.get(base + "/events").json()["items"]
        assert [r["action"] for r in events] == [
            "submitted",
            "running",
            "awaiting_approval",
            "approved",
            "simulated",
            "report_available",
            "completed",
        ]
        assert [r["seq"] for r in events] == list(range(1, 8))
        index = client.get(base + "/reports").json()["items"]
        assert index[0]["id"] == completed["report_id"]
        report = client.get("/api/v1/reports/" + completed["report_id"]).json()["data"]
        assert report["proposal"]["version"] == 3
        assert report["proposal"]["id"] == proposal["id"]
        assert cli("report-get", completed["report_id"])["data"]["id"] == completed["report_id"]
        assert len(cli("events", task_id)["items"]) == 7
        assert cli("reports", task_id)["items"] == index
        repeated = client.post(
            base + "/report",
            json={"expected_task_version": 5},
            headers={"Idempotency-Key": "report"},
        )
        assert repeated.content == published.content
    with server(tmp_path, database, data, bound=True) as client:
        assert client.get(base).json()["data"] == completed
        assert len(client.get(base + "/events").json()["items"]) == 7
        assert client.get("/api/v1/tasks").json()["items"][0] == completed
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.recover_before_serving()
        with candidate.snapshot() as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
            assert (
                connection.execute(
                    "SELECT count(*) FROM operations WHERE phase='completed'"
                ).fetchone()[0]
                == 4
            )
            assert connection.execute("SELECT count(*) FROM task_documents").fetchone()[0] == 2


def test_tcp_task_failures_replay_and_rejected_inputs(tmp_path, csv_factory, record):
    data = csv_factory([record])
    database = tmp_path / "candidate.sqlite3"
    with CandidateOwner(database) as owner:
        CandidateStore(owner).initialize_empty_candidate()
    with server(tmp_path, database, data, bound=True) as client:
        headers = {"Idempotency-Key": "analysis"}
        body = {"kind": "analysis", "request": {"query": "synthetic analysis"}}
        failed = client.post("/api/v1/tasks", json=body, headers=headers)
        assert failed.status_code == 503, failed.text
        task_id = failed.json()["task_id"]
        assert task_id
        assert client.get("/api/v1/tasks/" + task_id).json()["data"]["status"] == "failed"
        assert client.post("/api/v1/tasks", json=body, headers=headers).content == failed.content
        assert (
            client.post(
                "/api/v1/tasks", json=body | {"retry_of": task_id}, headers=headers
            ).status_code
            == 409
        )
        report = client.post(
            "/api/v1/tasks",
            json={"kind": "report", "request": DATES},
            headers={"Idempotency-Key": "report-task"},
        )
        assert report.status_code == 201, report.text
        assert report.json()["data"]["status"] == "completed"
        assert client.get("/api/v1/tasks?unknown=1").status_code == 422
        invalid = client.post("/api/v1/tasks", content=b"{}", headers={"Idempotency-Key": "bad"})
        assert invalid.status_code == 415
        large = client.post(
            "/api/v1/tasks",
            content=b" " * (64 * 1024 + 1),
            headers={"Idempotency-Key": "large", "Content-Type": "application/json"},
        )
        assert large.status_code == 413
        assert len(client.get("/api/v1/tasks").json()["items"]) == 2
        assert "synthetic analysis" not in json.dumps(client.get("/api/v1/tasks/" + task_id).json())
