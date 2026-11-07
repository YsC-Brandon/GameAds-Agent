"""Provider-independent contracts for plans, calls, evidence, and public responses."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from app.schemas import Contract, DateRange, Label

# Opaque correlation tokens, never file paths or executable function names.
# Printable ASCII permits vendor punctuation, while excluding whitespace/controls.
CallID = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[!-~]+$")]

ToolName = Literal[
    "list_campaigns",
    "query_campaigns",
    "get_campaign_detail",
    "summarize_campaign_performance",
    "compare_campaigns",
    "detect_campaign_anomalies",
]


class ChatRequest(Contract):
    query: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class Plan(Contract):
    task_type: Literal[
        "list",
        "query",
        "detail",
        "summary",
        "comparison",
        "anomalies",
        "analysis",
        "needs_clarification",
        "unsupported",
    ]
    date_range: DateRange | None = None
    steps: list[ToolName] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def check_scope(self):
        if self.task_type in {"needs_clarification", "unsupported"}:
            if self.steps:
                raise ValueError("Non-executable plans cannot contain steps.")
        elif not self.steps:
            raise ValueError("An executable plan requires tools.")
        if any(step != "list_campaigns" for step in self.steps) and self.date_range is None:
            raise ValueError("Explicit dates are required for data analysis.")
        return self


class FunctionCall(Contract):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    arguments: str = Field(max_length=32_000)


class ToolCall(Contract):
    id: CallID
    type: Literal["function"] = "function"
    function: FunctionCall


class ModelMessage(Contract):
    role: Literal["assistant"] = "assistant"
    content: str | None = Field(default=None, max_length=16_000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_calls(self):
        ids = [call.id for call in self.tool_calls]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate call IDs.")
        return self

    def wire(self) -> dict:
        result = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            result["tool_calls"] = [call.model_dump() for call in self.tool_calls]
        return result


class EvidenceReference(Contract):
    call_id: CallID
    # An empty path selects the entire result; arrays use decimal index segments.
    path: list[str] = Field(default_factory=list, max_length=12)


class Interpretation(Contract):
    call_id: CallID
    kind: Literal["campaign_list", "records", "performance", "ranking", "anomaly_screening"]


class FinishRequest(Contract):
    references: list[EvidenceReference] = Field(min_length=1, max_length=20)
    interpretations: list[Interpretation] = Field(default_factory=list, max_length=6)


class ToolTrace(Contract):
    call_id: str
    tool_name: str
    arguments: dict[str, JsonValue] | None = None
    status: Literal["success", "error"]
    duration_ms: float = Field(ge=0)
    result_status: str | None = None
    error_code: str | None = None


class Evidence(Contract):
    call_id: str
    tool_name: str
    path: list[str]
    value: JsonValue


class AgentResponse(Contract):
    request_id: str
    status: Literal[
        "completed",
        "needs_clarification",
        "unsupported",
        "model_unavailable",
        "failed",
        "insufficient_evidence",
    ]
    plan: Plan | None = None
    final_answer: str
    evidence: list[Evidence] = Field(default_factory=list)
    insights: list[str] = Field(default_factory=list)
    tool_results: dict[str, JsonValue] = Field(default_factory=dict)
    tool_calls: list[ToolTrace] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class DetailRequest(DateRange):
    campaign_id: Label
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=100, ge=1, le=100, strict=True)


class HealthResponse(Contract):
    status: Literal["ok"] = "ok"
    model_configured: bool


class DataResponse(Contract):
    status: str
    data: dict[str, JsonValue]


class ErrorResponse(Contract):
    status: Literal["error"] = "error"
    code: str
    message: str
