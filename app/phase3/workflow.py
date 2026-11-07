"""Report graph wrapping, not replacing, the accepted read-only AgentService."""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.contracts import AgentResponse, ChatRequest
from app.agent.service import AgentService
from app.phase3.contracts import ReportRequest
from app.phase3.reports import ReportService
from app.phase3.store import BusinessError, owned_action


class ReportState(TypedDict):
    request: ReportRequest
    agent: AgentResponse | None
    report: dict | None
    status: str
    steps: list[str]
    errors: list[str]


class ReportWorkflow:
    def __init__(self, agent: AgentService, reports: ReportService):
        self.agent, self.reports = agent, reports
        graph = StateGraph(ReportState)
        graph.add_node("analyze_with_phase2_agent", self.analyze)
        graph.add_node("publish_evidence_report", self.publish)
        graph.add_edge(START, "analyze_with_phase2_agent")
        graph.add_conditional_edges(
            "analyze_with_phase2_agent",
            self.route,
            {"publish": "publish_evidence_report", "end": END},
        )
        graph.add_edge("publish_evidence_report", END)
        self.graph = graph.compile()

    def analyze(self, state: ReportState) -> dict:
        request = state["request"]
        response = self.agent.run(
            ChatRequest(
                query=(
                    f"汇总 {request.start_date.isoformat()} 到 "
                    f"{request.end_date.isoformat()} 的广告整体表现，"
                    "使用 summarize_campaign_performance 返回完整区间五项指标并引用真实工具结果。"
                )
            )
        )
        if response.status != "completed":
            self.reports.store.failure(
                "agent_report_analysis_failed",
                response.status,
                [item.model_dump(mode="json") for item in response.tool_calls],
            )
        return {
            "agent": response,
            "status": response.status,
            "errors": response.errors,
            "steps": ["analyze_with_phase2_agent"],
        }

    @staticmethod
    def route(state: ReportState) -> str:
        return "publish" if state["status"] == "completed" else "end"

    def publish(self, state: ReportState) -> dict:
        try:
            report = self.reports.generate_campaign_report(state["request"], agent=state["agent"])
        except BusinessError as exc:
            return {
                "status": "failed",
                "errors": [exc.code],
                "steps": [*state["steps"], "publish_evidence_report"],
            }
        return {
            "report": report,
            "status": "completed",
            "steps": [*state["steps"], "publish_evidence_report"],
        }

    @owned_action
    def run(self, request: ReportRequest) -> dict:
        request = ReportRequest.model_validate(request)
        self.reports.store.check_unmanaged(request.proposal_id)
        state = self.graph.invoke(
            {
                "request": ReportRequest.model_validate(request),
                "agent": None,
                "report": None,
                "status": "running",
                "steps": [],
                "errors": [],
            }
        )
        return {
            "status": state["status"],
            "steps": state["steps"],
            "errors": state["errors"],
            "report": state["report"],
            "agent": state["agent"].model_dump(mode="json"),
        }
