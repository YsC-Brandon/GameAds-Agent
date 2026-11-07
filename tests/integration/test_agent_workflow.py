"""Scripted model decisions drive real LangGraph nodes and real local data tools.

These tests validate orchestration, not live-model tool selection accuracy.
"""

import copy
import json

import pytest

from app.agent.contracts import ChatRequest, ModelMessage
from app.agent.provider import ModelError
from app.agent.service import AgentService
from app.core.config import Settings

DATES = {"start_date": "2026-10-24", "end_date": "2026-10-30"}


def call(name, args, identity="call_1"):
    return {
        "id": identity,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args) if not isinstance(args, str) else args,
        },
    }


def message(*calls, content=None):
    return ModelMessage(content=content, tool_calls=list(calls))


def plan(tool, task="analysis", dates=DATES):
    return message(
        call(
            "submit_plan",
            {
                "task_type": task,
                "date_range": dates,
                "steps": [tool] if isinstance(tool, str) else tool,
            },
            "plan_1",
        )
    )


def finish(*ids, path=None, kinds=None, identity="finish_1"):
    return message(
        call(
            "finish_analysis",
            {
                "references": [{"call_id": identity, "path": path or []} for identity in ids],
                "interpretations": [
                    {"call_id": identity, "kind": kind}
                    for identity, kind in zip(ids, kinds or [], strict=False)
                ],
            },
            identity,
        )
    )


class ScriptedProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, tools, tool_choice="auto"):
        self.requests.append(copy.deepcopy((messages, tools, tool_choice)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def run(provider, query="分析 2026-10-24 到 2026-10-30", **kwargs):
    return AgentService(Settings(_env_file=None, **kwargs), provider=provider).run(
        ChatRequest(query=query)
    )


@pytest.mark.parametrize(
    "tool,task,query,args",
    [
        ("list_campaigns", "list", "列出广告计划", {}),
        (
            "query_campaigns",
            "query",
            "查询 2026-10-24 到 2026-10-30 的 C001",
            {**DATES, "campaign_ids": ["C001"]},
        ),
        (
            "get_campaign_detail",
            "detail",
            "查看 2026-10-24 到 2026-10-30 C001 明细",
            {**DATES, "campaign_id": "C001"},
        ),
        (
            "summarize_campaign_performance",
            "summary",
            "汇总 2026-10-24 到 2026-10-30 五项指标",
            DATES,
        ),
        (
            "compare_campaigns",
            "comparison",
            "找出 2026-10-24 到 2026-10-30 ROAS 最低三个",
            {**DATES, "metric": "roas", "top_n": 3, "order": "asc"},
        ),
        ("detect_campaign_anomalies", "anomalies", "检查 2026-10-24 到 2026-10-30 的异常", DATES),
    ],
)
def test_six_task_categories_use_real_tools(tool, task, query, args):
    kind = {
        "list": "campaign_list",
        "query": "records",
        "detail": "records",
        "summary": "performance",
        "comparison": "ranking",
        "anomalies": "anomaly_screening",
    }[task]
    provider = ScriptedProvider(
        plan(tool, task, None if task == "list" else DATES),
        message(call(tool, args)),
        finish("call_1", kinds=[kind]),
    )
    result = run(provider, query)
    assert result.status == "completed", result
    assert result.insights and "[call_1]" in result.insights[0]
    assert result.tool_calls[0].tool_name == tool
    assert result.tool_calls[0].status == "success"
    assert result.tool_calls[0].duration_ms >= 0
    assert result.evidence[0].value == result.tool_results["call_1"]
    assert result.steps == [
        "understand_request",
        "plan_task",
        "route_tools",
        "execute_tools",
        "route_tools",
        "validate_results",
        "generate_insight",
    ]
    returned = provider.requests[2][0][-1]
    assert returned["role"] == "tool" and returned["tool_call_id"] == "call_1"
    assert json.loads(returned["content"]) == result.tool_results["call_1"]
    assert "data_path" not in json.dumps(provider.requests[1][1])


def test_multiple_rounds_and_exact_metric_evidence():
    provider = ScriptedProvider(
        plan(["list_campaigns", "summarize_campaign_performance"]),
        message(call("list_campaigns", {}, "list_1")),
        message(call("summarize_campaign_performance", DATES, "metrics_1")),
        finish("list_1", "metrics_1", kinds=["campaign_list", "performance"]),
    )
    result = run(provider)
    assert result.status == "completed"
    assert len(result.tool_calls) == 2
    assert result.tool_results["metrics_1"]["overall"]["row_count"] == 56
    assert len(provider.requests) == 4


def test_multiple_tool_calls_in_one_response():
    provider = ScriptedProvider(
        plan(["list_campaigns", "query_campaigns"]),
        message(call("list_campaigns", {}, "a"), call("query_campaigns", DATES, "b")),
        finish("a", "b", kinds=["campaign_list", "records"]),
    )
    result = run(provider)
    assert result.status == "completed"
    assert [msg["tool_call_id"] for msg in provider.requests[-1][0][-2:]] == ["a", "b"]


@pytest.mark.parametrize(
    "args,code",
    [
        ({**DATES, "data_path": "private.csv"}, "invalid_tool_arguments"),
        ({**DATES, "limit": 101}, "invalid_tool_arguments"),
        ({**DATES, "start_date": "bad"}, "invalid_tool_arguments"),
        ({**DATES, "start_date": "2026-10-23"}, "tool_date_mismatch"),
        ("{not-json", "invalid_tool_arguments"),
    ],
)
def test_invalid_arguments_returned_to_model_and_can_be_repaired(args, code):
    provider = ScriptedProvider(
        plan("query_campaigns"),
        message(call("query_campaigns", args, "bad")),
        message(call("query_campaigns", DATES, "good")),
        finish("good", kinds=["records"]),
    )
    result = run(provider)
    assert result.status == "completed"
    assert result.tool_calls[0].error_code == code
    assert json.loads(provider.requests[2][0][-1]["content"])["code"] == code


@pytest.mark.parametrize(
    "tool", ["run_shell", "simulate_budget_changes", "generate_campaign_report"]
)
def test_unregistered_tools_never_execute(tool):
    provider = ScriptedProvider(plan("query_campaigns"), message(call(tool, {})), finish("call_1"))
    result = run(provider)
    assert result.status == "failed"
    assert result.tool_calls[0].error_code == "tool_not_allowed"
    assert result.errors == ["invalid_evidence"]


def test_unknown_campaign_error_is_not_success():
    provider = ScriptedProvider(
        plan("query_campaigns"),
        message(call("query_campaigns", {**DATES, "campaign_ids": ["missing"]})),
        finish("call_1"),
    )
    result = run(provider)
    assert result.status == "failed"
    assert result.tool_calls[0].error_code == "campaign_not_found"


@pytest.mark.parametrize(
    "ref,path", [("invented", []), ("call_1", ["does_not_exist"]), ("call_1", ["campaigns", "-1"])]
)
def test_fabricated_evidence_is_blocked(ref, path):
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {})),
        finish(ref, path=path),
    )
    assert run(provider, agent_max_rounds=2).errors == ["invalid_evidence"]


