"""Residual R01/R02/R04 checks, with real SQLite and supported HTTP transport."""

import json
import sqlite3
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.agent.contracts import AgentResponse
from app.main import create_app
from app.phase3.store import BusinessError, Store, digest, encode
from app.phase4.maintenance import check
from app.phase4.ownership import CandidateOwner, OwnershipError
from app.phase4.store import CandidateStore
from tests.phase4.test_acceptance_security import live, raw, snapshot
from tests.phase4.test_acceptance_state import context, create_budget, request
from tests.phase4.test_owner_application_integration import budget
from tests.phase4.test_reliability_generation import generations

__all__ = ["live", "context", "generations"]


def test_installed_root_rejects_all_unbound_writer_entries(generations, tmp_path, monkeypatch):
    rehearsal, _, _ = generations
    monkeypatch.setenv("GAMEADS_INSTANCE_ROOT", str(rehearsal.root))
    unbound = tmp_path / "standalone.sqlite3"
    with pytest.raises(OwnershipError, match="installed instance"):
        CandidateOwner(unbound).acquire()
    with pytest.raises(BusinessError, match="database_busy"):
        with Store(unbound).transaction():
            pytest.fail("unbound Store admitted")
    with pytest.raises(OwnershipError):
        check(unbound)
    assert not unbound.exists()
    assert not unbound.with_name(unbound.name + ".owner.lock").exists()
    result = subprocess.run(
        [sys.executable, "-m", "app.phase4.maintenance", "init", "--database", str(unbound)],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == "maintenance_failed"
    assert not unbound.exists()
    with pytest.raises(OwnershipError):
        with TestClient(create_app(business_db_path=unbound)):
            pytest.fail("unbound HTTP app admitted")


def test_installed_default_app_resolves_current_root_and_rejects_stale_explicit_path(
    generations,
    monkeypatch,
):
    rehearsal, _, new = generations
    monkeypatch.setenv("GAMEADS_INSTANCE_ROOT", str(rehearsal.root))

    def launch():
        return TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 1234))

    with launch() as client:
        assert client.get("/api/v1/tasks").status_code == 200
        assert client.app.state.business_db_path == rehearsal.root / "old/database.sqlite3"
        assert client.app.state.data_path == rehearsal.root / "old/dataset.csv"
        with pytest.raises(OwnershipError):
            rehearsal.owner.acquire()
    with rehearsal.locked():
        rehearsal.switch(new)
    with launch() as client:
        assert client.get("/api/v1/tasks").status_code == 200
        assert client.app.state.business_db_path == rehearsal.root / "new/database.sqlite3"
    with pytest.raises(OwnershipError):
        with TestClient(create_app(business_db_path=rehearsal.root / "old/database.sqlite3")):
            pytest.fail("stale explicit app admitted")


def test_installed_server_explicit_entry_uses_bound_dataset(generations, monkeypatch):
    from app import server

    rehearsal, _, _ = generations
    database = rehearsal.root / "old/database.sqlite3"
    calls = []

    def serve(app, **options):
        calls.append(options)
        assert app.state.data_path == database.parent / "dataset.csv"
        assert app.state.storage_owner.generation_directory == database.parent
        assert app.state.reports_dir == database.parent / "reports"

    monkeypatch.setattr(server, "serve", serve)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "server",
            "--instance-root",
            str(rehearsal.root),
            "--database",
            str(database),
            "--reports-dir",
            str(database.parent / "reports"),
        ],
    )
    server.main()
    assert calls == [{"port": 8000}]


