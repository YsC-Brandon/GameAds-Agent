"""Acceptance regressions against frozen r4; real SQLite, offline dependencies."""

import json
import os

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app, get_settings
from app.phase3.store import digest
from app.phase4.contracts import TaskResponse
from app.phase4.ownership import CandidateOwner
from app.phase4.service import TaskService
from app.phase4.store import CandidateStore
from tests.phase4.test_owner_application_integration import DATES, budget

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ownership acceptance")


@pytest.fixture
def local(tmp_path, csv_factory, record):
    data = csv_factory([record])
    with CandidateOwner(tmp_path / "candidate.sqlite3") as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        application = create_app(
            data_path=data,
            business_db_path=candidate.path,
            reports_dir=tmp_path / "reports",
            storage_owner=owner,
        )
        application.dependency_overrides[get_settings] = lambda: Settings(
            _env_file=None, llm_api_key="", llm_model=""
        )
        # Owner belongs to the fixture, not a lifespan adapter.
        client = TestClient(application, raise_server_exceptions=False)
        try:
            yield client, candidate
        finally:
            client.close()


def counts(candidate):
    with candidate.snapshot() as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "operations", "task_events", "documents", "events")
        )


def test_malformed_json_is_400_before_reservation(local):
    client, candidate = local
    response = client.post(
        "/api/v1/tasks",
        content=b'{"kind":',
        headers={"Idempotency-Key": "malformed", "Content-Type": "application/json"},
    )
    assert (response.status_code, response.json()["code"]) == (400, "invalid_json")
    assert counts(candidate) == (0, 0, 0, 0, 0)


def test_task_wrong_method_uses_frozen_error(local):
    client, candidate = local
    response = client.patch("/api/v1/tasks")
    assert (response.status_code, response.json()["code"]) == (405, "method_not_allowed")
    assert counts(candidate) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("running", [False, True])
def test_recovered_create_is_queryable_with_complete_error(tmp_path, running):
    database = tmp_path / "candidate.sqlite3"
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        operation, _ = candidate.reserve("POST", b"/api/v1/tasks", b"restart", digest({}))
        identity = candidate.create_submitted(operation["operation_id"], "analysis")
        if running:
            candidate.mark_running(operation["operation_id"], 1)
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        assert candidate.recover_before_serving() == 1
        service = TaskService(
            candidate,
            data_path=tmp_path / "absent.csv",
            reports_dir=tmp_path / "reports",
            agent=None,
        )
        result = TaskResponse.model_validate(service.get(identity))
        assert result.data.status == "interrupted"
        assert result.data.active_operation_id is None
        assert result.data.error.code == "operation_interrupted"
        assert result.data.error.retryable is False


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/budget-proposals/" + "a" * 32,
        "/api/v1/budget-proposals/" + "a" * 32 + "/events",
        "/api/v1/reports",
        "/api/v1/reports/" + "a" * 32,
    ],
)
def test_legacy_get_missing_database_does_not_initialize(tmp_path, path):
    database = tmp_path / "missing.sqlite3"
    app = create_app(business_db_path=database, reports_dir=tmp_path / "reports")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["code"] == "schema_upgrade_required"
    assert not database.exists()
    assert not (tmp_path / "reports").exists()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"kind": "other", "request": {}},
        {"kind": "analysis", "request": {"query": "x", "secret": "never-echo"}},
        {"kind": "report", "request": DATES | {"proposal_id": "a" * 32}},
        {"kind": "report", "request": DATES, "extra": True},
        {"kind": "report", "request": DATES, "retry_of": "BAD"},
        {"kind": "report", "request": DATES | {"baseline_days": True}},
    ],
)
def test_invalid_task_requests_never_consume_key(local, body):
    client, candidate = local
    response = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "unused"})
    assert response.status_code == 422
    assert response.json()["task_id"] is None
    assert "never-echo" not in response.text
    assert counts(candidate) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "suffix", ["", "/" + "a" * 32, "/" + "a" * 32 + "/events", "/" + "a" * 32 + "/reports"]
)
@pytest.mark.parametrize("query", ["unknown=1", "limit=1&limit=1", "offset=-1", "limit=101"])
def test_task_get_query_boundary(local, suffix, query):
    client, candidate = local
    assert client.get("/api/v1/tasks" + suffix + "?" + query).status_code == 422
    assert counts(candidate) == (0, 0, 0, 0, 0)


def test_every_task_operation_response_and_replay(local, record_property):
    client, candidate = local
    samples = []

    def call(method, path, *, template=None, body=None, key=None, expected=200, params=None):
        options = {"headers": {"Idempotency-Key": key} if key else {}}
        if body is not None:
            options["json"] = body
        if params is not None:
            options["params"] = params
        response = client.request(method, path, **options)
        assert response.status_code == expected, response.text
        samples.append(
            {
                "method": method.lower(),
                "path": template or path,
                "status": expected,
                "request": body,
                "response": response.json(),
            }
        )
        if method == "POST":
            before = counts(candidate)
            replay = client.request(method, path, **options)
            assert (replay.status_code, replay.content) == (response.status_code, response.content)
            assert counts(candidate) == before
        return response.json()

    task = call(
        "POST",
        "/api/v1/tasks",
        body={"kind": "budget_review", "request": budget()},
        key="accept-create",
        expected=201,
    )["data"]
    base = "/api/v1/tasks/" + task["id"]
    template = "/api/v1/tasks/{task_id}"
    call("GET", "/api/v1/tasks")
    call("GET", base, template=template)
    call("GET", base + "/events", template=template + "/events")
    call("GET", base + "/reports", template=template + "/reports")
    call(
        "GET",
        "/api/v1/tasks/operations/lookup",
        key="accept-create",
        params={"method": "POST", "path": "/api/v1/tasks"},
    )
    proposal = client.get("/api/v1/budget-proposals/" + task["proposal_id"]).json()["data"]
    review = {
        "expected_version": 1,
        "expected_task_version": 3,
        "proposal_sha256": proposal["proposal_sha256"],
        "confirmed": True,
        "approver": "synthetic-reviewer",
    }
    call("POST", base + "/approve", template=template + "/approve", body=review, key="approve")
    call(
        "POST",
        base + "/simulate",
        template=template + "/simulate",
        body={
            "expected_version": 2,
            "expected_task_version": 4,
            "proposal_sha256": proposal["proposal_sha256"],
        },
        key="simulate",
    )
    call(
        "POST",
        base + "/report",
        template=template + "/report",
        body={"expected_task_version": 5},
        key="report",
    )
    call("GET", base + "/reports", template=template + "/reports")
    other = call(
        "POST",
        "/api/v1/tasks",
        body={"kind": "budget_review", "request": budget()},
        key="reject-create",
        expected=201,
    )["data"]
    proposal = client.get("/api/v1/budget-proposals/" + other["proposal_id"]).json()["data"]
    call(
        "POST",
        "/api/v1/tasks/" + other["id"] + "/reject",
        template=template + "/reject",
        body=review | {"proposal_sha256": proposal["proposal_sha256"], "reason": "synthetic"},
        key="reject",
    )
    call(
        "POST",
        "/api/v1/tasks",
        body={"kind": "report", "request": DATES},
        key="report-create",
        expected=201,
    )
    call(
        "POST",
        "/api/v1/tasks",
        body={"kind": "analysis", "request": {"query": "offline"}},
        key="analysis-create",
        expected=503,
    )
    record_property("contract_samples", json.dumps(samples))
    record_property("runtime_openapi", json.dumps(client.get("/openapi.json").json()))
