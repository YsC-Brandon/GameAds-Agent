"""Real file/SQLite fault boundaries; injected exceptions are not power loss."""

import json
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from app.phase3.contracts import ReportRequest
from app.phase3.store import BusinessError
from app.phase4.contracts import CREATE_ADAPTER
from app.phase4.maintenance import backup, restore_candidate, sha, verify_backup
from tests.phase4.test_acceptance_failures import simulated
from tests.phase4.test_acceptance_state import context, create_budget, request

__all__ = ["context"]


@pytest.mark.parametrize(
    "boundary", ["fsync", "rename", "register", "seal", "cleanup", "register-cleanup"]
)
def test_report_boundary_failure_has_no_false_registration(
    context, monkeypatch, boundary, record_property
):
    service, store = context
    identity = simulated(service)
    before = service.get(identity)
    original_save, original_seal = service.store.save, store.seal
    original_unlink, original_rename = Path.unlink, Path.rename

    def seal(connection, operation, status, response, **kwargs):
        if status == 200 and boundary in {"seal", "cleanup"}:
            raise sqlite3.OperationalError("synthetic seal failure")
        return original_seal(connection, operation, status, response, **kwargs)

    def save(connection, identity, kind, document, **kwargs):
        if kind == "report" and boundary in {"register", "register-cleanup"}:
            raise sqlite3.OperationalError("synthetic registration failure")
        return original_save(connection, identity, kind, document, **kwargs)

    def unlink(path, *args, **kwargs):
        if path.suffix == ".md" and boundary in {"cleanup", "register-cleanup"}:
            raise PermissionError("synthetic occupied final report")
        return original_unlink(path, *args, **kwargs)

    def rename(path, target):
        if path.suffix == ".tmp" and boundary == "rename":
            raise PermissionError("synthetic replace failure")
        return original_rename(path, target)

    if boundary == "fsync":

        def fail_fsync(fd):
            raise OSError("synthetic file flush failure")

        monkeypatch.setattr("app.phase3.reports.os.fsync", fail_fsync)
    monkeypatch.setattr(service.store, "save", save)
    monkeypatch.setattr(store, "seal", seal)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "rename", rename)
    operation_identity = ("POST", f"/api/v1/tasks/{identity}/report", "fault")
    result = service.execute(
        operation_identity, request("report", 5), task_id=identity, action="report"
    )
    assert result[0] == 500
    assert service.get(identity) == before
    assert (
        service.execute(operation_identity, request("report", 5), task_id=identity, action="report")
        == result
    )
    with store.snapshot() as connection:
        state = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in ("tasks", "operations", "task_documents", "task_events", "events")
        }
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
        operation = connection.execute("SELECT * FROM operations WHERE key='fault'").fetchone()
        assert operation["response"] == result[1] and operation["sealed_http_status"] == 500
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    final = list(service.reports.reports_dir.glob("*.md"))
    assert len(final) == (1 if boundary in {"cleanup", "register-cleanup"} else 0)
    assert not list(service.reports.reports_dir.glob("*.tmp"))
    if boundary in {"cleanup", "register-cleanup"}:
        assert any('"outcome":"retained"' in str(row) for row in state["events"])
    record_property(
        "fault_evidence",
        json.dumps(
            {
                "injection": boundary,
                "state": state,
                "files": {p.name: sha(p) for p in final},
                "active_generation": "explicit candidate; no active selector",
                "recovery": "sealed failure replays; orphan never imported",
                "power_loss": "UNPROVEN",
            },
            default=str,
        ),
    )