@pytest.mark.parametrize("fault", ["status", "code", "retryable", "task_error", "event_code"])
def test_sealed_error_corruption_is_rejected_before_writes(context, fault):
    service, candidate = context
    task, proposal = create_budget(service)
    path = f"/api/v1/tasks/{task['id']}/approve"
    result = service.execute(
        ("POST", path, "stale"),
        request("approve", version=2, sha=proposal["proposal_sha256"]),
        task_id=task["id"],
        action="approve",
    )
    assert result[0] == 409
    candidate.owner.close()
    try:
        with sqlite3.connect(candidate.path) as connection:
            connection.row_factory = sqlite3.Row
            triggers = connection.execute(
                "SELECT sql,name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
            for row in triggers:
                connection.execute('DROP TRIGGER "' + row["name"] + '"')
            if fault == "status":
                connection.execute("UPDATE operations SET sealed_http_status=503 WHERE key='stale'")
            elif fault in {"code", "retryable"}:
                body = json.loads(result[1])
                body[fault] = "model_not_configured" if fault == "code" else True
                connection.execute(
                    "UPDATE operations SET response=? WHERE key='stale'", (encode(body).encode(),)
                )
            elif fault == "task_error":
                # Shape-valid but impossible: a denied continuation cannot make this task failed.
                connection.execute(
                    "UPDATE tasks SET status='failed',error=?",
                    (
                        encode(
                            {
                                "code": "stale_task_version",
                                "message": "safe",
                                "retryable": False,
                            }
                        ),
                    ),
                )
            else:
                connection.execute("UPDATE task_events SET code='other' WHERE action='denied'")
            for row in triggers:
                connection.execute(row["sql"])
        connection.close()
        before = candidate.path.read_bytes()
        with CandidateOwner(candidate.path) as owner:
            with pytest.raises(BusinessError, match="stored_evidence_integrity_error"):
                CandidateStore(owner).recover_before_serving()
            assert not owner.ready
        assert candidate.path.read_bytes() == before
    finally:
        candidate.owner.acquire()


@pytest.mark.parametrize(
    "outcome,http",
    [
        ("needs_clarification", 422),
        ("unsupported", 422),
        ("insufficient_evidence", 422),
        ("model_unavailable", 503),
        ("failed", 502),
    ],
)
def test_valid_sealed_error_survives_restart_exactly(context, outcome, http):
    from app.phase4.contracts import CREATE_ADAPTER
    from app.phase4.service import TaskService

    service, candidate = context

    class Offline:
        def run(self, body):
            return AgentResponse(request_id="offline", status=outcome, final_answer="safe")

    service.agent = Offline()
    body = CREATE_ADAPTER.validate_python({"kind": "analysis", "request": {"query": "private"}})
    identity = ("POST", "/api/v1/tasks", "error")
    result = service.execute(identity, body)
    assert result[0] == http
    candidate.owner.close()
    try:
        with CandidateOwner(candidate.path) as owner:
            reopened = CandidateStore(owner)
            assert reopened.recover_before_serving() == 0
            replay = TaskService(
                reopened,
                data_path=service.business.data_path,
                reports_dir=service.reports.reports_dir,
                agent=None,
            )
            assert replay.execute(identity, body) == result
    finally:
        candidate.owner.acquire()


@pytest.mark.parametrize(
    "fault",
    [
        "pending-sequence",
        "pending-predecessor",
        "complete-identity",
        "complete-old",
        "recovered-observed",
        "recovered-admission",
        "missing-complete",
        "active-sequence",
    ],
)
def test_generation_record_combinations_reject_without_repair(generations, fault):
    rehearsal, old, new = generations
    with rehearsal.locked():
        decision = rehearsal.switch(new)
    active = rehearsal.root / "active.json"
    complete = rehearsal.root / "00000002.complete.json"
    transition = json.loads(complete.read_text())
    if fault.startswith("pending"):
        transition["new"]["sequence" if fault.endswith("sequence") else "predecessor"] = (
            4 if fault.endswith("sequence") else new
        )
        (rehearsal.root / "pending.json").write_text(encode(transition))
    elif fault == "complete-identity":
        complete.rename(rehearsal.root / "00000003.complete.json")
    elif fault == "complete-old":
        transition["old"]["generation"] = new
        complete.write_text(encode(transition))
    elif fault.startswith("recovered"):
        evidence = {
            "observed": decision,
            **transition,
            "writer_admission": False,
            "requires_explicit_decision": True,
            "frozen_ambiguous_outcome": "UNPROVEN",
            "power_loss": "UNPROVEN",
        }
        if fault == "recovered-observed":
            evidence["observed"] = {**decision, "generation": old}
        else:
            evidence["writer_admission"] = True
        (rehearsal.root / "00000002.recovered.json").write_text(encode(evidence))
    elif fault == "missing-complete":
        complete.unlink()
    else:
        value = {**decision, "sequence": True}
        active.write_text(encode(value | {"sha256": digest(value)}))
    before = {p.name: p.read_bytes() for p in rehearsal.root.glob("*.json")}
    with rehearsal.locked():
        with pytest.raises(BusinessError, match="stored_evidence_integrity_error"):
            rehearsal.resolve()
    with pytest.raises(OwnershipError):
        CandidateOwner(rehearsal.root / "new/database.sqlite3").acquire()
    assert {p.name: p.read_bytes() for p in rehearsal.root.glob("*.json")} == before


@pytest.mark.parametrize(
    "outcome",
    [
        "completed",
        "needs_clarification",
        "unsupported",
        "insufficient_evidence",
        "model_unavailable",
        "failed",
        "in_progress",
        "interrupted",
    ],
)
def test_security_inputs_cross_every_analysis_outcome(live, tmp_path, monkeypatch, outcome, caplog):
    client, candidate, counters, port, _ = live
    body = {"kind": "analysis", "request": {"query": "private-matrix-sentinel"}}
    path = b"/api/v1/tasks"
    if outcome in {"in_progress", "interrupted"}:
        from app.phase4.contracts import CREATE_ADAPTER

        candidate.reserve(
            "POST",
            path,
            b"matrix",
            digest(CREATE_ADAPTER.validate_python(body).model_dump(mode="json")),
        )
        if outcome == "interrupted":
            # Same actual restart recovery algorithm, with no abandoned model call.
            candidate._admitted = False
            candidate.recover_before_serving()
        result = client.post(path.decode(), json=body, headers={"Idempotency-Key": "matrix"})
        assert result.status_code == 409
    else:
        monkeypatch.setattr(
            "app.agent.service.AgentService.run",
            lambda self, body: AgentResponse(
                request_id="offline",
                status=outcome,
                final_answer="safe",
            ),
        )
        result = client.post(path.decode(), json=body, headers={"Idempotency-Key": "matrix"})
        assert result.status_code == {
            "completed": 201,
            "model_unavailable": 503,
            "failed": 502,
        }.get(outcome, 422)
    before = snapshot(candidate, tmp_path)
    host = (b"Host", f"127.0.0.1:{port}".encode())
    key = (b"Idempotency-Key", b"matrix")
    vectors = [
        (path, [host, key, (b"Origin", b"null")], b"{}", 403),
        (path, [(b"Host", b"evil.invalid"), key], b"{}", 403),
        (path, [host, key, (b"Idempotency-Key", b"matrix")], b"{}", 422),
        (path + b"/", [host, key], b"{}", 422),
        (path, [host, key, (b"Content-Type", b"text/plain")], b"{}", 415),
        (path, [host, key, (b"Content-Type", b"application/json")], b"{", 400),
        (path, [host, key, (b"Content-Type", b"application/json")], b"{}", 422),
    ]
    for target, headers, payload, expected in vectors:
        counters.update(router=0, body=0)
        code, wire = raw(port, "POST", target, headers, payload)
        assert code == expected, wire
        if (
            expected not in {400, 422}
            or target != path
            or len(headers) == 3
            and headers[-1][0] == b"Idempotency-Key"
        ):
            assert counters == {"router": 0, "body": 0}
        assert snapshot(candidate, tmp_path) == before
    changed = {**body, "request": {"query": "changed"}}
    assert (
        client.post(path.decode(), json=changed, headers={"Idempotency-Key": "matrix"}).json()[
            "code"
        ]
        == "idempotency_key_reused"
    )
    replay = client.post(path.decode(), json=body, headers={"Idempotency-Key": "matrix"})
    assert (replay.status_code, replay.content) == (result.status_code, result.content)
    assert snapshot(candidate, tmp_path) == before
    assert "private-matrix-sentinel" not in caplog.text + result.text


@pytest.mark.parametrize("action", ["approve", "reject", "simulate", "report"])
@pytest.mark.parametrize("phase", ["success", "denied", "in_progress", "interrupted"])
def test_continuation_security_cross_operation_phases(live, tmp_path, action, phase):
    client, candidate, counters, port, _ = live
    response = client.post(
        "/api/v1/tasks",
        json={"kind": "budget_review", "request": budget()},
        headers={"Idempotency-Key": "create"},
    )
    assert response.status_code == 201
    task = response.json()["data"]
    proposal = client.get("/api/v1/budget-proposals/" + task["proposal_id"]).json()["data"]
    base = f"/api/v1/tasks/{task['id']}"
    if action in {"simulate", "report"}:
        payload = request("approve", sha=proposal["proposal_sha256"]).model_dump(mode="json")
        assert (
            client.post(
                base + "/approve", json=payload, headers={"Idempotency-Key": "approved"}
            ).status_code
            == 200
        )
    if action == "report":
        payload = request(
            "simulate", version=4, proposal_version=2, sha=proposal["proposal_sha256"]
        ).model_dump(mode="json")
        assert (
            client.post(
                base + "/simulate", json=payload, headers={"Idempotency-Key": "simulated"}
            ).status_code
            == 200
        )
    payload = request(
        action,
        version=5 if action == "report" else 4 if action == "simulate" else 3,
        proposal_version=2 if action == "simulate" else 1,
        sha=proposal["proposal_sha256"],
    ).model_dump(mode="json")
    if phase == "denied":
        payload["expected_task_version"] = 99
    path = (base + "/" + action).encode()
    if phase in {"in_progress", "interrupted"}:
        candidate.reserve("POST", path, b"matrix", digest(payload), task_id=task["id"])
        if phase == "interrupted":
            candidate._admitted = False
            candidate.recover_before_serving()
    result = client.post(path.decode(), json=payload, headers={"Idempotency-Key": "matrix"})
    assert result.status_code == (200 if phase == "success" else 409)
    before = snapshot(candidate, tmp_path)
    host = (b"Host", f"127.0.0.1:{port}".encode())
    headers = [host, (b"Idempotency-Key", b"matrix")]
    for target, extra, content, expected, early in [
        (path, [(b"Origin", b"null")], b"{}", 403, True),
        (path + b"/", [], b"{}", 422, True),
        (path, [(b"Idempotency-Key", b"matrix")], b"{}", 422, True),
        (path, [(b"Content-Type", b"text/plain")], b"{}", 415, True),
        (path, [(b"Content-Type", b"application/json")], b"{", 400, False),
        (
            path,
            [(b"Content-Type", b"application/json")],
            encode({**payload, "expected_task_version": True}).encode(),
            422,
            False,
        ),
    ]:
        counters.update(router=0, body=0)
        code, wire = raw(port, "POST", target, headers + extra, content)
        assert code == expected, wire
        if early:
            assert counters == {"router": 0, "body": 0}
        assert snapshot(candidate, tmp_path) == before
    replay = client.post(path.decode(), json=payload, headers={"Idempotency-Key": "matrix"})
    assert (replay.status_code, replay.content) == (result.status_code, result.content)
    assert snapshot(candidate, tmp_path) == before