def test_freely_invented_answer_never_displayed():
    provider = ScriptedProvider(
        plan("query_campaigns"), message(content="ROAS is 99999; synthetic-secret")
    )
    result = run(provider)
    assert result.errors == ["ungrounded_model_response"]
    assert "99999" not in result.model_dump_json()


def test_no_tool_results_cannot_finish():
    assert run(ScriptedProvider(plan("query_campaigns"), finish("fake"))).errors == [
        "invalid_evidence"
    ]


def test_missing_plan_capability_cannot_finish():
    provider = ScriptedProvider(
        plan(["list_campaigns", "query_campaigns"]),
        message(call("list_campaigns", {})),
        finish("call_1"),
    )
    result = run(provider, agent_max_rounds=2)
    assert result.errors == ["invalid_evidence"]
    assert "evidence_plan_incomplete" in result.final_answer


@pytest.mark.parametrize("kind", ["needs_clarification", "unsupported"])
def test_non_executable_tasks_stop_before_tools(kind):
    result = run(ScriptedProvider(plan([], kind, None)), query="请处理这个请求")
    assert result.status == kind and not result.tool_calls


def test_model_cannot_invent_dates_for_relative_request():
    result = run(ScriptedProvider(plan("query_campaigns")), query="查询最近一周")
    assert result.status == "needs_clarification"
    assert not result.tool_calls


def test_requests_do_not_reuse_dates_or_messages():
    provider = ScriptedProvider(
        plan([], "unsupported", None), plan([], "needs_clarification", None)
    )
    service = AgentService(Settings(_env_file=None), provider=provider)
    service.run(ChatRequest(query="2026-10-24 到 2026-10-30 生成报告"))
    service.run(ChatRequest(query="查询最近一周"))
    assert "2026-10-24" not in json.dumps(provider.requests[1][0])


