"""Reports are produced by real tools, with traceable evidence and safe local output."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from app.phase3.contracts import ReportRequest
from app.phase3.reports import ReportService, render_report, text
from app.phase3.store import BusinessError
from app.tools.analytics_tools import summarize_campaign_performance


def request(**kwargs):
    return ReportRequest(start_date="2026-10-01", end_date="2026-10-01", **kwargs)


def test_report_contains_exact_tool_results_and_citations(reports):
    document = reports.generate_campaign_report(request())
    retrieved = reports.get_report(document["id"])
    content = retrieved["content"]
    assert hashlib.sha256(content.encode()).hexdigest() == document["content_sha256"]
    assert content == render_report(document)
    traces = document["tool_calls"]
    assert len(traces) == 3 and len({t["call_id"] for t in traces}) == 3
    summary = summarize_campaign_performance(
        "2026-10-01", "2026-10-01", data_path=reports.business.data_path
    )
    assert document["tool_results"][traces[0]["call_id"]] == summary
    embedded = json.loads(content.split("```json\n")[1].split("\n```")[0])
    assert embedded["tool_results"] == document["tool_results"]
    assert all(t["status"] == "success" and t["duration_ms"] >= 0 for t in traces)
    assert f"[{traces[0]['call_id']}#/overall/metrics/roas]" in content
    assert "1.5" in content  # independent expected total revenue 150 / spend 100
    assert "insufficient_evidence" in content  # baseline absent, not a false non-trigger
    assert reports.list_reports()[0]["id"] == document["id"]
    assert reports.list_reports(offset=1) == []


def test_report_restart_metadata_and_content(reports):
    document = reports.generate_campaign_report(request())
    restarted = ReportService(reports.business, reports.reports_dir)
    assert restarted.get_report(document["id"])["content"] == render_report(document)


def test_empty_data_explicit_no_data(reports, csv_factory):
    reports.business.data_path = csv_factory([], "empty.csv")
    doc = reports.generate_campaign_report(request())
    content = reports.get_report(doc["id"])["content"]
    assert "无可用数据" in content and "null" in content
    assert "无可计算排名" in content


def test_subset_does_not_claim_unfiltered_overall(reports):
    doc = reports.generate_campaign_report(request(campaign_ids=["A"]))
    content = reports.get_report(doc["id"])["content"].split("## 完整可复核证据")[0]
    assert "不将全数据集整体指标冒充筛选后合计" in content
    assert "| 整体 |" not in content and "| B |" not in content
    assert "| A | ROAS | 2.0" in content


def test_unknown_report_campaign_rejected(reports):
    with pytest.raises(BusinessError, match="report_campaign_missing"):
        reports.generate_campaign_report(request(campaign_ids=["unknown"]))
    assert reports.list_reports() == []


@pytest.mark.parametrize(
    "extra",
    [
        {"output_path": "../private.md"},
        {"output_format": "html"},
        {"proposal_id": "../private"},
        {"campaign_ids": ["A", "A"]},
    ],
)
def test_arbitrary_output_and_invalid_scope_rejected(extra):
    with pytest.raises(ValidationError):
        request(**extra)


@pytest.mark.parametrize(
    "identity", ["../outside", "..\\outside", "C:\\private", "a" * 31, "G" * 32]
)
def test_report_path_traversal_denied(reports, identity):
    with pytest.raises(BusinessError, match="invalid_report_id"):
        reports._path(identity)


def test_report_tampering_and_missing_file(reports):
    doc = reports.generate_campaign_report(request())
    path = reports.reports_dir / doc["filename"]
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(BusinessError, match="report_file_integrity_error"):
        reports.get_report(doc["id"])
    path.unlink()
    with pytest.raises(BusinessError, match="report_file_missing"):
        reports.get_report(doc["id"])


def test_report_attaches_immutable_budget_snapshot(reports, budget_request):
    proposal = reports.business.propose_budget_reallocation(budget_request)
    doc = reports.generate_campaign_report(request(proposal_id=proposal["id"]))
    assert doc["proposal"] == proposal
    assert "pending_approval" in reports.get_report(doc["id"])["content"]


def test_proposal_report_period_and_dataset_must_match(reports, budget_request):
    proposal = reports.business.propose_budget_reallocation(budget_request)
    with pytest.raises(BusinessError, match="proposal_report_evidence_mismatch"):
        reports.generate_campaign_report(
            ReportRequest(
                start_date="2026-10-02", end_date="2026-10-02", proposal_id=proposal["id"]
            )
        )
    raw = reports.business.data_path.read_text(encoding="utf-8")
    reports.business.data_path.write_text(raw.replace("100.00", "101.00"), encoding="utf-8")
    with pytest.raises(BusinessError, match="proposal_report_evidence_mismatch"):
        reports.generate_campaign_report(request(proposal_id=proposal["id"]))


def test_tool_failure_does_not_publish_report(reports, tmp_path):
    reports.business.data_path = tmp_path / "missing-private.csv"
    with pytest.raises(BusinessError, match="data_source_error"):
        reports.generate_campaign_report(request())
    assert reports.list_reports() == []
    assert not reports.reports_dir.exists()


def test_markdown_untrusted_labels_escaped():
    result = text("<script> | [click](https://invalid) ```\nnew")
    assert "<script>" not in result and "```" not in result and "[click]" not in result
    assert "\n" not in result and "&#124;" in result


def test_write_failure_rolls_back_database(reports, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(reports.store, "save", fail)
    with pytest.raises(BusinessError, match="report_write_failed"):
        reports.generate_campaign_report(request())
    assert not list(reports.reports_dir.glob("*.md"))
    assert reports.list_reports() == []