@pytest.mark.parametrize("commit_outcome", ["before", "after"])
def test_sqlite_commit_outcome_is_reconciled_without_reexecution(
    context, monkeypatch, commit_outcome
):
    service, store = context
    original = store.unit_of_work
    injected = False

    @contextmanager
    def fault(operation):
        nonlocal injected
        with original(operation) as connection:
            yield connection
            row = connection.execute(
                "SELECT phase FROM operations WHERE operation_id=?", (operation,)
            ).fetchone()
            hit = row[0] == "completed" and not injected
            if hit:
                injected = True
                if commit_outcome == "before":
                    raise sqlite3.OperationalError("before COMMIT")
        if hit and commit_outcome == "after":
            raise sqlite3.OperationalError("COMMIT succeeded but acknowledgement lost")

    monkeypatch.setattr(store, "unit_of_work", fault)
    body = CREATE_ADAPTER.validate_python(
        {"kind": "report", "request": {"start_date": "2026-10-01", "end_date": "2026-10-01"}}
    )
    identity = ("POST", "/api/v1/tasks", "commit")
    result = service.execute(identity, body)
    assert injected and result[0] == (500 if commit_outcome == "before" else 201)
    assert service.execute(identity, body) == result
    with store.snapshot() as connection:
        op = connection.execute("SELECT * FROM operations WHERE key='commit'").fetchone()
        assert op["response"] == result[1] and op["phase"] == "completed"
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM documents WHERE kind='report'").fetchone()[
            0
        ] == (0 if commit_outcome == "before" else 1)


@pytest.mark.parametrize("phase", ["backup", "restore"])
def test_interrupted_copy_retains_diagnostics_and_never_overwrites_source(
    context, monkeypatch, tmp_path, phase
):
    service, store = context
    store.owner.close()
    try:
        original = sha(store.path)
        directory = tmp_path / "backup"
        real_copy = shutil.copyfile
        if phase == "restore":
            backup(store.path, directory, service.reports.reports_dir, service.business.data_path)
        target = directory if phase == "backup" else tmp_path / "restored"

        def partial(source, destination, **kwargs):
            if Path(destination).is_relative_to(target):
                Path(destination).write_bytes(b"partial synthetic copy")
                raise OSError("injected copy interruption")
            return real_copy(source, destination, **kwargs)

        monkeypatch.setattr(shutil, "copyfile", partial)
        with pytest.raises(OSError):
            if phase == "backup":
                backup(
                    store.path, directory, service.reports.reports_dir, service.business.data_path
                )
            else:
                restore_candidate(directory, target)
        assert sha(store.path) == original
        assert target.exists() and not (target / "manifest.json").exists()
        with pytest.raises((OSError, BusinessError, ValueError)):
            verify_backup(target)
        with pytest.raises(FileExistsError):
            if phase == "backup":
                backup(
                    store.path, directory, service.reports.reports_dir, service.business.data_path
                )
            else:
                restore_candidate(directory, target)
    finally:
        store.owner.acquire()


