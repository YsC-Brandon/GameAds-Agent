"""Task HTTP adapter, enabled only for an explicitly recovered candidate owner."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from app.agent.service import AgentService
from app.core.config import Settings
from app.phase3.contracts import Identity
from app.phase4.contracts import (
    EventPage,
    OperationResponse,
    ReportIndexPage,
    TaskApproval,
    TaskCreate,
    TaskError,
    TaskPage,
    TaskPublish,
    TaskRejection,
    TaskResponse,
    TaskSimulation,
)
from app.phase4.service import TaskService
from app.task_wire import parse_identity


def create_task_router(get_settings, get_agent_service):
    errors = {
        status: {"model": TaskError}
        for status in (400, 403, 404, 405, 409, 413, 415, 422, 500, 502, 503)
    }
    router = APIRouter(prefix="/api/v1/tasks", responses=errors)

    def service(
        request: Request,
        settings: Annotated[Settings, Depends(get_settings)],
        agent: Annotated[AgentService, Depends(get_agent_service)],
    ):
        if request.app.state.business_store._closed:
            from app.phase3.store import BusinessError

            raise BusinessError("database_busy", 503)
        return TaskService(
            request.app.state.storage_owner.storage,
            data_path=request.app.state.data_path,
            reports_dir=request.app.state.reports_dir,
            agent=agent,
            thresholds=settings.anomaly,
        )

    def query(request, allowed):
        from app.phase3.store import BusinessError

        names = list(request.query_params.keys())
        if any(
            name not in allowed or len(request.query_params.getlist(name)) != 1 for name in names
        ):
            raise BusinessError("invalid_request", 422)

    def execute(request, service, body, task_id=None, action=None):
        identity = parse_identity(request.scope)
        status, raw = service.execute(identity, body, task_id=task_id, action=action)
        headers = {}
        if status == 201:
            import json

            result = json.loads(raw)
            headers["Location"] = "/api/v1/tasks/" + result["data"]["id"]
        return Response(raw, status_code=status, media_type="application/json", headers=headers)

    key_parameter = {
        "name": "Idempotency-Key",
        "in": "header",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
            "pattern": "^[A-Za-z0-9._:-]+$",
        },
    }
    post_metadata = {"parameters": [key_parameter]}

    @router.post(
        "",
        status_code=201,
        response_model=TaskResponse,
        openapi_extra=post_metadata
        | {"responses": {"201": {"headers": {"Location": {"schema": {"type": "string"}}}}}},
    )
    def create(request: Request, body: TaskCreate, tasks: Annotated[TaskService, Depends(service)]):
        return execute(request, tasks, body)

    @router.get("", response_model=TaskPage)
    def listing(
        request: Request,
        tasks: Annotated[TaskService, Depends(service)],
        kind: Literal["analysis", "budget_review", "report"] = None,
        status: Literal[
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
        ] = None,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        query(request, {"kind", "status", "limit", "offset"})
        return tasks.page(kind=kind, status=status, limit=limit, offset=offset)

    @router.get(
        "/operations/lookup",
        response_model=OperationResponse,
        openapi_extra={
            "parameters": [
                key_parameter,
                {
                    "name": "method",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "const": "POST"},
                },
                {
                    "name": "path",
                    "in": "query",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "pattern": (
                            "^/api/v1/tasks(/[0-9a-f]{32}/(approve|reject|simulate|report))?$"
                        ),
                    },
                },
            ],
            "responses": {
                str(status): {
                    "headers": {
                        "Cache-Control": {"schema": {"type": "string", "const": "no-store"}}
                    }
                }
                for status in (200, 403, 404, 405, 422, 500, 503)
            },
        },
    )
    def lookup(request: Request, tasks: Annotated[TaskService, Depends(service)]):
        if (
            request.headers.get("content-length", "0") != "0"
            or "transfer-encoding" in request.headers
        ):
            from app.phase3.store import BusinessError

            raise BusinessError("invalid_request", 422)
        return tasks.lookup(*parse_identity(request.scope))

    @router.get("/{task_id}", response_model=TaskResponse)
    def get(task_id: Identity, request: Request, tasks: Annotated[TaskService, Depends(service)]):
        query(request, set())
        return tasks.get(task_id)

    @router.get("/{task_id}/events", response_model=EventPage)
    def events(
        task_id: Identity,
        request: Request,
        tasks: Annotated[TaskService, Depends(service)],
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        query(request, {"limit", "offset"})
        return tasks.page(task_id=task_id, collection="events", limit=limit, offset=offset)

    @router.get("/{task_id}/reports", response_model=ReportIndexPage)
    def reports(
        task_id: Identity,
        request: Request,
        tasks: Annotated[TaskService, Depends(service)],
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        query(request, {"limit", "offset"})
        return tasks.page(task_id=task_id, collection="reports", limit=limit, offset=offset)

    @router.post("/{task_id}/approve", response_model=TaskResponse, openapi_extra=post_metadata)
    def approve(
        task_id: Identity,
        request: Request,
        body: TaskApproval,
        tasks: Annotated[TaskService, Depends(service)],
    ):
        return execute(request, tasks, body, task_id, "approve")

    @router.post("/{task_id}/reject", response_model=TaskResponse, openapi_extra=post_metadata)
    def reject(
        task_id: Identity,
        request: Request,
        body: TaskRejection,
        tasks: Annotated[TaskService, Depends(service)],
    ):
        return execute(request, tasks, body, task_id, "reject")

    @router.post("/{task_id}/simulate", response_model=TaskResponse, openapi_extra=post_metadata)
    def simulate(
        task_id: Identity,
        request: Request,
        body: TaskSimulation,
        tasks: Annotated[TaskService, Depends(service)],
    ):
        return execute(request, tasks, body, task_id, "simulate")

    @router.post("/{task_id}/report", response_model=TaskResponse, openapi_extra=post_metadata)
    def publish(
        task_id: Identity,
        request: Request,
        body: TaskPublish,
        tasks: Annotated[TaskService, Depends(service)],
    ):
        return execute(request, tasks, body, task_id, "report")

    return router
