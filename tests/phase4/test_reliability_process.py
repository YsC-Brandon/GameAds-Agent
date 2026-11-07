"""Kill actual interpreters at report transaction boundaries, then reopen SQLite.

The child acknowledges the reached production method before the parent kills it.
This verifies process interruption only, never storage power-loss durability.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.phase4.maintenance import backup, report_inventory, sha, verify_backup
from app.phase4.ownership import CandidateOwner
from app.phase4.service import TaskService
from app.phase4.store import CandidateStore
from tests.phase4.test_acceptance_state import context

__all__ = ["context"]

CHILD = r"""
import json, os, sys
from pathlib import Path
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
from app.phase4.service import TaskService
from app.phase4.contracts import CREATE_ADAPTER
database, data, reports = map(Path, sys.argv[1:4])
boundary = sys.argv[4]
def stop():
    print(json.dumps({'pid':os.getpid(),'boundary':boundary}),flush=True)
    sys.stdin.readline()
with CandidateOwner(database) as owner:
    store = CandidateStore(owner)
    store.recover_before_serving()
    service = TaskService(store,data_path=data,reports_dir=reports,agent=None)
    if boundary == 'reserve':
        original = store.reserve
        def reserve(*a,**kw):
            result = original(*a,**kw)
            stop()
            return result
        store.reserve = reserve
    elif boundary == 'wal':
        original = store.mark_running
        def stage(*a,**kw):
            result = original(*a,**kw)
            with store._transaction() as connection:
                connection.execute('UPDATE tasks SET updated_at=updated_at')
                stop()
            return result
        store.mark_running = stage
    elif boundary in ('submitted','running'):
        name = 'create_submitted' if boundary == 'submitted' else 'mark_running'
        original = getattr(store,name)
        def stage(*a,**kw):
            result = original(*a,**kw)
            stop()
            return result
        setattr(store,name,stage)
    elif boundary == 'fsync':
        import app.phase3.reports as publication
        original = publication.os.fsync
        def fsync(fd):
            original(fd)
            stop()
        publication.os.fsync = fsync
    elif boundary == 'rename':
        original = Path.rename
        def rename(path,target):
            result = original(path,target)
            if path.suffix == '.tmp': stop()
            return result
        Path.rename = rename
    elif boundary == 'register':
        original = service.store.save
        def save(connection,identity,kind,value,**kw):
            result = original(connection,identity,kind,value,**kw)
            if kind == 'report': stop()
            return result
        service.store.save = save
    elif boundary == 'seal':
        original = store.seal
        def seal(*a,**kw):
            result = original(*a,**kw)
            stop()
            return result
        store.seal = seal
    result = service.execute(('POST','/api/v1/tasks','crash'),
        CREATE_ADAPTER.validate_python({'kind':'report','request':{
        'start_date':'2026-10-01','end_date':'2026-10-01'}}))
    if boundary == 'response': stop()
    raise AssertionError('unreached boundary '+boundary)
