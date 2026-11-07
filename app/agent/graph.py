"""A real LangGraph loop, bounded by model rounds, tool count, and context size."""

import json
import re
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.agent.contracts import (
    AgentResponse,
    ChatRequest,
    Evidence,
    FinishRequest,
    ModelMessage,
    Plan,
    ToolTrace,
)
from app.agent.insights import INTERPRETATION_TOOLS, render_interpretation
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.provider import ModelError, ModelProvider
from app.agent.registry import ToolRegistry, function_schema


class AgentState(TypedDict):
    request_id: str
    query: str
    messages: list[dict]
    plan: Plan | None
    pending: ModelMessage | None
    traces: list[ToolTrace]
    results: dict
    evidence: list[Evidence]
    insights: list[str]
    rounds: int
    seen_ids: list[str]
    steps: list[str]
    errors: list[str]
    status: str
    final_answer: str


def _step(state: AgentState, name: str) -> list[str]:
    return [*state["steps"], name]


def _error(state: AgentState, code: str, *, model=False) -> dict:
    reason = {
        "model_not_configured": "模型未配置，请在本地设置模型名称和 API Key。",
        "model_authentication_failed": "模型认证失败，请检查本地凭据和访问权限。",
        "model_invalid_request": "模型服务拒绝请求（HTTP 400），请检查工具调用协议兼容性。",
        "model_insufficient_balance": "模型账号余额不足（HTTP 402），请到提供商控制台检查余额。",
        "model_endpoint_or_model_not_found": (
            "模型接口或模型不存在（HTTP 404），请检查地址和模型名称。"
        ),
        "model_invalid_parameters": "模型服务拒绝参数（HTTP 422），请检查模型支持的请求参数。",
        "model_upstream_error": "模型服务发生上游故障（HTTP 500/502/503/504），请稍后重试。",
        "model_rate_limited": "模型服务限流，请稍后重试。",
        "model_timeout": "模型请求超时，请检查服务状态或调整超时配置。",
        "model_network_error": "无法连接模型服务，请检查地址和网络。",
        "model_http_error": "模型服务返回 HTTP 错误，请检查提供商服务状态。",
        "model_protocol_error": (
            "模型响应解析失败；不一定是模型不支持工具调用，请检查适配器兼容性。"
        ),
        "model_invalid_json": "模型接口返回 HTTP 200，但响应不是有效 JSON 或文本编码不合法。",
        "model_invalid_response_structure": "模型响应缺少有效的 choices 结构。",
        "model_empty_choices": "模型接口返回了空 choices，未提供可解析的回答。",
        "model_invalid_message": "模型 message 的角色、正文类型或长度未通过校验。",
        "model_invalid_tool_calls": "模型工具调用结构、类型、函数名称或调用数量未通过校验。",
        "model_invalid_tool_call_id": "模型工具调用 ID 为空、过长或包含不支持的字符。",
        "model_invalid_tool_arguments": (
            "模型 function.arguments 必须是长度不超过 32000 的 JSON 字符串。"
        ),
        "model_incomplete_response": "模型生成被截断、过滤或中断，未返回完整响应。",
        "invalid_evidence": "模型引用或解释缺少有效的工具证据。",
        "ungrounded_model_response": "模型未按工具协议提供分析，无法验证其回答。",
    }.get(code, "本次执行未能通过校验，请检查配置、任务范围或工具轨迹。")
    return {
        "status": "model_unavailable" if model else "failed",
        "errors": [*state["errors"], code],
        "final_answer": f"任务未完成（{code}）。{reason}",
    }


