"""Runtime request models for the frozen 4A-r4 task contract."""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from app.agent.contracts import AgentResponse, ChatRequest
from app.phase3.contracts import (
    ApprovalRequest,
    BudgetRequest,
    Digest,
    Identity,
    RejectionRequest,
    ReportRequest,
    SimulationRequest,
)
from app.schemas import Contract


class TaskReportRequest(ReportRequest):
    proposal_id: None = None


class AnalysisTaskCreate(Contract):
    kind: Literal["analysis"]
    request: ChatRequest
    retry_of: Identity | None = None


class BudgetTaskCreate(Contract):
    kind: Literal["budget_review"]
    request: BudgetRequest
    retry_of: Identity | None = None


class ReportTaskCreate(Contract):
    kind: Literal["report"]
    request: TaskReportRequest
    retry_of: Identity | None = None


TaskCreate = Annotated[
    AnalysisTaskCreate | BudgetTaskCreate | ReportTaskCreate, Field(discriminator="kind")
]
CREATE_ADAPTER = TypeAdapter(TaskCreate)


class TaskApproval(ApprovalRequest):
    expected_task_version: int = Field(ge=1, strict=True)


class TaskRejection(RejectionRequest):
    expected_task_version: int = Field(ge=1, strict=True)


class TaskSimulation(SimulationRequest):
    expected_task_version: int = Field(ge=1, strict=True)


class TaskPublish(Contract):
    expected_task_version: int = Field(ge=1, strict=True)


TaskKind = Literal["analysis", "budget_review", "report"]
TaskStatus = Literal[
    "submitted",
    "running",
    "awaiting_approval",
    "approved",
    "simulated",
    "completed",
    "rejected",
    "failed",
    "needs_clarification",
    "unsupported",
    "insufficient_evidence",
    "interrupted",
]


class TaskFailure(Contract):
    code: str
    message: str
    retryable: bool


class TaskError(TaskFailure):
    status: Literal["error"]
    task_id: Identity | None


class TaskRecord(Contract):
    id: Identity
    kind: TaskKind
    status: TaskStatus
    version: int = Field(ge=1)
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    updated_at: str = Field(json_schema_extra={"format": "date-time"})
    retry_of: Identity | None
    proposal_id: Identity | None
    report_id: Identity | None
    agent_result: AgentResponse | None
    error: TaskFailure | None
    source_sha256: Digest | None
    active_operation_id: Identity | None


class TaskResponse(Contract):
    status: Literal["ok"]
    data: TaskRecord


class TaskEvent(Contract):
    seq: int = Field(ge=1)
    task_id: Identity
    action: str
    from_status: TaskStatus | None
    to_status: TaskStatus
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    code: str | None


class ReportIndex(Contract):
    id: Identity
    task_id: Identity
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    content_sha256: Digest
    evidence_sha256: Digest


class Page(Contract):
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    has_more: bool


class TaskPage(Page):
    items: list[TaskRecord]


class EventPage(Page):
    items: list[TaskEvent]


class ReportIndexPage(Page):
    items: list[ReportIndex] = Field(max_length=1)


class OperationRecord(Contract):
    model_config = {
        "json_schema_extra": {
            "oneOf": [
                {
                    "properties": {
                        "phase": {"const": "in_progress"},
                        "sealed_http_status": {"type": "null"},
                    }
                },
                {
                    "properties": {
                        "phase": {"const": "completed"},
                        "sealed_http_status": {"type": "integer", "minimum": 200, "maximum": 599},
                    }
                },
                {
                    "properties": {
                        "phase": {"const": "interrupted"},
                        "sealed_http_status": {"const": 409},
                    }
                },
            ]
        }
    }
    operation_id: Identity
    method: Literal["POST"]
    path: str = Field(pattern=r"^/api/v1/tasks(/[0-9a-f]{32}/(approve|reject|simulate|report))?$")
    task_id: Identity | None
    phase: Literal["in_progress", "completed", "interrupted"]
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    updated_at: str = Field(json_schema_extra={"format": "date-time"})
    sealed_http_status: int | None = Field(ge=200, le=599)

    @model_validator(mode="after")
    def phase_and_result(self):
        if (self.phase == "in_progress") != (self.sealed_http_status is None):
            raise ValueError("Invalid operation phase/result")
        if self.phase == "interrupted" and self.sealed_http_status != 409:
            raise ValueError("Interrupted operations seal 409")
        return self


class OperationResponse(Contract):
    status: Literal["ok"]
    data: OperationRecord
