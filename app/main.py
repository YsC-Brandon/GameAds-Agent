"""Local HTTP entry point; accepted Phase 2 routes plus isolated Phase 3 operations."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from app.agent.contracts import (
    AgentResponse,
    ChatRequest,
    DataResponse,
    ErrorResponse,
    HealthResponse,
)
from app.agent.provider import model_configured
from app.agent.service import AgentService
from app.core.config import PROJECT_ROOT, Settings
from app.core.exceptions import CampaignError
from app.http_transport import TransportBoundary
from app.local_boundary import LocalBoundary
from app.phase3.api import create_router
from app.phase3.store import BusinessError, Store
from app.phase4.api import create_task_router
from app.phase4.http_body import TaskBodyBoundary
from app.phase4.ownership import CandidateOwner
from app.schemas import CampaignQuery
from app.services.campaign_service import DEFAULT_DATA_PATH
from app.task_wire import TaskWireBoundary, is_task_path
from app.tools.campaign_tools import list_campaigns, query_campaigns


def get_settings() -> Settings:
    return Settings()


def get_agent_service(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> AgentService:
    return AgentService(settings, data_path=request.app.state.data_path)


def create_app(
    *,
    data_path: Path | None = None,
    business_db_path: Path | None = None,
    reports_dir: Path | None = None,
    storage_owner: CandidateOwner | None = None,
) -> FastAPI:
    installed_root = os.environ.get("GAMEADS_INSTANCE_ROOT")
    explicit_database = business_db_path
    explicit_data_path = data_path
    explicit_reports_dir = reports_dir
    data_path = data_path or DEFAULT_DATA_PATH
    business_db_path = business_db_path or PROJECT_ROOT / "data" / "phase3.sqlite3"
    reports_dir = reports_dir or PROJECT_ROOT / "reports"
    store = Store(business_db_path, owner=storage_owner, lifecycle=True)

    @asynccontextmanager
    async def lifespan(application):
        installed_owner = None
        try:
            if installed_root and storage_owner is None:
                from app.phase4.generation import GenerationRehearsal
                from app.phase4.store import CandidateStore

                with GenerationRehearsal(Path(installed_root)).locked() as instance:
                    _, generation = instance.resolve()
                    directory = Path(installed_root) / generation["directory"]
                database = explicit_database or directory / "database.sqlite3"
                if (
                    explicit_database is not None
                    and database.resolve() != (directory / "database.sqlite3").resolve()
                ):
                    from app.phase4.ownership import OwnershipError

                    raise OwnershipError("Explicit database is not the active installed generation")
                if (
                    explicit_data_path is not None
                    and Path(explicit_data_path).resolve() != (directory / "dataset.csv").resolve()
                ):
                    from app.phase4.ownership import OwnershipError

                    raise OwnershipError("Explicit dataset is not the active installed generation")
                if (
                    explicit_reports_dir is not None
                    and Path(explicit_reports_dir).resolve() != (directory / "reports").resolve()
                ):
                    from app.phase4.ownership import OwnershipError

                    raise OwnershipError(
                        "Explicit reports directory is not the active installed generation"
                    )
                installed_owner = CandidateOwner(database).acquire()
                CandidateStore(installed_owner).recover_before_serving()
                application.state.storage_owner = installed_owner
                application.state.business_store = Store(database, owner=installed_owner)
                application.state.business_db_path = database
                application.state.data_path = directory / "dataset.csv"
                application.state.reports_dir = directory / "reports"
            yield
        finally:
            await asyncio.to_thread(store.close)
            if installed_owner is not None:
                application.state.business_store.close()
                await asyncio.to_thread(installed_owner.close)

    application = FastAPI(
        title="GameAds-Agent",
        version="0.1.0",
        lifespan=lifespan,
        responses={status: {"model": ErrorResponse} for status in (403, 404, 405, 422, 500)},
    )
    if storage_owner is not None or installed_root:
        application.add_middleware(TaskBodyBoundary)
        application.include_router(create_task_router(get_settings, get_agent_service))
    if storage_owner is not None:
        storage_owner.require()
        if not storage_owner.ready:
            raise RuntimeError("Storage must recover before task admission")
        application.state.storage_owner = storage_owner
    application.add_middleware(TaskWireBoundary)
    application.add_middleware(LocalBoundary)
    application.add_middleware(TransportBoundary)
    application.state.data_path = data_path
    application.state.business_db_path = business_db_path
    application.state.business_store = store
    application.state.reports_dir = reports_dir

    def error(code: str, status: int, request=None) -> JSONResponse:
        body = ErrorResponse(
            code=code, message="Request could not be completed. Check inputs/configuration."
        )
        data = body.model_dump()
        if request is not None and is_task_path(request.scope.get("raw_path", b"")):
            data |= {"task_id": None, "retryable": False}
        return JSONResponse(status_code=status, content=data)

    @application.exception_handler(RequestValidationError)
    @application.exception_handler(ValidationError)
    async def invalid_request(request, exc):
        if is_task_path(request.scope.get("raw_path", b"")) and any(
            item["type"] == "json_invalid" for item in exc.errors()
        ):
            return error("invalid_json", 400, request)
        return error("invalid_request", 422, request)

    @application.exception_handler(CampaignError)
    async def campaign_error(request, exc):
        return error(exc.code, 404 if exc.code == "campaign_not_found" else 400)

    @application.exception_handler(BusinessError)
    async def business_error(request, exc):
        return error(exc.code, exc.status, request)

    @application.exception_handler(Exception)
    async def internal_error(request, exc):
        return error("internal_error", 500, request)

    @application.exception_handler(HTTPException)
    async def http_error(request, exc):
        if exc.status_code == 405 and is_task_path(request.scope.get("raw_path", b"")):
            return error("method_not_allowed", 405, request)
        return error(
            "not_found" if exc.status_code == 404 else "http_error", exc.status_code, request
        )

    @application.get("/health", response_model=HealthResponse)
    def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
        # Liveness only: neither dataset readiness nor upstream model connectivity.
        return HealthResponse(model_configured=model_configured(settings))

    @application.get(
        "/api/v1/campaigns", response_model=DataResponse, responses={400: {"model": ErrorResponse}}
    )
    def campaigns(
        start_date: str | None = None,
        end_date: str | None = None,
        campaign_ids: Annotated[list[str] | None, Query()] = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> DataResponse:
        if start_date is None and end_date is None and campaign_ids is None:
            result = list_campaigns(data_path=data_path)
        else:
            query = CampaignQuery(
                start_date=start_date,
                end_date=end_date,
                campaign_ids=campaign_ids,
                offset=offset,
                limit=limit,
            )
            result = query_campaigns(**query.model_dump(), data_path=data_path)
        return DataResponse(status=result["status"], data=result)

    @application.post(
        "/api/v1/chat",
        response_model=AgentResponse,
        responses={502: {"model": AgentResponse}, 503: {"model": AgentResponse}},
    )
    def chat(request: ChatRequest, service: Annotated[AgentService, Depends(get_agent_service)]):
        response = service.run(request)
        status = {"model_unavailable": 503, "failed": 502}.get(response.status, 200)
        return JSONResponse(status_code=status, content=response.model_dump(mode="json"))

    application.include_router(create_router(get_settings, get_agent_service))
    return application


app = create_app()