"""


@pytest.mark.parametrize(
    "boundary",
    ["reserve", "submitted", "running", "fsync", "rename", "register", "seal", "response"],
)
def test_report_process_kill_keeps_atomic_state_and_never_reexecutes(
    context, tmp_path, boundary, record_property
):
    service, store = context
    store.owner.close()
    process = None
    try:
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        with (tmp_path / "crash.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(
                [
                    sys._base_executable,
                    "-u",
                    "-c",
                    CHILD,
                    str(store.path),
                    str(service.business.data_path),
                    str(service.reports.reports_dir),
                    boundary,
                ],
                cwd=tmp_path,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(process.stdout.readline)
                try:
                    ack = json.loads(pending.result(timeout=15))
                except BaseException:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            assert ack == {"pid": process.pid, "boundary": boundary}
            process.kill()
            process.wait(timeout=5)
        files_before = {p.name: p.stat().st_size for p in tmp_path.rglob("*") if p.is_file()}
        with CandidateOwner(store.path) as owner:
            recovered = CandidateStore(owner)
            assert recovered.recover_before_serving() == (0 if boundary == "response" else 1)
            reopened = TaskService(
                recovered,
                data_path=service.business.data_path,
                reports_dir=service.reports.reports_dir,
                agent=None,
            )
            operation = recovered.lookup("POST", b"/api/v1/tasks", b"crash")
            assert operation["phase"] == ("completed" if boundary == "response" else "interrupted")
            with recovered.snapshot() as connection:
                state = {
                    table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                    for table in ("tasks", "operations", "task_events", "task_documents")
                }
                assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert not connection.execute("PRAGMA foreign_key_check").fetchall()
                assert connection.execute(
                    "SELECT count(*) FROM documents WHERE kind='report'"
                ).fetchone()[0] == (1 if boundary == "response" else 0)
            if operation["task_id"]:
                task = reopened.get(operation["task_id"])["data"]
                assert task["status"] == ("completed" if boundary == "response" else "interrupted")
                assert task["active_operation_id"] is None
            from app.phase4.contracts import CREATE_ADAPTER

            body = CREATE_ADAPTER.validate_python(
                {
                    "kind": "report",
                    "request": {"start_date": "2026-10-01", "end_date": "2026-10-01"},
                }
            )
            assert reopened.execute(("POST", "/api/v1/tasks", "crash"), body) == (
                operation["sealed_http_status"],
                operation["response"],
            )
        inventory = report_inventory(store.path, service.reports.reports_dir)
        assert not inventory["missing"] and not inventory["damaged"]
        assert len(inventory["orphan"]) == (1 if boundary in {"rename", "register", "seal"} else 0)
        assert len(inventory["temporary"]) == (1 if boundary == "fsync" else 0)
        record_property(
            "process_fault_evidence",
            json.dumps(
                {
                    "ack": ack,
                    "state": state,
                    "files_at_kill": files_before,
                    "inventory": inventory,
                    "power_loss": "UNPROVEN",
                },
                default=str,
            ),
        )
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
        store.owner.acquire()


def test_backup_of_killed_wal_source_preserves_entire_physical_set(
    context, tmp_path, record_property
):
    service, store = context
    store.owner.close()
    process = None
    try:
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        with (tmp_path / "wal-backup.stderr.log").open("wb") as stderr:
            process = subprocess.Popen(
                [
                    sys._base_executable,
                    "-u",
                    "-c",
                    CHILD,
                    str(store.path),
                    str(service.business.data_path),
                    str(service.reports.reports_dir),
                    "wal",
                ],
                cwd=tmp_path,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(process.stdout.readline)
                try:
                    ack = json.loads(pending.result(timeout=15))
                except BaseException:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            assert ack["pid"] == process.pid
            process.kill()
            process.wait(timeout=5)

        def physical():
            return {
                p.name: sha(p)
                for p in tmp_path.glob(store.path.name + "*")
                if not p.name.endswith(".owner.lock")
            }

        before = physical()
        assert store.path.name + "-wal" in before and store.path.name + "-shm" in before
        directory = tmp_path / "backup"
        manifest = backup(
            store.path, directory, service.reports.reports_dir, service.business.data_path
        )
        assert verify_backup(directory) == manifest
        assert physical() == before
        with CandidateOwner(directory / "database.sqlite3") as owner:
            copied = CandidateStore(owner)
            assert copied.recover_before_serving() == 1
            with copied.snapshot() as connection:
                assert connection.execute("SELECT status FROM tasks").fetchone()[0] == "interrupted"
        assert physical() == before
        record_property(
            "wal_backup_evidence",
            json.dumps(
                {
                    "physical_before_after": before,
                    "manifest": manifest,
                    "recovery": "working copy only",
                    "power_loss": "UNPROVEN",
                }
            ),
        )
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
        store.owner.acquire()
