"""Semantic request partitions over HTTP with full SQLite/file no-effect checks."""

import copy
import hashlib
import sqlite3

import pytest

from app.phase4.contracts import TaskError
from tests.phase4.test_final_contract import local
from tests.phase4.test_owner_application_integration import DATES, budget

__all__ = ["local"]


def snapshot(candidate):
    with candidate.snapshot() as connection:
        rows = {
            table: [
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in (
                "tasks",
                "operations",
                "task_events",
                "task_documents",
                "documents",
                "events",
                "schema_migrations",
            )
        }
    files = {
        str(path.relative_to(candidate.path.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in candidate.path.parent.rglob("*")
        if path.is_file() and not path.name.endswith(("-shm", ".owner.lock"))
    }
    return rows, files


def bodies():
    return {
        "analysis": {"kind": "analysis", "request": {"query": "synthetic private input"}},
        "budget_review": {"kind": "budget_review", "request": budget()},
        "report": {"kind": "report", "request": DATES},
        "approve": {
            "expected_task_version": 3,
            "expected_version": 1,
            "proposal_sha256": "a" * 64,
            "approver": "synthetic",
            "confirmed": True,
        },
        "reject": {
            "expected_task_version": 3,
            "expected_version": 1,
            "proposal_sha256": "a" * 64,
            "approver": "synthetic",
            "confirmed": True,
            "reason": "synthetic",
        },
        "simulate": {
            "expected_task_version": 4,
            "expected_version": 2,
            "proposal_sha256": "a" * 64,
        },
        "publish": {"expected_task_version": 5},
    }


def cases():
    output = []
    for kind, body in bodies().items():
        for field in body:
            if field == "retry_of":
                continue
            missing = copy.deepcopy(body)
            del missing[field]
            output.append(pytest.param(kind, missing, id=f"{kind}-missing-{field}"))
            for value, label in ((None, "null"), ([], "array"), (False, "bool")):
                changed = copy.deepcopy(body)
                changed[field] = value
                output.append(pytest.param(kind, changed, id=f"{kind}-{field}-{label}"))
        output.append(pytest.param(kind, body | {"unknown": "private"}, id=f"{kind}-extra"))
    vectors = [
        ("analysis", {"query": ""}),
        ("analysis", {"query": 7}),
        ("analysis", {"query": "ok", "unknown": 1}),
        ("report", DATES | {"start_date": "2026-11-01"}),
        ("report", DATES | {"start_date": "2026-02-30"}),
        ("report", DATES | {"start_date": "2026-10-01T00:00:00Z"}),
        ("report", DATES | {"campaign_ids": []}),
        ("report", DATES | {"campaign_ids": ["C001", "C001"]}),
        ("report", DATES | {"baseline_days": True}),
        ("report", DATES | {"baseline_days": 366}),
        ("report", DATES | {"proposal_id": "a" * 32}),
        ("report", DATES | {"output_format": "html"}),
    ]
    for field in ("start_date", "end_date", "budget_period", "campaigns"):
        changed = budget()
        del changed[field]
        vectors.append(("budget_review", changed))
    for field, values in {
        "total_budget": [True, "-0.01", "1.001", "10000000000000000.00", "NaN"],
        "max_change_fraction": [True, "-0.0001", "1.0001", "0.00001"],
        "min_impressions": [True, 0, "1", 1.1],
        "currency": ["EUR"],
        "campaigns": [[], [budget()["campaigns"][0]] * 2],
        "budget_period": [DATES],
    }.items():
        for value in values:
            vectors.append(("budget_review", budget() | {field: value}))
    for field, value in (("min_budget", "201"), ("current_budget", True), ("campaign_id", "\0")):
        changed = budget()
        changed["campaigns"][0][field] = value
        vectors.append(("budget_review", changed))
    for index, (kind, request) in enumerate(vectors):
        output.append(pytest.param(kind, {"kind": kind, "request": request}, id=f"nested-{index}"))
    for kind in ("approve", "reject", "simulate", "publish"):
        for field in ("expected_task_version", "expected_version"):
            if field not in bodies()[kind]:
                continue
            for value in (0, -1, True, "3", 1.5):
                output.append(
                    pytest.param(
                        kind, bodies()[kind] | {field: value}, id=f"{kind}-{field}-{value}"
                    )
                )
    for kind in ("approve", "reject"):
        for value in (False, 1, "true"):
            output.append(
                pytest.param(
                    kind, bodies()[kind] | {"confirmed": value}, id=f"{kind}-confirm-{value}"
                )
            )
    return output


@pytest.mark.parametrize("kind,body", cases())
def test_structural_errors_never_reserve_or_audit(local, kind, body):
    client, candidate = local
    path = "/api/v1/tasks"
    if kind in {"approve", "reject", "simulate", "publish"}:
        path += "/" + "a" * 32 + "/" + ("report" if kind == "publish" else kind)
    before = snapshot(candidate)
    response = client.post(path, json=body, headers={"Idempotency-Key": "invalid"})
    assert response.status_code == 422, response.text
    error = TaskError.model_validate(response.json())
    assert error.code == "invalid_request" and error.task_id is None and not error.retryable
    assert "private" not in response.text
    assert snapshot(candidate) == before


@pytest.mark.parametrize(
    "suffix", ["", "/" + "a" * 32, "/" + "a" * 32 + "/events", "/" + "a" * 32 + "/reports"]
)
@pytest.mark.parametrize("query", ["unknown=1", "limit=0", "offset=-1", "limit=1&limit=2"])
def test_get_errors_are_read_only(local, suffix, query):
    client, candidate = local
    before = snapshot(candidate)
    response = client.get("/api/v1/tasks" + suffix + "?" + query)
    assert response.status_code == 422
    TaskError.model_validate(response.json())
    assert snapshot(candidate) == before


def test_semantic_denial_and_conflicting_replay_match_persisted_bytes(local):
    client, candidate = local
    body = bodies()["budget_review"]
    created = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "created"})
    assert created.status_code == 201
    task = created.json()["data"]
    before = snapshot(candidate)
    conflict = client.post(
        "/api/v1/tasks",
        json=body | {"retry_of": task["id"]},
        headers={"Idempotency-Key": "created"},
    )
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_key_reused"
    assert snapshot(candidate) == before
    path = "/api/v1/tasks/" + task["id"] + "/approve"
    denied = client.post(
        path,
        json=bodies()["approve"] | {"expected_task_version": 2},
        headers={"Idempotency-Key": "denied"},
    )
    assert denied.status_code == 409 and denied.json()["code"] == "stale_task_version"
    TaskError.model_validate(denied.json())
    assert client.get("/api/v1/tasks/" + task["id"]).json()["data"] == task
    with candidate.snapshot() as connection:
        op = connection.execute("SELECT * FROM operations WHERE key='denied'").fetchone()
        assert op["phase"] == "completed" and op["sealed_http_status"] == denied.status_code
        assert op["response"] == denied.content
        events = list(connection.execute("SELECT * FROM task_events WHERE action='denied'"))
        assert len(events) == 1 and events[0]["from_status"] == events[0]["to_status"]
    sealed = snapshot(candidate)
    assert (
        client.post(
            path,
            json=bodies()["approve"] | {"expected_task_version": 2},
            headers={"Idempotency-Key": "denied"},
        ).content
        == denied.content
    )
    assert snapshot(candidate) == sealed


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "/" + "a" * 32,
        "/" + "a" * 32 + "/events",
        "/" + "a" * 32 + "/reports",
        "/operations/lookup",
    ],
)
def test_actual_read_lock_contention_returns_503_without_persistent_effect(local, suffix):
    client, candidate = local
    # Normalize committed WAL through explicit maintenance before capturing
    # physical hashes; closing the anchor must not checkpoint earlier writes.
    assert candidate.checkpoint() == (0, 0, 0)
    before = snapshot(candidate)
    # Explicit maintenance drain permits the independent connection to obtain
    # EXCLUSIVE ownership. Ordinary GETs must never perform this close/open.
    with candidate.owner.mutex:
        candidate.owner._read_anchor.close()
        candidate.owner._read_anchor = None
    blocker = sqlite3.connect(candidate.path, isolation_level=None)
    try:
        assert blocker.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0] == "exclusive"
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute("SELECT count(*) FROM tasks").fetchone()
        options = {}
        if suffix == "/operations/lookup":
            options = {
                "params": {"method": "POST", "path": "/api/v1/tasks"},
                "headers": {"Idempotency-Key": "busy"},
            }
        response = client.get("/api/v1/tasks" + suffix, **options)
        assert response.status_code == 503, response.text
        error = TaskError.model_validate(response.json())
        assert error.code == "database_busy" and error.task_id is None
    finally:
        blocker.close()
        candidate.owner.warm_reads()
    assert snapshot(candidate) == before