def test_registered_report_get_never_recreates_missing_directory(context):
    service, store = context
    body = CREATE_ADAPTER.validate_python(
        {"kind": "report", "request": {"start_date": "2026-10-01", "end_date": "2026-10-01"}}
    )
    result = service.execute(("POST", "/api/v1/tasks", "read-only-report"), body)
    assert result[0] == 201
    report_id = json.loads(result[1])["data"]["report_id"]
    shutil.rmtree(service.reports.reports_dir)
    with pytest.raises(BusinessError, match="report_file_missing"):
        service.reports.get_report(report_id)
    assert not service.reports.reports_dir.exists()
    with store.snapshot() as connection:
        assert (
            connection.execute("SELECT count(*) FROM documents WHERE kind='report'").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("entry", ["legacy", "task"])
@pytest.mark.parametrize("outcome", ["before", "after"])
def test_real_sqlite_commit_boundary_keeps_http_result_and_report_consistent(
    context, monkeypatch, entry, outcome, record_property
):
    service, store = context
    real_connect = sqlite3.connect
    injected = []

    class FaultConnection(sqlite3.Connection):
        def commit(self):
            hit = (
                not injected
                and self.in_transaction
                and self.execute("SELECT count(*) FROM documents WHERE kind='report'").fetchone()[0]
                == 1
            )
            if hit:
                injected.append(outcome)
                if outcome == "before":
                    raise sqlite3.OperationalError("real connection before COMMIT")
            super().commit()
            if hit and outcome == "after":
                raise sqlite3.OperationalError("real connection after successful COMMIT")

    def connect(*args, **kwargs):
        kwargs["factory"] = FaultConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    request_data = {"start_date": "2026-10-01", "end_date": "2026-10-01"}
    if entry == "legacy":
        if outcome == "before":
            with pytest.raises(BusinessError, match="report_write_failed"):
                service.reports.generate_campaign_report(ReportRequest(**request_data))
        else:
            report = service.reports.generate_campaign_report(ReportRequest(**request_data))
            assert (
                service.reports.get_report(report["id"])["content_sha256"]
                == report["content_sha256"]
            )
    else:
        body = CREATE_ADAPTER.validate_python({"kind": "report", "request": request_data})
        result = service.execute(("POST", "/api/v1/tasks", "real-commit"), body)
        assert result[0] == (500 if outcome == "before" else 201)
        assert service.execute(("POST", "/api/v1/tasks", "real-commit"), body) == result
    assert injected == [outcome]
    expected = int(outcome == "after")
    with store.snapshot() as connection:
        assert (
            connection.execute("SELECT count(*) FROM documents WHERE kind='report'").fetchone()[0]
            == expected
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(list(service.reports.reports_dir.glob("*.md"))) == expected
    assert not list(service.reports.reports_dir.glob("*.tmp"))
    record_property(
        "real_commit_evidence",
        json.dumps(
            {
                "entry": entry,
                "injection": outcome,
                "registered_reports": expected,
                "final_files": expected,
                "power_loss": "UNPROVEN",
            }
        ),
    )


@pytest.mark.parametrize("action", ["approve", "reject", "simulate", "report"])
@pytest.mark.parametrize("outcome", ["before", "after"])
def test_continuation_real_commit_boundary_preserves_one_decision(
    context, monkeypatch, action, outcome, record_property
):
    service, store = context
    task, proposal = create_budget(service)
    task_id = task["id"]
    if action in {"simulate", "report"}:
        assert (
            service.execute(
                ("POST", f"/api/v1/tasks/{task_id}/approve", "approve"),
                request("approve", sha=proposal["proposal_sha256"]),
                task_id=task_id,
                action="approve",
            )[0]
            == 200
        )
    if action == "report":
        assert (
            service.execute(
                ("POST", f"/api/v1/tasks/{task_id}/simulate", "simulate"),
                request("simulate", 4, 2, proposal["proposal_sha256"]),
                task_id=task_id,
                action="simulate",
            )[0]
            == 200
        )
    before = service.get(task_id)["data"]
    real_connect = sqlite3.connect
    injected = []

    class FaultConnection(sqlite3.Connection):
        def commit(self):
            row = self.execute(
                "SELECT phase,sealed_http_status FROM operations WHERE key='actual-boundary'"
            ).fetchone()
            hit = not injected and row is not None and row[0] == "completed" and row[1] == 200
            if hit:
                injected.append(outcome)
                if outcome == "before":
                    raise sqlite3.OperationalError("continuation before COMMIT")
            super().commit()
            if hit and outcome == "after":
                raise sqlite3.OperationalError("continuation successful COMMIT, lost ack")

    def connect(*args, **kwargs):
        kwargs["factory"] = FaultConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    body = request(
        action, before["version"], 2 if action == "simulate" else 1, proposal["proposal_sha256"]
    )
    identity = ("POST", f"/api/v1/tasks/{task_id}/{action}", "actual-boundary")
    result = service.execute(identity, body, task_id=task_id, action=action)
    assert injected == [outcome]
    assert result[0] == (500 if outcome == "before" else 200)
    assert service.execute(identity, body, task_id=task_id, action=action) == result
    after = service.get(task_id)["data"]
    if outcome == "before":
        assert after == before
    else:
        assert after["version"] == before["version"] + 1
        assert (
            after["status"]
            == {
                "approve": "approved",
                "reject": "rejected",
                "simulate": "simulated",
                "report": "completed",
            }[action]
        )
    with store.snapshot() as connection:
        op = connection.execute("SELECT * FROM operations WHERE key='actual-boundary'").fetchone()
        assert op["response"] == result[1] and op["sealed_http_status"] == result[0]
        assert op["phase"] == "completed"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert len(list(service.reports.reports_dir.glob("*.md"))) == int(
        action == "report" and outcome == "after"
    )
    record_property(
        "continuation_commit_evidence",
        json.dumps(
            {
                "action": action,
                "injection": outcome,
                "task_before": before,
                "task_after": after,
                "sealed_status": result[0],
                "power_loss": "UNPROVEN",
            }
        ),
    )