class AgentGraph:
    def __init__(self, provider: ModelProvider, registry: ToolRegistry):
        self.provider = provider
        self.registry = registry
        self.settings = registry.settings
        graph = StateGraph(AgentState)
        graph.add_node("understand_request", self.understand_request)
        graph.add_node("plan_task", self.plan_task)
        graph.add_node("route_tools", self.route_tools)
        graph.add_node("execute_tools", self.execute_tools)
        graph.add_node("validate_results", self.validate_results)
        graph.add_node("generate_insight", self.generate_insight)
        graph.add_edge(START, "understand_request")
        graph.add_conditional_edges(
            "understand_request",
            self.after_plan,
            {
                "plan": "plan_task",
                "end": END,
            },
        )
        graph.add_edge("plan_task", "route_tools")
        graph.add_conditional_edges(
            "route_tools",
            self.after_model,
            {
                "execute": "execute_tools",
                "validate": "validate_results",
                "end": END,
            },
        )
        graph.add_edge("execute_tools", "route_tools")
        graph.add_conditional_edges(
            "validate_results",
            self.after_validation,
            {
                "render": "generate_insight",
                "repair": "route_tools",
                "end": END,
            },
        )
        graph.add_edge("generate_insight", END)
        self.graph = graph.compile()

    def _complete(self, messages, tools, choice="auto") -> ModelMessage:
        if len(json.dumps(messages).encode()) > self.settings.agent_max_context_bytes:
            raise ModelError("context_limit")
        return self.provider.complete(messages, tools, choice)

    def understand_request(self, state: AgentState) -> dict:
        steps = _step(state, "understand_request")
        schema = function_schema("submit_plan", "Submit a validated read-only task plan.", Plan)
        try:
            message = self._complete(
                state["messages"],
                [schema],
                {"type": "function", "function": {"name": "submit_plan"}},
            )
            if len(message.tool_calls) != 1 or message.tool_calls[0].function.name != "submit_plan":
                return {"steps": steps, **_error(state, "invalid_plan_response")}
            call = message.tool_calls[0]
            plan = Plan.model_validate_json(call.function.arguments)
            if plan.date_range is not None:
                dates = re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", state["query"])
                wanted = [
                    plan.date_range.start_date.isoformat(),
                    plan.date_range.end_date.isoformat(),
                ]
                if not all(day in dates for day in wanted):
                    return {
                        "steps": steps,
                        "status": "needs_clarification",
                        "final_answer": (
                            "请明确提供 YYYY-MM-DD 格式的起止日期；不会使用模型猜测的日期。"
                        ),
                    }
            messages = [
                *state["messages"],
                message.wire(),
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(
                        {"status": "accepted", "plan": plan.model_dump(mode="json")}
                    ),
                },
            ]
            result = {"steps": steps, "plan": plan, "messages": messages, "seen_ids": [call.id]}
            if plan.task_type == "needs_clarification":
                result.update(
                    status="needs_clarification",
                    final_answer="请明确分析的起止日期、计划及任务要求。",
                )
            elif plan.task_type == "unsupported":
                result.update(
                    status="unsupported",
                    final_answer="当前仅支持只读查询、汇总、排名和异常筛查；预算、审批和报告未实现。",
                )
            return result
        except ModelError as exc:
            return {"steps": steps, **_error(state, exc.code, model=True)}
        except ValidationError:
            return {"steps": steps, **_error(state, "invalid_plan")}

    @staticmethod
    def after_plan(state):
        return "plan" if state["status"] == "running" else "end"

    def plan_task(self, state: AgentState) -> dict:
        # The plan is structured by the model; deterministic validation enforces capabilities.
        if not set(state["plan"].steps).issubset(self.registry.tools):
            return _error(state, "unsupported_plan")
        return {"steps": _step(state, "plan_task")}

    def route_tools(self, state: AgentState) -> dict:
        steps = _step(state, "route_tools")
        if state["status"] != "running":
            return {"steps": steps}
        if state["rounds"] >= self.settings.agent_max_rounds:
            return {"steps": steps, **_error(state, "model_round_limit")}
        tools = self.registry.schemas() + [
            function_schema(
                "finish_analysis",
                "Select evidence references AND matching interpretations for every planned tool. "
                "Copy an actual successful call_id, not a campaign ID or finish call ID. "
                "Use path=[] for a complete result. Successful candidates: "
                + json.dumps(
                    [
                        {
                            "call_id": trace.call_id,
                            "tool_name": trace.tool_name,
                            "kind": next(
                                kind
                                for kind, names in INTERPRETATION_TOOLS.items()
                                if trace.tool_name in names
                            ),
                        }
                        for trace in state["traces"]
                        if trace.status == "success"
                    ]
                ),
                FinishRequest,
            )
        ]
        try:
            message = self._complete(state["messages"], tools)
        except ModelError as exc:
            return {"steps": steps, **_error(state, exc.code, model=True)}
        if not message.tool_calls:
            return {"steps": steps, **_error(state, "ungrounded_model_response")}
        ids = [call.id for call in message.tool_calls]
        if set(ids).intersection(state["seen_ids"]):
            return {"steps": steps, **_error(state, "duplicate_tool_call_id")}
        finish = [call for call in message.tool_calls if call.function.name == "finish_analysis"]
        if finish and len(message.tool_calls) != 1:
            return {"steps": steps, **_error(state, "mixed_finish_and_tool_calls")}
        if not finish and len(state["traces"]) + len(ids) > self.settings.agent_max_tool_calls:
            return {"steps": steps, **_error(state, "tool_call_limit")}
        return {
            "steps": steps,
            "pending": message,
            "rounds": state["rounds"] + 1,
            "messages": [*state["messages"], message.wire()],
            "seen_ids": [*state["seen_ids"], *ids],
        }

    @staticmethod
    def after_model(state):
        if state["status"] != "running":
            return "end"
        return (
            "validate"
            if state["pending"].tool_calls[0].function.name == "finish_analysis"
            else "execute"
        )

    def execute_tools(self, state: AgentState) -> dict:
        traces, messages, results = (
            list(state["traces"]),
            list(state["messages"]),
            dict(state["results"]),
        )
        for call in state["pending"].tool_calls:
            trace, result = self.registry.execute(
                call,
                allowed=state["plan"].steps,
                period=state["plan"].date_range,
            )
            traces.append(trace)
            results[call.id] = result
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
            )
        return {
            "steps": _step(state, "execute_tools"),
            "traces": traces,
            "results": results,
            "messages": messages,
        }

    def validate_results(self, state: AgentState) -> dict:
        steps = _step(state, "validate_results")
        issue = "finish_schema_invalid"
        try:
            finish = FinishRequest.model_validate_json(
                state["pending"].tool_calls[0].function.arguments
            )
            successful = {
                trace.call_id: trace for trace in state["traces"] if trace.status == "success"
            }
            selected = []
            for ref in finish.references:
                issue = "evidence_call_not_successful"
                trace = successful[ref.call_id]
                value = state["results"][ref.call_id]
                issue = "evidence_path_invalid"
                for segment in ref.path:
                    if isinstance(value, list):
                        if not segment.isdecimal():
                            raise ValueError("Array index required.")
                        value = value[int(segment)]
                    else:
                        value = value[segment]
                selected.append(
                    Evidence(
                        call_id=ref.call_id,
                        tool_name=trace.tool_name,
                        path=ref.path,
                        value=value,
                    )
                )
            issue = "evidence_plan_incomplete"
            if not set(state["plan"].steps).issubset({item.tool_name for item in selected}):
                raise ValueError("Incomplete evidence for plan.")
            referenced = {item.call_id for item in selected}
            insights = []
            for item in finish.interpretations:
                issue = "interpretation_not_referenced"
                if item.call_id not in referenced:
                    raise ValueError("Interpretation must cite selected evidence.")
                issue = "interpretation_tool_mismatch"
                if successful[item.call_id].tool_name not in INTERPRETATION_TOOLS[item.kind]:
                    raise ValueError("Interpretation must match tool.")
                issue = "interpretation_result_invalid"
                insights.append(
                    render_interpretation(
                        item, successful[item.call_id], state["results"][item.call_id]
                    )
                )
            interpreted_tools = {
                successful[item.call_id].tool_name for item in finish.interpretations
            }
            issue = "interpretation_plan_incomplete"
            if not set(state["plan"].steps).issubset(interpreted_tools):
                raise ValueError("Missing interpretation for planned capability.")
        except (ValidationError, KeyError, IndexError, TypeError, ValueError):
            explanations = {
                "finish_schema_invalid": "完成参数格式错误；需要 references 和 interpretations。",
                "evidence_call_not_successful": "引用的 call_id 不属于成功执行的业务工具。",
                "evidence_path_invalid": "引用的字段路径不存在或数组下标无效；可用空路径 []。",
                "evidence_plan_incomplete": "证据引用未覆盖所有计划工具。",
                "interpretation_not_referenced": "解释引用的 call_id 未出现在 references 中。",
                "interpretation_tool_mismatch": "解释类型与工具不匹配，例如列表需 campaign_list。",
                "interpretation_result_invalid": "实际工具结果无法支持指定解释。",
                "interpretation_plan_incomplete": "缺少解释，或解释未覆盖所有计划工具。",
            }
            if (
                any(trace.status == "success" for trace in state["traces"])
                and state["rounds"] < self.settings.agent_max_rounds
            ):
                call = state["pending"].tool_calls[0]
                return {
                    "steps": steps,
                    "pending": None,
                    "messages": [
                        *state["messages"],
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(
                                {
                                    "status": "error",
                                    "code": "invalid_evidence",
                                    "reason": issue,
                                    "message": explanations[issue],
                                    "instruction": (
                                        "Repair finish_analysis using successful candidates "
                                        "in its schema description. Use a new tool call ID. Do not "
                                        "rerun successful business tools or invent evidence."
                                    ),
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                }
            failure = _error(state, "invalid_evidence")
            failure["final_answer"] += f" 原因：{issue}。{explanations[issue]}"
            return {"steps": steps, **failure}
        # Keep successful results, including coverage and warnings, in the public response.
        return {"steps": steps, "evidence": selected, "insights": insights}

    @staticmethod
    def after_validation(state):
        if state["status"] != "running":
            return "end"
        return "repair" if state["pending"] is None else "render"

    def generate_insight(self, state: AgentState) -> dict:
        referenced = {ref.call_id for ref in state["evidence"]}
        insufficient = any(
            state["results"][key].get("status") in {"no_data", "no_computable_values"}
            for key in referenced
        )
        lines = [
            "以下为实际工具证据（合成数据，不代表真实广告效果）。",
            "完整工具结果包含样本量、统计区间、不可计算状态和质量警告，请一并审阅。",
            "异常筛查为启发式规则，不代表因果关系；证据不足不等同于无异常。",
        ]
        lines.extend(state["insights"])
        for ref in state["evidence"]:
            lines.append(
                f"[{ref.call_id}] {ref.tool_name} /{'/'.join(ref.path)}: "
                + json.dumps(ref.value, ensure_ascii=False)
            )
        if insufficient:
            lines.insert(0, "数据不足，不能据此给出完整分析结论。")
        return {
            "steps": _step(state, "generate_insight"),
            "status": "insufficient_evidence" if insufficient else "completed",
            "final_answer": "\n".join(lines),
        }

    def run(self, request: ChatRequest) -> AgentResponse:
        initial = AgentState(
            request_id=str(uuid4()),
            query=request.query,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": request.query},
            ],
            plan=None,
            pending=None,
            traces=[],
            results={},
            evidence=[],
            insights=[],
            rounds=0,
            seen_ids=[],
            steps=[],
            errors=[],
            status="running",
            final_answer="",
        )
        state = self.graph.invoke(
            initial, {"recursion_limit": 2 * self.settings.agent_max_rounds + 10}
        )
        return AgentResponse(
            request_id=state["request_id"],
            status=state["status"],
            plan=state["plan"],
            final_answer=state["final_answer"],
            evidence=state["evidence"],
            insights=state["insights"],
            tool_calls=state["traces"],
            tool_results=state["results"],
            steps=state["steps"],
            errors=state["errors"],
        )
