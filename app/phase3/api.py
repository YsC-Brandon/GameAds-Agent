"""Explicit local-only operations; no approval or execution tool is exposed to the model."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.agent.contracts import ErrorResponse
from app.agent.service import AgentService
from app.core.config import Settings
from app.phase3.contracts import (
    ApprovalRequest,
    BudgetRequest,
    BusinessResponse,
    Identity,
    RejectionRequest,
    ReportRequest,
    SimulationRequest,
)
from app.phase3.reports import ReportService
from app.phase3.service import BusinessService
from app.phase3.workflow import ReportWorkflow


def create_router(get_settings, get_agent_service) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        responses={
            409: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )

    def business(request: Request, settings: Annotated[Settings, Depends(get_settings)]):
        return BusinessService(
            request.app.state.business_store,
            data_path=request.app.state.data_path,
            thresholds=settings.anomaly,
        )

    def reports(request: Request, service: Annotated[BusinessService, Depends(business)]):
        return ReportService(service, request.app.state.reports_dir)

    def response(data: dict) -> BusinessResponse:
        return BusinessResponse(status=data.get("status", "ok"), data=data)

    @router.post("/budget-proposals", response_model=BusinessResponse, status_code=201)
    def propose(body: BudgetRequest, service: Annotated[BusinessService, Depends(business)]):
        return response(service.propose_budget_reallocation(body))

    @router.get("/budget-proposals/{proposal_id}", response_model=BusinessResponse)
    def proposal(proposal_id: Identity, service: Annotated[BusinessService, Depends(business)]):
        return response(service.get_budget_proposal(proposal_id))

    @router.get("/budget-proposals/{proposal_id}/events", response_model=BusinessResponse)
    def events(proposal_id: Identity, service: Annotated[BusinessService, Depends(business)]):
        return response({"events": service.store.events(proposal_id, "proposal")})

    @router.post("/budget-proposals/{proposal_id}/approve", response_model=BusinessResponse)
    def approve(
        proposal_id: Identity,
        body: ApprovalRequest,
        service: Annotated[BusinessService, Depends(business)],
    ):
        return response(service.approve_budget_proposal(proposal_id, body))

    @router.post("/budget-proposals/{proposal_id}/reject", response_model=BusinessResponse)
    def reject(
        proposal_id: Identity,
        body: RejectionRequest,
        service: Annotated[BusinessService, Depends(business)],
    ):
        return response(service.reject_budget_proposal(proposal_id, body))

    @router.post("/budget-proposals/{proposal_id}/simulate", response_model=BusinessResponse)
    def simulate(
        proposal_id: Identity,
        body: SimulationRequest,
        service: Annotated[BusinessService, Depends(business)],
    ):
        return response(service.simulate_budget_changes(proposal_id, body))

    @router.post("/reports", response_model=BusinessResponse, status_code=201)
    def generate(body: ReportRequest, service: Annotated[ReportService, Depends(reports)]):
        return response(service.generate_campaign_report(body))

    @router.get("/reports", response_model=BusinessResponse)
    def report_list(
        service: Annotated[ReportService, Depends(reports)],
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        return response({"reports": service.list_reports(limit, offset)})

    @router.get("/reports/{report_id}", response_model=BusinessResponse)
    def report(report_id: Identity, service: Annotated[ReportService, Depends(reports)]):
        return response(service.get_report(report_id))

    @router.post(
        "/report-workflows",
        response_model=BusinessResponse,
        responses={
            502: {"model": BusinessResponse},
            503: {"model": BusinessResponse},
        },
    )
    def workflow(
        body: ReportRequest,
        service: Annotated[ReportService, Depends(reports)],
        agent: Annotated[AgentService, Depends(get_agent_service)],
    ):
        result = ReportWorkflow(agent, service).run(body)
        status = {"model_unavailable": 503, "failed": 502}.get(result["status"], 200)
        return JSONResponse(status_code=status, content=response(result).model_dump(mode="json"))

    return router
