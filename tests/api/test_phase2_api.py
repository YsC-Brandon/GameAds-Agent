"""In-process HTTP tests with real local data and no network model dependency."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent.contracts import ModelMessage
from app.agent.provider import OpenAICompatibleProvider
from app.agent.service import AgentService
from app.core.config import Settings
from app.main import create_app, get_agent_service, get_settings


def client_for(data_path=None):
    app = create_app(**({"data_path": data_path} if data_path is not None else {}))
    settings = Settings(_env_file=None)
    app.dependency_overrides[get_settings] = lambda: settings
    return app, TestClient(app, raise_server_exceptions=False)


def test_no_key_health_data_and_swagger():
    app, client = client_for()
    assert client.get("/health").json() == {"status": "ok", "model_configured": False}
    data = client.get("/api/v1/campaigns").json()
    assert data["status"] == "ok" and data["data"]["campaign_count"] == 8
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    result = client.post("/api/v1/chat", json={"query": "列出计划"})
    assert result.status_code == 503
    assert result.json()["errors"] == ["model_not_configured"]
    assert not result.json()["tool_calls"]


def test_campaign_query_and_date_validation():
    _, client = client_for()
    result = client.get(
        "/api/v1/campaigns",
        params={
            "start_date": "2026-10-01",
            "end_date": "2026-10-07",
            "campaign_ids": "C001",
            "limit": 2,
        },
    )
    assert result.status_code == 200
    assert result.json()["data"]["total_records"] == 7
    assert result.json()["data"]["returned_records"] == 2
    assert client.get("/api/v1/campaigns", params={"start_date": "2026-10-01"}).status_code == 422
    assert client.get("/api/v1/campaigns", params={"limit": 1001}).status_code == 422


def test_errors_do_not_reflect_input_or_paths(tmp_path):
    _, client = client_for(tmp_path / "sensitive.csv")
    assert client.get("/health").status_code == 200
    response = client.get("/api/v1/campaigns")
    assert response.status_code == 400
    assert "sensitive.csv" not in response.text
    response = client.post("/api/v1/chat", json={"query": "hello", "api_key": "synthetic-secret"})
    assert response.status_code == 422
    assert "synthetic-secret" not in response.text


def test_unknown_campaign_and_unknown_route():
    _, client = client_for()
    response = client.get(
        "/api/v1/campaigns",
        params={
            "start_date": "2026-10-01",
            "end_date": "2026-10-07",
            "campaign_ids": "missing",
        },
    )
    assert response.status_code == 404
    assert response.json()["code"] == "campaign_not_found"
    assert client.get("/api/v1/tasks/missing").status_code == 404


def test_chat_injected_provider_uses_service():
    class ClarifyingProvider:
        def complete(self, messages, tools, tool_choice="auto"):
            return ModelMessage(
                tool_calls=[
                    {
                        "id": "plan",
                        "type": "function",
                        "function": {
                            "name": "submit_plan",
                            "arguments": '{"task_type":"needs_clarification","steps":[]}',
                        },
                    }
                ]
            )

    app, client = client_for()
    app.dependency_overrides[get_agent_service] = lambda: AgentService(
        Settings(_env_file=None),
        provider=ClarifyingProvider(),
    )
    result = client.post("/api/v1/chat", json={"query": "查询最近一周"})
    assert result.status_code == 200
    assert result.json()["status"] == "needs_clarification"
    assert result.json()["steps"] == ["understand_request"]


def test_example_agent_limits_load_from_environment(monkeypatch):
    monkeypatch.setenv("GAMEADS_AGENT_MAX_ROUNDS", "4")
    monkeypatch.setenv("GAMEADS_AGENT_MAX_TOOL_CALLS", "8")
    settings = Settings(_env_file=None)
    assert settings.agent_max_rounds == 4 and settings.agent_max_tool_calls == 8


@pytest.mark.parametrize(
    "upstream_status,code,explanation",
    [
        (400, "model_invalid_request", "HTTP 400"),
        (402, "model_insufficient_balance", "余额不足"),
        (404, "model_endpoint_or_model_not_found", "HTTP 404"),
        (422, "model_invalid_parameters", "HTTP 422"),
        (503, "model_upstream_error", "上游故障"),
    ],
)
def test_model_http_errors_have_safe_actionable_explanations(upstream_status, code, explanation):
    app, client = client_for()
    settings = Settings(_env_file=None, llm_model="mock", llm_api_key="synthetic-secret")
    provider = OpenAICompatibleProvider(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(upstream_status, text="private-upstream-body")
        ),
    )
    app.dependency_overrides[get_agent_service] = lambda: AgentService(settings, provider=provider)
    result = client.post("/api/v1/chat", json={"query": "列出广告计划"})
    assert result.status_code == 503
    body = result.json()
    assert body["errors"] == [code]
    assert explanation in body["final_answer"]
    assert body["plan"] is None and not body["tool_calls"]
    assert "private-upstream-body" not in result.text
    assert "synthetic-secret" not in result.text


def test_protocol_diagnostic_reaches_http_without_rejected_values():
    app, client = client_for()
    settings = Settings(_env_file=None, llm_model="mock", llm_api_key="synthetic-secret")
    provider = OpenAICompatibleProvider(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"finish_reason": "stop", "message": {"role": "private-value"}}]},
            )
        ),
    )
    app.dependency_overrides[get_agent_service] = lambda: AgentService(settings, provider=provider)
    result = client.post("/api/v1/chat", json={"query": "列出计划"})
    assert result.status_code == 503
    assert result.json()["errors"] == ["model_invalid_message"]
    assert "message" in result.json()["final_answer"]
    assert "private-value" not in result.text
    assert not result.json()["tool_calls"]


@pytest.mark.parametrize("rounds,expected_status", [(2, 502), (3, 200)])
def test_finish_validation_diagnostic_and_repair_through_http(rounds, expected_status):
    replies = [
        ("plan_1", "submit_plan", {"task_type": "list", "steps": ["list_campaigns"]}),
        ("call_00_TggozqUoCZ536u9V4NRP0226", "list_campaigns", {}),
        (
            "finish_1",
            "finish_analysis",
            {"references": [{"call_id": "call_00_TggozqUoCZ536u9V4NRP0226", "path": []}]},
        ),
        (
            "finish_2",
            "finish_analysis",
            {
                "references": [{"call_id": "call_00_TggozqUoCZ536u9V4NRP0226", "path": []}],
                "interpretations": [
                    {"call_id": "call_00_TggozqUoCZ536u9V4NRP0226", "kind": "campaign_list"}
                ],
            },
        ),
    ]
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        identity, name, arguments = replies.pop(0)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": identity,
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        },
                    }
                ]
            },
        )

    app, client = client_for()
    settings = Settings(
        _env_file=None,
        llm_model="mock",
        llm_api_key="synthetic-secret",
        llm_base_url="https://api.deepseek.com",
        agent_max_rounds=rounds,
    )
    provider = OpenAICompatibleProvider(settings, transport=httpx.MockTransport(handle))
    app.dependency_overrides[get_agent_service] = lambda: AgentService(settings, provider=provider)
    response = client.post("/api/v1/chat", json={"query": "列出广告计划"})
    assert response.status_code == expected_status
    body = response.json()
    assert len(body["tool_calls"]) == 1
    assert body["tool_calls"][0]["status"] == "success"
    assert len(requests) == rounds + 1
    assert all(request["thinking"] == {"type": "disabled"} for request in requests)
    assert "synthetic-secret" not in response.text
    if expected_status == 200:
        assert body["status"] == "completed"
        assert body["evidence"][0]["value"]["campaign_count"] == 8
        assert "8 个广告计划" in body["insights"][0]
        assert requests[-1]["messages"][-1]["tool_call_id"] == "finish_1"
    else:
        assert body["errors"] == ["invalid_evidence"]
        assert "interpretation_plan_incomplete" in body["final_answer"]
        assert body["evidence"] == [] and body["insights"] == []
