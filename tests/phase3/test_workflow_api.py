"""Real Phase 2 graph inside Phase 3 report graph; scripted model only, no paid calls."""

import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.agent.contracts import ModelMessage
from app.agent.provider import ModelError
from app.agent.service import AgentService
from app.core.config import Settings
from app.main import get_agent_service, get_settings
from app.phase3.contracts import ReportRequest
from app.phase3.workflow import ReportWorkflow
from tests.transport_support import create_app

DATES = {"start_date": "2026-10-01", "end_date": "2026-10-01"}


def message(name, arguments, identity):
    return ModelMessage(
        tool_calls=[
            {
                "id": identity,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    )


class ScriptedProvider:
    def __init__(self, replies):
        self.replies, self.requests = replies, []

    def complete(self, messages, tools, tool_choice="auto"):
        self.requests.append(copy.deepcopy((messages, tools, tool_choice)))
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def provider():
    return ScriptedProvider(
        [
            message(
                "submit_plan",
                {
                    "task_type": "summary",
                    "date_range": DATES,
                    "steps": ["summarize_campaign_performance"],
                },
                "plan",
            ),
            message("summarize_campaign_performance", DATES, "summary"),
            message(
                "finish_analysis",
                {
                    "references": [{"call_id": "summary", "path": []}],
                    "interpretations": [{"call_id": "summary", "kind": "performance"}],
                },
                "finish",
            ),
        ]
    )


def agent(business, model):
    return AgentService(
        Settings(_env_file=None, llm_api_key="", llm_model=""),
        provider=model,
        data_path=business.data_path,
    )


def test_real_nested_langgraph_and_feedback(reports):
    model = provider()
    workflow = ReportWorkflow(agent(reports.business, model), reports)
    assert type(workflow.graph).__name__ == "CompiledStateGraph"
    result = workflow.run(ReportRequest(**DATES))
    assert result["status"] == "completed"
    assert result["steps"] == ["analyze_with_phase2_agent", "publish_evidence_report"]
    assert result["report"]["generator"] == "agent_report_workflow"
    assert result["agent"]["tool_results"]["summary"] == json.loads(
        model.requests[2][0][-1]["content"]
    )
    assert model.requests[2][0][-1]["tool_call_id"] == "summary"
    assert result["report"]["agent"]["request_id"] == result["agent"]["request_id"]
    schemas = json.dumps(model.requests)
    assert "approve_budget_proposal" not in schemas and "simulate_budget_changes" not in schemas


@pytest.mark.parametrize("code", ["model_timeout", "model_not_configured", "model_rate_limited"])
def test_model_failure_stops_before_report(reports, code):
    model = ScriptedProvider([ModelError(code)])
    result = ReportWorkflow(agent(reports.business, model), reports).run(ReportRequest(**DATES))
    assert result["status"] == "model_unavailable"
    assert result["report"] is None
    assert result["steps"] == ["analyze_with_phase2_agent"]
    assert reports.list_reports() == []


def test_wrong_agent_dates_cannot_publish(reports):
    model = provider()
    model.replies[0] = message(
        "submit_plan",
        {
            "task_type": "summary",
            "date_range": {"start_date": "2026-10-02", "end_date": "2026-10-02"},
            "steps": ["summarize_campaign_performance"],
        },
        "plan",
    )
    result = ReportWorkflow(agent(reports.business, model), reports).run(ReportRequest(**DATES))
    assert result["status"] == "needs_clarification" and result["report"] is None


@pytest.fixture
def client(business, tmp_path):
    application = create_app(
        data_path=business.data_path,
        business_db_path=business.store.path,
        reports_dir=tmp_path / "reports",
    )
    application.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, llm_api_key="", llm_model=""
    )
    return TestClient(application, raise_server_exceptions=False)


def test_api_no_key_budget_review_simulate_and_report(client, budget_request):
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    response = client.post("/api/v1/budget-proposals", json=budget_request.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    proposal = response.json()["data"]
    base = "/api/v1/budget-proposals/" + proposal["id"]
    args = {"expected_version": 1, "proposal_sha256": proposal["proposal_sha256"]}
    assert client.post(base + "/simulate", json=args).status_code == 409
    assert client.post(base + "/approve", json=args).status_code == 422
    approved = client.post(
        base + "/approve", json=args | {"confirmed": True, "approver": "local-reviewer"}
    )
    assert approved.status_code == 200, approved.text
    args["expected_version"] = 2
    assert client.post(base + "/simulate", json=args | {"mode": "real"}).status_code == 422
    assert client.post(base + "/simulate", json=args).json()["data"]["status"] == "simulated"
    assert client.post(base + "/simulate", json=args).status_code == 409
    assert client.get(base).json()["data"]["version"] == 3
    assert len(client.get(base + "/events").json()["data"]["events"]) >= 4
    report = client.post("/api/v1/reports", json=DATES | {"proposal_id": proposal["id"]})
    assert report.status_code == 201, report.text
    report_id = report.json()["data"]["id"]
    assert client.get("/api/v1/reports/" + report_id).json()["data"]["content"]
    assert client.get("/api/v1/reports").json()["data"]["reports"][0]["id"] == report_id


def test_api_reject_cannot_later_execute(client, budget_request):
    proposal = client.post(
        "/api/v1/budget-proposals", json=budget_request.model_dump(mode="json")
    ).json()["data"]
    base = "/api/v1/budget-proposals/" + proposal["id"]
    args = {"expected_version": 1, "proposal_sha256": proposal["proposal_sha256"]}
    rejected = client.post(
        base + "/reject",
        json=args | {"confirmed": True, "approver": "reviewer", "reason": "Insufficient evidence"},
    )
    assert rejected.status_code == 200
    assert client.post(base + "/simulate", json=args | {"expected_version": 2}).status_code == 409


def test_api_workflow_uses_existing_injected_agent(client, business):
    model = provider()
    client.app.dependency_overrides[get_agent_service] = lambda: agent(business, model)
    result = client.post("/api/v1/report-workflows", json=DATES)
    assert result.status_code == 200, result.text
    assert result.json()["data"]["report"]["generator"] == "agent_report_workflow"
    assert len(model.requests) == 3


def test_api_no_key_workflow_is_not_fake_success(client):
    response = client.post("/api/v1/report-workflows", json=DATES)
    assert response.status_code == 503, response.text
    assert response.json()["data"]["report"] is None


@pytest.mark.parametrize(
    "headers", [{"origin": "https://evil.invalid"}, {"host": "evil.invalid"}, {"origin": "null"}]
)
def test_cross_origin_and_host_rejected(client, budget_request, headers):
    response = client.post(
        "/api/v1/budget-proposals", json=budget_request.model_dump(mode="json"), headers=headers
    )
    assert response.status_code == 403


def test_errors_do_not_reflect_paths_or_unknown_fields(client):
    response = client.post("/api/v1/reports", json=DATES | {"output_path": "private-secret.md"})
    assert response.status_code == 422 and "private-secret" not in response.text
    # r4 forbids GET initialization. Keep the historical absent-object 404
    # assertion on an explicitly initialized legacy fixture, not a missing DB.
    assert not client.app.state.business_store.path.exists()
    with client.app.state.business_store.transaction():
        pass
    assert client.get("/api/v1/reports/" + "0" * 32).status_code == 404
    assert client.get("/api/v1/reports?limit=1000").status_code == 422
