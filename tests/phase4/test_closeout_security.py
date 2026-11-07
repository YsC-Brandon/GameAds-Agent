"""Positive TCP browser/legacy controls and filesystem-exact read controls."""

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.phase4.test_acceptance_security import live, raw, snapshot
from tests.phase4.test_owner_application_integration import DATES, budget

__all__ = ["live"]


def tree(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        if p.is_file()
        else None
        for p in root.rglob("*")
    }


@pytest.mark.parametrize("existing", [False, True])
def test_legacy_read_routes_create_no_directory_lock_or_database(tmp_path, existing):
    database = tmp_path / "state.sqlite3"
    if existing:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE documents(id TEXT PRIMARY KEY,kind TEXT NOT NULL,"
                "payload TEXT NOT NULL,sha256 TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE events(id INTEGER PRIMARY KEY,document_id TEXT NOT NULL,"
                "action TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL)"
            )
        connection.close()
    app = create_app(business_db_path=database, reports_dir=tmp_path / "missing" / "reports")
    before = tree(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        for path in (
            "/api/v1/reports",
            "/api/v1/reports/" + "a" * 32,
            "/api/v1/budget-proposals/" + "a" * 32,
            "/api/v1/budget-proposals/" + "a" * 32 + "/events",
        ):
            response = client.get(path)
            assert response.status_code == (
                (200 if path == "/api/v1/reports" else 404) if existing else 503
            )
            assert tree(tmp_path) == before
    assert tree(tmp_path) == before


@pytest.mark.parametrize("authority", ["127.0.0.1", "localhost", "[::1]"])
@pytest.mark.parametrize("origin", [False, True])
def test_positive_security_all_legacy_mutations_and_readbacks(live, authority, origin, tmp_path):
    client, candidate, counters, port, _ = live
    host = f"{authority}:{port}"
    headers = {"Host": host}
    if origin:
        headers["Origin"] = "http://" + host
    # Raw TCP confirms the supported transport retains legitimate headers too.
    code, _ = raw(port, "GET", b"/health", [(k.encode(), v.encode()) for k, v in headers.items()])
    assert code == 200
    for decision in ("approve", "reject"):
        response = client.post("/api/v1/budget-proposals", json=budget(), headers=headers)
        assert response.status_code == 201
        proposal = response.json()["data"]
        path = "/api/v1/budget-proposals/" + proposal["id"]
        body = {
            "expected_version": 1,
            "proposal_sha256": proposal["proposal_sha256"],
            "confirmed": True,
            "approver": "synthetic",
        }
        if decision == "reject":
            body["reason"] = "synthetic decision"
        result = client.post(path + "/" + decision, json=body, headers=headers)
        assert result.status_code == 200 and result.json()["data"]["status"] == (
            "approved" if decision == "approve" else "rejected"
        )
        if decision == "approve":
            result = client.post(
                path + "/simulate",
                json={"expected_version": 2, "proposal_sha256": proposal["proposal_sha256"]},
                headers=headers,
            )
            assert result.status_code == 200 and result.json()["data"]["status"] == "simulated"
        before = snapshot(candidate, tmp_path)
        for suffix in ("", "/events"):
            assert client.get(path + suffix, headers=headers).status_code == 200
            assert snapshot(candidate, tmp_path) == before
    response = client.post("/api/v1/reports", json=DATES, headers=headers)
    assert response.status_code == 201
    report = response.json()["data"]
    before = snapshot(candidate, tmp_path)
    assert client.get("/api/v1/reports", headers=headers).status_code == 200
    assert client.get("/api/v1/reports/" + report["id"], headers=headers).json()["data"]["content"]
    assert snapshot(candidate, tmp_path) == before
    assert counters["router"] > 0


@pytest.mark.parametrize("action", ["approve", "reject", "simulate", "report"])
def test_sealed_continuation_invalid_inputs_have_zero_side_effects(live, action, tmp_path):
    client, candidate, _, _, _ = live
    response = client.post(
        "/api/v1/tasks",
        json={"kind": "budget_review", "request": budget()},
        headers={"Idempotency-Key": "create"},
    )
    assert response.status_code == 201
    task = response.json()["data"]
    proposal = client.get("/api/v1/budget-proposals/" + task["proposal_id"]).json()["data"]
    path = f"/api/v1/tasks/{task['id']}"

    def body(version, proposal_version=1):
        if action == "report" and version == 5:
            return {"expected_task_version": version}
        return {
            "expected_task_version": version,
            "expected_version": proposal_version,
            "proposal_sha256": proposal["proposal_sha256"],
        }

    if action in {"simulate", "report"}:
        payload = body(3) | {"confirmed": True, "approver": "synthetic"}
        assert (
            client.post(
                path + "/approve", json=payload, headers={"Idempotency-Key": "approve"}
            ).status_code
            == 200
        )
    if action == "report":
        assert (
            client.post(
                path + "/simulate", json=body(4, 2), headers={"Idempotency-Key": "simulate"}
            ).status_code
            == 200
        )
    payload = body(
        5 if action == "report" else 4 if action == "simulate" else 3,
        2 if action == "simulate" else 1,
    )
    if action in {"approve", "reject"}:
        payload |= {"confirmed": True, "approver": "synthetic"}
        if action == "reject":
            payload["reason"] = "synthetic"
    headers = {"Idempotency-Key": "sealed-action"}
    result = client.post(path + "/" + action, json=payload, headers=headers)
    assert result.status_code == 200, result.text
    before = snapshot(candidate, tmp_path)
    for invalid in ({}, payload | {"expected_task_version": True}, payload | {"unknown": 1}):
        rejected = client.post(path + "/" + action, json=invalid, headers=headers)
        assert rejected.status_code == 422
        assert snapshot(candidate, tmp_path) == before
    replay = client.post(path + "/" + action, json=payload, headers=headers)
    assert (replay.status_code, replay.content) == (result.status_code, result.content)
    assert snapshot(candidate, tmp_path) == before
    sealed = candidate.lookup("POST", (path + "/" + action).encode(), b"sealed-action")
    assert sealed["response"] == result.content
    assert json.loads(sealed["response"])["data"]["active_operation_id"] is None
