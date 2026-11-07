"""S08 valid preconditions: legacy shared service and HTTP cannot bypass tasks."""

import os

import pytest

from app.phase3.contracts import ApprovalRequest, RejectionRequest, ReportRequest, SimulationRequest
from app.phase3.reports import ReportService
from app.phase3.service import BusinessService
from app.phase3.store import BusinessError, Store
from app.phase3.workflow import ReportWorkflow
from tests.phase4.test_final_contract import local
from tests.phase4.test_owner_application_integration import DATES, budget

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows candidate legacy guard")
__all__ = ["local"]


@pytest.mark.parametrize("action", ["approve", "reject", "simulate", "reports", "report-workflows"])
def test_shared_and_http_guard_with_legal_preconditions(local, tmp_path, action):
    client, candidate = local
    task = client.post(
        "/api/v1/tasks",
        json={"kind": "budget_review", "request": budget()},
        headers={"Idempotency-Key": "create"},
    ).json()["data"]
    proposal = client.get("/api/v1/budget-proposals/" + task["proposal_id"]).json()["data"]
    base = "/api/v1/tasks/" + task["id"]
    review = {
        "expected_version": 1,
        "expected_task_version": 3,
        "proposal_sha256": proposal["proposal_sha256"],
        "confirmed": True,
        "approver": "synthetic",
    }
    if action in {"simulate", "reports", "report-workflows"}:
        assert (
            client.post(
                base + "/approve", json=review, headers={"Idempotency-Key": "approve"}
            ).status_code
            == 200
        )
        review |= {"expected_version": 2, "expected_task_version": 4}
    if action in {"reports", "report-workflows"}:
        assert (
            client.post(
                base + "/simulate",
                json={
                    key: review[key]
                    for key in ("expected_version", "expected_task_version", "proposal_sha256")
                },
                headers={"Idempotency-Key": "simulate"},
            ).status_code
            == 200
        )
    before = client.get(base).json()
    original = client.get("/api/v1/budget-proposals/" + task["proposal_id"]).json()
    business = BusinessService(
        Store(candidate.path, owner=candidate.owner), data_path=client.app.state.data_path
    )
    reports = ReportService(business, tmp_path / "reports")

    class ForbiddenAgent:
        def run(self, *args):
            pytest.fail("task-managed denial reached model")

    if action in {"reports", "report-workflows"}:
        body = DATES | {"proposal_id": proposal["id"], "campaign_ids": ["A"]}
        path = "/api/v1/" + action

        def direct():
            if action == "reports":
                return reports.generate_campaign_report(ReportRequest(**body))
            return ReportWorkflow(ForbiddenAgent(), reports).run(ReportRequest(**body))
    else:
        body = {key: review[key] for key in ("expected_version", "proposal_sha256")}
        if action != "simulate":
            body |= {"confirmed": True, "approver": "synthetic"}
        if action == "reject":
            body["reason"] = "synthetic decline"
        path = "/api/v1/budget-proposals/" + proposal["id"] + "/" + action
        model = {
            "approve": ApprovalRequest,
            "reject": RejectionRequest,
            "simulate": SimulationRequest,
        }[action](**body)

        def direct():
            return getattr(
                business,
                {
                    "approve": "approve_budget_proposal",
                    "reject": "reject_budget_proposal",
                    "simulate": "simulate_budget_changes",
                }[action],
            )(proposal["id"], model)

    response = client.post(path, json=body)
    assert response.status_code == 409 and response.json()["code"] == "task_managed_resource"
    with pytest.raises(BusinessError, match="task_managed_resource"):
        direct()
    assert client.get(base).json() == before
    assert client.get("/api/v1/budget-proposals/" + task["proposal_id"]).json() == original
    assert not list((tmp_path / "reports").glob("*"))