def test_loop_and_call_limits():
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None), message(call("list_campaigns", {}))
    )
    assert run(provider, agent_max_rounds=1).errors == ["model_round_limit"]
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {}, "a"), call("list_campaigns", {}, "b")),
    )
    assert run(provider, agent_max_tool_calls=1).errors == ["tool_call_limit"]


def test_reused_call_id_is_rejected():
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None), message(call("list_campaigns", {}, "plan_1"))
    )
    assert run(provider).errors == ["duplicate_tool_call_id"]


def test_model_failure_retains_prior_tool_trace():
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {})),
        ModelError("model_timeout"),
    )
    result = run(provider)
    assert result.status == "model_unavailable"
    assert len(result.tool_calls) == 1
    assert result.errors == ["model_timeout"]


def test_key_redacted_before_provider_and_in_output():
    provider = ScriptedProvider(
        plan("query_campaigns"),
        message(call("query_campaigns", {**DATES, "campaign_ids": ["synthetic-secret"]})),
        finish("call_1"),
    )
    result = run(
        provider, query="2026-10-24 到 2026-10-30 synthetic-secret", llm_api_key="synthetic-secret"
    )
    assert "synthetic-secret" not in json.dumps(provider.requests[0][0])
    assert "synthetic-secret" not in result.model_dump_json()


def test_no_data_remains_insufficient():
    dates = {"start_date": "2020-01-01", "end_date": "2020-01-07"}
    provider = ScriptedProvider(
        plan("query_campaigns", dates=dates),
        message(call("query_campaigns", dates)),
        finish("call_1", kinds=["records"]),
    )
    assert run(provider, query="查询 2020-01-01 到 2020-01-07").status == "insufficient_evidence"


def test_invalid_plan_and_mixed_finish_are_blocked():
    provider = ScriptedProvider(message(content="an invented plan"))
    assert run(provider).errors == ["invalid_plan_response"]
    provider = ScriptedProvider(plan("query_campaigns", dates=None))
    assert run(provider).errors == ["invalid_plan"]
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {}, "a"), call("finish_analysis", {}, "b")),
    )
    assert run(provider).errors == ["mixed_finish_and_tool_calls"]


def test_missing_dataset_failure_retains_trace(tmp_path):
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {})),
        finish("call_1"),
    )
    result = AgentService(
        Settings(_env_file=None),
        provider=provider,
        data_path=tmp_path / "private.csv",
    ).run(ChatRequest(query="列出计划"))
    assert result.tool_calls[0].error_code == "data_source_error"
    assert result.status == "failed"
    assert "private.csv" not in result.model_dump_json()


@pytest.mark.parametrize("identity", ["call_1", "call.0:summary/1", "x" * 256])
def test_http_adapter_graph_and_tools_complete_chain(identity):
    import httpx

    from app.agent.provider import OpenAICompatibleProvider

    replies = [
        plan("summarize_campaign_performance", "summary"),
        message(call("summarize_campaign_performance", DATES, identity)),
        finish(identity, path=["overall", "totals", "spend"], kinds=["performance"]),
    ]
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        reply = replies.pop(0)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": reply.wire(),
                    }
                ]
            },
        )

    settings = Settings(_env_file=None, llm_api_key="synthetic-secret", llm_model="mock")
    provider = OpenAICompatibleProvider(settings, transport=httpx.MockTransport(handle))
    result = AgentService(settings, provider=provider).run(
        ChatRequest(query="汇总 2026-10-24 到 2026-10-30")
    )
    assert result.status == "completed"
    total = result.tool_results[identity]["overall"]["totals"]["spend"]
    assert result.evidence[0].value == total
    assert total in result.final_answer
    assert requests[2]["messages"][-1]["tool_call_id"] == identity
    assert json.loads(requests[2]["messages"][-1]["content"])["overall"]["totals"]["spend"] == total


def test_internal_exception_is_redacted():
    result = run(ScriptedProvider(RuntimeError("sensitive-path-secret")))
    assert result.errors == ["agent_internal_error"]
    assert "sensitive-path-secret" not in result.model_dump_json()


def test_interpretation_cannot_misrepresent_evidence_type():
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {})),
        finish("call_1", kinds=["ranking"]),
    )
    assert run(provider, agent_max_rounds=2).errors == ["invalid_evidence"]


def test_missing_interpretation_cannot_complete():
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {})),
        finish("call_1"),
    )
    assert run(provider, agent_max_rounds=2).errors == ["invalid_evidence"]


