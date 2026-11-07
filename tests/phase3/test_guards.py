"""Additional failure branches and explicit preservation of the accepted execution chain."""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.contracts import AgentResponse, Evidence, Plan, ToolTrace
from app.core.config import PROJECT_ROOT
from app.phase3.contracts import ApprovalRequest, BudgetRequest, ReportRequest
from app.phase3.reports import ReportService
from app.phase3.store import BusinessError, digest
from app.tools.analytics_tools import summarize_campaign_performance
from tests.transport_support import create_app

DATES = {"start_date": "2026-10-01", "end_date": "2026-10-01"}


def test_phase2_core_original_tests_and_dataset_remain_unchanged():
    baseline = json.loads((PROJECT_ROOT / "docs/generated/phase2-core-baseline.json").read_text())
    assert baseline
    for relative, expected in baseline.items():
        assert hashlib.sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest() == expected, (
            relative
        )


def test_historical_archives_are_byte_identical():
    archive = PROJECT_ROOT / "docs/archive/2026-11-05-phase2"
    for entry in json.loads((archive / "manifest.json").read_text()):
        data = (archive / Path(entry["source"]).name).read_bytes()
        assert len(data) == entry["bytes"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]


def test_existing_dataset_is_not_changed_by_new_no_key_startup(tmp_path):
    database, reports_dir = tmp_path / "state.sqlite3", tmp_path / "reports"
    app = create_app(business_db_path=database, reports_dir=reports_dir)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/openapi.json").status_code == 200
    assert not database.exists() and not reports_dir.exists()


def test_nonloopback_requests_rejected(tmp_path):
    app = create_app(business_db_path=tmp_path / "state.sqlite3", reports_dir=tmp_path / "reports")
    client = TestClient(app, client=("203.0.113.5", 12345))
    response = client.get("/api/v1/reports")
    assert response.status_code == 403 and response.json()["code"] == "local_access_only"
    assert not (tmp_path / "state.sqlite3").exists()


def test_symlink_report_path_and_directory_rejected(reports, monkeypatch):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p.suffix == ".md" or original(p))
    with pytest.raises(BusinessError, match="unsafe_report_path"):
        reports._path("a" * 32)
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == reports.reports_dir or original(p))
    with pytest.raises(BusinessError, match="unsafe_reports_directory"):
        reports._path("a" * 32)


@pytest.mark.parametrize("limit,offset", [(0, 0), (101, 0), (True, 0), (1, -1), (1, True)])
def test_python_report_pagination_is_validated(reports, limit, offset):
    with pytest.raises(BusinessError, match="invalid_pagination"):
        reports.list_reports(limit, offset)


def valid_response(business):
    result = summarize_campaign_performance(**DATES, data_path=business.data_path)
    return AgentResponse(
        request_id="offline-validation",
        status="completed",
        final_answer="Evidence only.",
        plan=Plan(task_type="summary", date_range=DATES, steps=["summarize_campaign_performance"]),
        tool_calls=[
            ToolTrace(
                call_id="summary",
                tool_name="summarize_campaign_performance",
                arguments=DATES,
                status="success",
                duration_ms=1,
            )
        ],
        tool_results={"summary": result},
        evidence=[
            Evidence(
                call_id="summary", tool_name="summarize_campaign_performance", path=[], value=result
            )
        ],
    ), result


@pytest.mark.parametrize(
    "change,code",
    [
        ("status", "requires_completed_evidence"),
        ("dates", "date_mismatch"),
        ("result", "evidence_mismatch"),
        ("citation", "missing_citation"),
    ],
)
def test_agent_report_requires_real_matching_evidence(business, change, code):
    response, summary = valid_response(business)
    data = response.model_dump(mode="json")
    if change == "status":
        data["status"] = "failed"
    elif change == "dates":
        data["plan"]["date_range"] = {"start_date": "2026-10-02", "end_date": "2026-10-02"}
    elif change == "result":
        data["tool_results"] = {"summary": {"invented": 999}}
    else:
        data["evidence"] = []
    with pytest.raises(BusinessError, match=code):
        ReportService._validate_agent(AgentResponse.model_validate(data), DATES, summary)


def test_bad_tool_output_does_not_create_report(reports, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private raw error must not leak")

    monkeypatch.setattr("app.phase3.reports.compare_campaigns", fail)
    with pytest.raises(BusinessError, match="tool_failed") as failure:
        reports.generate_campaign_report(ReportRequest(**DATES))
    assert "private" not in str(failure.value)
    assert reports.list_reports() == []
    with reports.store.transaction() as connection:
        event = json.loads(connection.execute("SELECT payload FROM events").fetchone()[0])
        assert event["tool_calls"][-1]["status"] == "error"
        assert event["tool_calls"][-1]["error_code"] == "tool_failed"


def test_dataset_changed_between_tools_aborts(reports, monkeypatch):
    from app.phase3 import reports as report_module

    original = report_module.compare_campaigns

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        path = reports.business.data_path
        path.write_text(path.read_text().replace("100.00", "101.00"))
        return result

    monkeypatch.setattr(report_module, "compare_campaigns", changed)
    with pytest.raises(BusinessError, match="dataset_changed_during_analysis"):
        reports.generate_campaign_report(ReportRequest(**DATES))
    assert reports.list_reports() == []


def test_all_zero_eligible_weights_equal_split(business, budget_request, record, csv_factory):
    business.data_path = csv_factory(
        [
            {**record, "revenue": "0"},
            {**record, "campaign_id": "B", "campaign_name": "B", "revenue": "0"},
        ],
        "zero.csv",
    )
    proposal = business.propose_budget_reallocation(budget_request)
    assert [r["proposed_budget"] for r in proposal["payload"]["allocation"]["allocations"]] == [
        "100.00",
        "100.00",
    ]


def test_zero_current_cannot_grow_and_infeasible_bounds_rejected(business, budget_request):
    data = budget_request.model_dump(mode="json")
    data["campaigns"][0]["current_budget"] = "0"
    proposal = business.propose_budget_reallocation(BudgetRequest.model_validate(data))
    assert proposal["payload"]["allocation"]["allocations"][0]["proposed_budget"] == "0.00"
    data["campaigns"][0]["min_budget"] = "10"
    with pytest.raises(BusinessError, match="infeasible_campaign_bounds"):
        business.propose_budget_reallocation(BudgetRequest.model_validate(data))


def test_review_digest_detects_payload_change_even_if_storage_rehashed(business, budget_request):
    proposal = business.propose_budget_reallocation(budget_request)
    proposal["payload"]["allocation"]["allocations"][0]["proposed_budget"] = "999.00"
    assert digest(proposal["payload"]) != proposal["proposal_sha256"]
    with business.store.transaction() as connection:
        business.store.save(connection, proposal["id"], "proposal", proposal)
    with pytest.raises(BusinessError, match="proposal_integrity_error"):
        business.approve_budget_proposal(
            proposal["id"],
            ApprovalRequest(
                approver="reviewer",
                confirmed=True,
                expected_version=1,
                proposal_sha256=proposal["proposal_sha256"],
            ),
        )
