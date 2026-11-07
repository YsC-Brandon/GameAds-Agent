"""Ordinary failure windows, r4 task errors and complete backup manifests."""

import json
import os
from pathlib import Path

import pytest

from app.phase3.store import BusinessError
from app.phase4.contracts import CREATE_ADAPTER
from app.phase4.maintenance import backup, restore_candidate, sha, verify_backup
from tests.phase4.test_acceptance_state import context, create_budget, request

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows failure matrix")
__all__ = ["context"]


@pytest.mark.parametrize("size,expected", [(2 * 1024 * 1024, 201), (2 * 1024 * 1024 + 1, 413)])
def test_analysis_utf8_evidence_limit(context, size, expected):
    from app.agent.contracts import AgentResponse
    from app.phase3.store import encode

    service, _ = context
    result = AgentResponse(
        request_id="synthetic",
        status="completed",
        final_answer="Inspect the structured plan and tool evidence.",
        tool_results={"blob": ""},
    )
    overhead = len(encode(result.model_dump(mode="json")).encode("utf-8"))
    result.tool_results["blob"] = "a" * (size - overhead)

    class Offline:
        def run(self, request):
            return result

    service.agent = Offline()
    status, raw = service.execute(
        ("POST", "/api/v1/tasks", "evidence-limit"),
        CREATE_ADAPTER.validate_python({"kind": "analysis", "request": {"query": "offline"}}),
    )
    assert status == expected
    if expected == 413:
        assert json.loads(raw)["code"] == "result_too_large"


def test_maintenance_connection_cannot_write_without_owner(tmp_path):
    from app.phase4.maintenance import connect
    from app.phase4.ownership import OwnershipError

    with pytest.raises(OwnershipError):
        connect(tmp_path / "must-not-exist.sqlite3", readonly=False)
    assert not (tmp_path / "must-not-exist.sqlite3").exists()


@pytest.mark.parametrize("kind", ["budget_review", "report"])
def test_task_missing_dataset_uses_500_and_sealed_task_id(context, kind):
    from tests.phase4.test_owner_application_integration import DATES, budget

    service, _ = context
    service.business.data_path.unlink()
    body = CREATE_ADAPTER.validate_python(
        {"kind": kind, "request": budget() if kind == "budget_review" else DATES}
    )
    identity = ("POST", "/api/v1/tasks", "missing-data")
    result = service.execute(identity, body)
    assert result[0] == 500 and json.loads(result[1])["code"] == "data_source_error"
    task_id = json.loads(result[1])["task_id"]
    assert service.get(task_id)["data"]["status"] == "failed"
    assert service.execute(identity, body) == result


def simulated(service):
    task, proposal = create_budget(service)
    for action, version, proposal_version in (("approve", 3, 1), ("simulate", 4, 2)):
        result = service.execute(
            ("POST", f"/api/v1/tasks/{task['id']}/{action}", action),
            request(action, version, proposal_version, proposal["proposal_sha256"]),
            task_id=task["id"],
            action=action,
        )
        assert result[0] == 200
    return task["id"]


def test_budget_report_dataset_change_keeps_simulated(context):
    service, _ = context
    identity = simulated(service)
    service.business.data_path.write_bytes(service.business.data_path.read_bytes() + b"\n")
    result = service.execute(
        ("POST", f"/api/v1/tasks/{identity}/report", "changed"),
        request("report", 5),
        task_id=identity,
        action="report",
    )
    assert result[0] == 409
    assert json.loads(result[1])["code"] == "dataset_changed_during_analysis"
    current = service.get(identity)["data"]
    assert current["status"] == "simulated" and current["version"] == 5
    assert current["report_id"] is None and current["error"] is None


@pytest.mark.parametrize("fault", ["fsync", "rename", "register", "seal"])
def test_report_publication_failure_windows(context, monkeypatch, fault):
    service, candidate = context
    identity = simulated(service)
    before = service.get(identity)
    original_seal = candidate.seal

    def fail(*args, **kwargs):
        raise OSError("synthetic disk fault")

    if fault == "fsync":
        monkeypatch.setattr(os, "fsync", fail)
    elif fault == "rename":
        monkeypatch.setattr(Path, "rename", fail)
    elif fault == "register":
        original_save = service.store.save

        def fail_save(connection, identity, kind, value, **kwargs):
            if kind == "report":
                fail()
            return original_save(connection, identity, kind, value, **kwargs)

        monkeypatch.setattr(service.store, "save", fail_save)
    else:

        def fail_seal(connection, operation, status, response, **kwargs):
            if status == 200:
                raise BusinessError("internal_error", 500)
            return original_seal(connection, operation, status, response, **kwargs)

        monkeypatch.setattr(candidate, "seal", fail_seal)
    result = service.execute(
        ("POST", f"/api/v1/tasks/{identity}/report", "fault"),
        request("report", 5),
        task_id=identity,
        action="report",
    )
    assert result[0] == 500
    assert service.get(identity) == before
    with candidate.snapshot() as connection:
        assert (
            connection.execute("SELECT count(*) FROM documents WHERE kind='report'").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM task_documents WHERE role='report'"
            ).fetchone()[0]
            == 0
        )
    assert not list(service.reports.reports_dir.glob("*.md"))
    if fault == "seal":
        with candidate.snapshot() as connection:
            assert (
                connection.execute(
                    "SELECT payload FROM events WHERE action='report_cleanup'"
                ).fetchone()[0]
                == '{"outcome":"removed"}'
            )
    assert not list(service.reports.reports_dir.glob("*.tmp"))


@pytest.mark.parametrize("size,expected", [(4 * 1024 * 1024, 201), (4 * 1024 * 1024 + 1, 413)])
def test_report_utf8_size_boundary(context, monkeypatch, size, expected):
    from tests.phase4.test_owner_application_integration import DATES

    service, candidate = context
    monkeypatch.setattr("app.phase3.reports.render_report", lambda document: "a" * size)
    result = service.execute(
        ("POST", "/api/v1/tasks", "size"),
        CREATE_ADAPTER.validate_python({"kind": "report", "request": DATES}),
    )
    assert result[0] == expected
    if expected == 413:
        assert json.loads(result[1])["code"] == "result_too_large"
        assert not list(service.reports.reports_dir.glob("*.md"))


def test_backup_manifest_must_include_all_registered_reports(context, tmp_path):
    service, candidate = context
    identity = simulated(service)
    assert (
        service.execute(
            ("POST", f"/api/v1/tasks/{identity}/report", "publish"),
            request("report", 5),
            task_id=identity,
            action="report",
        )[0]
        == 200
    )
    # Close fixture owner temporarily and reacquire its lifecycle afterward.
    candidate.owner.close()
    try:
        directory = tmp_path / "backup"
        backup(candidate.path, directory, service.reports.reports_dir, service.business.data_path)
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"] = {
            name: value
            for name, value in manifest["files"].items()
            if not name.startswith("reports/")
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        before = sha(candidate.path)
        with pytest.raises(BusinessError):
            verify_backup(directory)
        with pytest.raises(BusinessError):
            restore_candidate(directory, tmp_path / "restored")
        assert not (tmp_path / "restored").exists()
        assert sha(candidate.path) == before
    finally:
        candidate.owner.acquire()