@pytest.mark.parametrize(
    "arguments,reason",
    [
        ({"references": []}, "finish_schema_invalid"),
        ({"references": [{"call_id": "invented"}]}, "evidence_call_not_successful"),
        (
            {"references": [{"call_id": "call_1", "path": ["missing"]}]},
            "evidence_path_invalid",
        ),
        (
            {"references": [{"call_id": "call_1", "path": ["campaigns", "-1"]}]},
            "evidence_path_invalid",
        ),
        (
            {
                "references": [{"call_id": "call_1"}],
                "interpretations": [{"call_id": "invented", "kind": "campaign_list"}],
            },
            "interpretation_not_referenced",
        ),
        (
            {
                "references": [{"call_id": "call_1"}],
                "interpretations": [{"call_id": "call_1", "kind": "ranking"}],
            },
            "interpretation_tool_mismatch",
        ),
        ({"references": [{"call_id": "call_1"}]}, "interpretation_plan_incomplete"),
    ],
)
def test_invalid_finish_can_repair_without_reexecuting_tools(arguments, reason):
    corrected = finish("call_1", kinds=["campaign_list"], identity="finish_2")
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {})),
        message(call("finish_analysis", arguments, "finish_1")),
        corrected,
    )
    result = run(provider, query="列出广告计划")
    assert result.status == "completed"
    assert result.errors == []
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].status == "success"
    assert result.steps.count("validate_results") == 2
    assert result.evidence[0].value == result.tool_results["call_1"]
    feedback = provider.requests[3][0][-1]
    assert feedback["role"] == "tool"
    assert feedback["tool_call_id"] == "finish_1"
    assert json.loads(feedback["content"])["reason"] == reason
    description = provider.requests[3][1][-1]["function"]["description"]
    assert '"call_id": "call_1"' in description
    assert '"kind": "campaign_list"' in description


@pytest.mark.parametrize("rounds", [3, 6, 12])
def test_invalid_finish_repair_is_bounded_and_keeps_evidence_unpublished(rounds):
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {})),
        *(finish("call_1", identity=f"finish_{index}") for index in range(rounds - 1)),
    )
    result = run(provider, agent_max_rounds=rounds)
    assert len(provider.requests) == rounds + 1  # Planning + bounded execution rounds.
    assert result.errors == ["invalid_evidence"]
    assert "interpretation_plan_incomplete" in result.final_answer
    assert result.evidence == [] and result.insights == []
    assert result.tool_results["call_1"]["campaign_count"] == 8


def test_repair_cannot_reuse_finish_call_id():
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {})),
        finish("call_1"),
        finish("call_1", kinds=["campaign_list"]),
    )
    assert run(provider).errors == ["duplicate_tool_call_id"]


def test_repair_handles_multiple_planned_capabilities_and_partial_coverage():
    provider = ScriptedProvider(
        plan(["list_campaigns", "query_campaigns"]),
        message(call("list_campaigns", {}, "list_1"), call("query_campaigns", DATES, "query_1")),
        finish("list_1", kinds=["campaign_list"]),
        finish("list_1", "query_1", kinds=["campaign_list", "records"], identity="finish_2"),
    )
    result = run(provider)
    assert result.status == "completed"
    assert len(result.tool_calls) == 2
    assert {ref.tool_name for ref in result.evidence} == {"list_campaigns", "query_campaigns"}
    feedback = json.loads(provider.requests[3][0][-1]["content"])
    assert feedback["reason"] == "evidence_plan_incomplete"


def test_repair_failure_does_not_expose_model_arguments():
    provider = ScriptedProvider(
        plan("list_campaigns", "list", None),
        message(call("list_campaigns", {})),
        message(call("finish_analysis", {"private": "sensitive-model-prose"}, "finish_1")),
    )
    result = run(provider, agent_max_rounds=2)
    assert result.errors == ["invalid_evidence"]
    assert "finish_schema_invalid" in result.final_answer
    assert "sensitive-model-prose" not in result.model_dump_json()


def test_ranking_insight_matches_actual_order_and_values():
    provider = ScriptedProvider(
        plan("compare_campaigns"),
        message(call("compare_campaigns", {**DATES, "metric": "roas", "order": "asc", "top_n": 3})),
        finish("call_1", path=["rankings"], kinds=["ranking"]),
    )
    result = run(provider)
    assert result.status == "completed"
    text = result.insights[0]
    rankings = result.tool_results["call_1"]["rankings"]
    positions = [text.index(row["campaign_id"]) for row in rankings]
    assert positions == sorted(positions)
    for row in rankings:
        assert str(row["metrics"]["roas"]["value"]) in text
