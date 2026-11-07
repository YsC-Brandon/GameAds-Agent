"""Real Windows/SQLite G01 candidate tests; no active library or model calls."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from app.phase3.store import BusinessError, digest
from app.phase4.ownership import CandidateOwner, OwnershipError
from app.phase4.store import CandidateStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows candidate ownership")
FP = digest({"synthetic": True})


@pytest.fixture
def candidate(tmp_path):
    with CandidateOwner(tmp_path / "candidate.sqlite3") as owner:
        store = CandidateStore(owner)
        store.initialize_empty_candidate()
        yield store


def create(store, key=b"create", kind="analysis"):
    operation, fresh = store.reserve("POST", b"/api/v1/tasks", key, FP)
    assert fresh
    task_id = store.create_submitted(
        operation["operation_id"],
        kind,
        validated_request=None if kind == "analysis" else {"synthetic": True},
    )
    return operation["operation_id"], task_id


def test_candidate_atomic_create_seal_and_exact_replay(candidate):
    op, task = create(candidate)
    candidate.mark_running(op, 1)
    before = None
    with candidate.snapshot() as connection:
        before = tuple(connection.execute("SELECT status,version FROM tasks").fetchone())
        assert before == ("running", 2)
    with pytest.raises(RuntimeError, match="before commit"):
        with candidate.unit_of_work(op) as connection:
            connection.execute("UPDATE tasks SET status='completed',version=3 WHERE id=?", (task,))
            candidate.event(connection, task, "completed", "running", "completed")
            candidate.seal(connection, op, 201, b'{"synthetic": "exact bytes"}')
            raise RuntimeError("before commit")
    with candidate.snapshot() as connection:
        assert tuple(connection.execute("SELECT status,version FROM tasks").fetchone()) == before
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 2
        assert connection.execute("SELECT phase FROM operations").fetchone()[0] == "in_progress"
    body = b'{ "synthetic" : "response lost after commit" }'
    with candidate.unit_of_work(op) as connection:
        connection.execute("UPDATE tasks SET status='completed',version=3 WHERE id=?", (task,))
        candidate.event(connection, task, "completed", "running", "completed")
        candidate.seal(connection, op, 201, body)
    replay, fresh = candidate.reserve("POST", b"/api/v1/tasks", b"create", FP)
    assert not fresh and replay["response"] == body and replay["sealed_http_status"] == 201
    assert replay["operation_id"] == op
    with pytest.raises(BusinessError, match="idempotency_key_reused"):
        candidate.reserve("POST", b"/api/v1/tasks", b"create", digest({"different": True}))
    with candidate.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 3


def test_real_thread_race_unique_key_and_single_task_occupancy(candidate):
    barrier = threading.Barrier(8)

    def reserve(_):
        barrier.wait()
        return candidate.reserve("POST", b"/api/v1/tasks", b"same-key", FP)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(fresh for _, fresh in results) == 1
    assert len({row["operation_id"] for row, _ in results}) == 1
    op = results[0][0]["operation_id"]
    task = candidate.create_submitted(op, "budget_review", validated_request={"synthetic": True})
    # Admission/state checking above this storage unit remains G03/G04 work.
    with candidate.unit_of_work(op) as connection:
        connection.execute(
            "UPDATE tasks SET status='awaiting_approval',version=3 WHERE id=?", (task,)
        )
        candidate.seal(connection, op, 201, b"{}")
    action_path = f"/api/v1/tasks/{task}/approve".encode()
    barrier = threading.Barrier(8)

    def action(index):
        barrier.wait()
        try:
            return candidate.reserve("POST", action_path, f"key-{index}".encode(), FP, task_id=task)
        except BusinessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        actions = list(pool.map(action, range(8)))
    assert actions.count("task_busy") == 7
    with candidate.snapshot() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM operations WHERE task_id=? AND phase='in_progress'", (task,)
            ).fetchone()[0]
            == 1
        )


def test_database_constraints_fk_replacement_and_seal_immutability(candidate):
    op, task = create(candidate)
    with candidate.unit_of_work(op) as connection:
        for sql, parameters in (
            ("UPDATE operations SET phase='completed' WHERE operation_id=?", (op,)),
            ("UPDATE operations SET key=' x' WHERE operation_id=?", (op,)),
            ("UPDATE tasks SET version=0 WHERE id=?", (task,)),
            ("INSERT INTO task_documents VALUES(?,?,'proposal')", (task, "missing")),
            ("DELETE FROM tasks WHERE id=?", (task,)),
            ("INSERT OR REPLACE INTO tasks SELECT * FROM tasks WHERE id=?", (task,)),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, parameters)
        # The unique index must work even when SQL bypasses the Python guard.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO operations SELECT ?,method,path,?,fingerprint,task_id,phase,"
                "created_at,updated_at,NULL,NULL FROM operations WHERE operation_id=?",
                ("b" * 32, "different-key", op),
            )
        candidate.seal(connection, op, 422, b'{"code":"synthetic_failure"}')
    with candidate._transaction() as connection:
        for sql in (
            "UPDATE operations SET response=x'7b7d'",
            "DELETE FROM operations",
            "INSERT OR REPLACE INTO operations SELECT * FROM operations",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)


def test_reads_never_initialize_upgrade_or_recover(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    with CandidateOwner(path) as owner:
        store = CandidateStore(owner)
        with pytest.raises(BusinessError, match="database_busy"):
            store.lookup("POST", b"/api/v1/tasks", b"absent")
        assert not path.exists()
        store.initialize_empty_candidate()
        with pytest.raises(FileExistsError):
            store.initialize_empty_candidate()
        with store._transaction() as connection:
            connection.execute("PRAGMA user_version=99")
        before = path.read_bytes()
        with pytest.raises(BusinessError, match="schema_upgrade_required"):
            store.lookup("POST", b"/api/v1/tasks", b"absent")
        assert path.read_bytes() == before


CHILD = """
import json, sys
from pathlib import Path
from app.phase3.store import digest
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
owner = CandidateOwner(Path(sys.argv[1])).acquire()
store = CandidateStore(owner)
store.initialize_empty_candidate()
# An independent read snapshot pins WAL history while writes commit. It is a
# fixture connection in the owning process, killed together with that process.
anchor = store._connect(readonly=True)
anchor.execute("BEGIN")
anchor.execute("SELECT count(*) FROM tasks").fetchone()
fp = digest({"synthetic": True})
ops = []
for key, kind in ((b"submitted", "analysis"), (b"running", "analysis"),
                  (b"continuation", "budget_review"), (b"success", "analysis")):
    from app.phase3.contracts import BudgetRequest
    from tests.phase4.test_owner_application_integration import budget
    request=BudgetRequest.model_validate(budget()).model_dump(mode='json')
    fingerprint=fp if kind=='analysis' else digest({'kind':kind,'request':request,'retry_of':None})
    op, _ = store.reserve("POST", b"/api/v1/tasks", key, fingerprint)
    task = store.create_submitted(op["operation_id"], kind,
        validated_request=None if kind=="analysis" else request)
    if key == b"running":
        store.mark_running(op["operation_id"], 1)
    if key in (b"continuation", b"success"):
        store.mark_running(op["operation_id"], 1)
        with store.unit_of_work(op["operation_id"]) as c:
            target = "awaiting_approval" if key==b"continuation" else "completed"
            c.execute("UPDATE tasks SET status=?,version=3 WHERE id=?",(target,task))
            store.event(c,task,target,"running",target)
            if key==b"continuation":
                from app.phase3.store import Store
                proposal = 'c'*32
                payload={'source':{'dataset_sha256':'a'*64},'request':request}
                Store.save(c,proposal,'proposal',{'id':proposal,'status':'approved','version':2,
                    'payload':payload,'proposal_sha256':digest(payload),
                    'approval':{'decision':'approved','proposal_sha256':digest(payload),
                                'reviewed_version':1},'simulation':None},new=True)
                c.execute("INSERT INTO task_documents VALUES(?,?,'proposal')",(task,proposal))
                c.execute("UPDATE tasks SET source_sha256=? WHERE id=?",('a'*64,task))
            else:
                from app.agent.contracts import AgentResponse
                result=AgentResponse(request_id='synthetic',status='completed',final_answer='offline')
                c.execute("UPDATE tasks SET agent_result=? WHERE id=?",
                    (result.model_dump_json(),task))
            from app.phase4.service import TaskService
            response = {'status':'ok',
                'data':TaskService.record(c,task)|{'active_operation_id':None}}
            store.seal(c,op["operation_id"],201,json.dumps(response).encode())
        if key==b"continuation":
            approval,_ = store.reserve('POST',f'/api/v1/tasks/{task}/approve'.encode(),
                b'approve',fp,task_id=task)
            with store.unit_of_work(approval['operation_id']) as c:
                c.execute("UPDATE tasks SET status='approved',version=4 WHERE id=?",(task,))
                store.event(c,task,'approved','awaiting_approval','approved')
                response = {'status':'ok',
                    'data':TaskService.record(c,task)|{'active_operation_id':None}}
                store.seal(c,approval['operation_id'],200,json.dumps(response).encode())
    if key == b"continuation":
        store.reserve("POST",f"/api/v1/tasks/{task}/simulate".encode(),b"simulate",fp,task_id=task)
    ops.append((key.decode(),task,op["operation_id"]))
store.reserve("POST",b"/api/v1/tasks",b"unattached",fp)
# Hold a genuine SQLite write transaction with an uncommitted change at kill.
with store._transaction() as c:
    c.execute("UPDATE tasks SET status='simulated',version=4 WHERE id=?",(ops[2][1],))
    print(json.dumps({"pid":__import__("os").getpid(),"ops":ops}),flush=True)
    sys.stdin.readline()
"""


@pytest.mark.parametrize("interrupt_recovery", [False, True])
def test_windows_owner_process_kill_wal_and_real_recovery(
    tmp_path, record_property, interrupt_recovery
):
    # Use the actual base interpreter to avoid a Windows venv launcher parent PID.
    path = tmp_path / "candidate.sqlite3"
    env = dict(os.environ)
    root = str(__import__("pathlib").Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = root
    with (tmp_path / "child.stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            [sys._base_executable, "-u", "-c", CHILD, str(path)],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        try:
            # Kill on a missing ack BEFORE waiting for the reader thread to exit.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(process.stdout.readline)
                try:
                    line = pending.result(timeout=15)
                except TimeoutError:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            ack = json.loads(line)
            assert ack["pid"] == process.pid
            with pytest.raises(OwnershipError):
                CandidateOwner(path).acquire()
            assert path.with_name(path.name + "-wal").stat().st_size > 0
            assert path.with_name(path.name + "-shm").exists()
            process.kill()
            process.wait(timeout=5)
            if interrupt_recovery:
                recovery_code = """
import os, sys
from pathlib import Path
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
class InterruptedRecovery(CandidateStore):
    @staticmethod
    def seal(connection, *args, **kwargs):
        CandidateStore.seal(connection, *args, **kwargs)
        print(os.getpid(), flush=True)
        sys.stdin.readline()
with CandidateOwner(Path(sys.argv[1])) as owner:
    InterruptedRecovery(owner).recover_before_serving()
"""
                recovering = subprocess.Popen(
                    [sys._base_executable, "-u", "-c", recovery_code, str(path)],
                    cwd=tmp_path,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                )
                try:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pending = pool.submit(recovering.stdout.readline)
                        try:
                            recovery_pid = int(pending.result(timeout=15))
                        except (TimeoutError, ValueError):
                            recovering.kill()
                            recovering.wait(timeout=5)
                            raise
                    assert recovery_pid == recovering.pid
                finally:
                    recovering.kill()
                    recovering.wait(timeout=5)
                    recovering.stdin.close()
                    recovering.stdout.close()
            with CandidateOwner(path) as owner:
                store = CandidateStore(owner)
                with pytest.raises(BusinessError, match="recovery_required"):
                    store.reserve("POST", b"/api/v1/tasks", b"before-recovery", FP)
                assert store.recover_before_serving() == 4
                with store.snapshot() as connection:
                    states = {
                        row["id"]: (row["status"], row["version"])
                        for row in connection.execute("SELECT * FROM tasks")
                    }
                    assert states[ack["ops"][0][1]] == ("interrupted", 2)
                    assert states[ack["ops"][1][1]] == ("interrupted", 3)
                    assert states[ack["ops"][2][1]] == ("approved", 4)
                    assert states[ack["ops"][3][1]] == ("completed", 3)
                    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    assert not connection.execute("PRAGMA foreign_key_check").fetchall()
                success = store.lookup("POST", b"/api/v1/tasks", b"success")
                from app.phase4.service import TaskService

                with store.snapshot() as connection:
                    expected = json.dumps(
                        {"status": "ok", "data": TaskService.record(connection, ack["ops"][3][1])}
                    ).encode()
                assert success["response"] == expected
                for key in (b"submitted", b"running", b"unattached"):
                    row, fresh = store.reserve("POST", b"/api/v1/tasks", key, FP)
                    assert not fresh and row["phase"] == "interrupted"
                    assert row["sealed_http_status"] == 409
                    assert json.loads(row["response"])["retryable"] is False
                assert store.checkpoint() == (0, 0, 0)
            # A second independent admission is idempotent; no duplicated recovery events.
            with CandidateOwner(path) as owner:
                assert CandidateStore(owner).recover_before_serving() == 0
            record_property("kill_ack", json.dumps(ack))
            record_property("recovered_states", json.dumps(states))
            record_property("recovery_interrupted_before_commit", str(interrupt_recovery))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()


def test_owner_close_waits_for_connections_and_rejects_new_work(candidate):
    opened, release, closed = threading.Event(), threading.Event(), threading.Event()

    def reader():
        with candidate.snapshot():
            opened.set()
            assert release.wait(5)

    def closer():
        candidate.owner.close()
        closed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(reader)
        assert opened.wait(5)
        closing = pool.submit(closer)
        assert not closed.wait(0.1)
        release.set()
        reading.result(timeout=5)
        closing.result(timeout=5)
    with pytest.raises(OwnershipError):
        candidate.lookup("POST", b"/api/v1/tasks", b"anything")
    # Restore fixture ownership solely so its context can perform final cleanup.
    candidate.owner.acquire()


def test_real_sqlite_writer_contention_preserves_reservation(candidate):
    op, task = create(candidate)
    blocker = sqlite3.connect(candidate.path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            candidate.mark_running(op, 1)
    finally:
        blocker.rollback()
        blocker.close()
    with candidate.snapshot() as connection:
        assert tuple(connection.execute("SELECT status,version FROM tasks").fetchone()) == (
            "submitted",
            1,
        )
    candidate.mark_running(op, 1)
    assert candidate.lookup("POST", b"/api/v1/tasks", b"create")["phase"] == "in_progress"


def test_real_windows_delete_sharing_conflict(candidate):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # Permit read/write, deny FILE_SHARE_DELETE. This is an actual Windows handle.
    handle = kernel.CreateFileW(str(candidate.path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    before = candidate.path.read_bytes()
    try:
        with pytest.raises(PermissionError):
            candidate.path.rename(candidate.path.with_suffix(".moved"))
        assert candidate.path.read_bytes() == before
    finally:
        assert kernel.CloseHandle(handle)
    # This verifies the real sharing failure only. No candidate switch or
    # rollback primitive is implemented or inferred from the failed rename.


def test_owner_and_recovery_lifecycle_cannot_be_reused(candidate):
    with pytest.raises(RuntimeError, match="One store lifecycle"):
        CandidateStore(candidate.owner)
    with pytest.raises(RuntimeError, match="Recovery cannot run"):
        candidate.recover_before_serving()
    with candidate.snapshot():
        with pytest.raises(OwnershipError, match="active transaction"):
            candidate.owner.close()
        with pytest.raises(RuntimeError, match="Nested"):
            candidate.lookup("POST", b"/api/v1/tasks", b"anything")
        with pytest.raises(RuntimeError, match="Nested"):
            candidate.checkpoint()
    candidate.owner.close()
    candidate.owner.acquire()
    with pytest.raises(RuntimeError, match="Stale store lifecycle"):
        candidate.lookup("POST", b"/api/v1/tasks", b"anything")
    with pytest.raises(RuntimeError, match="Stale store lifecycle"):
        candidate.checkpoint()
    with pytest.raises(RuntimeError, match="Stale store lifecycle"):
        candidate.recover_before_serving()


def test_schema_drift_rejected_even_with_matching_ledger(candidate):
    with candidate._transaction() as connection:
        connection.execute("DROP TRIGGER operation_sealed_immutable")
    with pytest.raises(BusinessError, match="schema_upgrade_required"):
        candidate.lookup("POST", b"/api/v1/tasks", b"anything")


def test_recovery_commit_admits_before_releasing_owner_lock(tmp_path, monkeypatch):
    path = tmp_path / "candidate.sqlite3"
    with CandidateOwner(path) as owner:
        store = CandidateStore(owner)
        store.initialize_empty_candidate()
        create(store)
    with CandidateOwner(path) as owner:
        store = CandidateStore(owner)
        with pytest.raises(BusinessError, match="recovery_required"):
            store.checkpoint()
        committed, finish = threading.Event(), threading.Event()
        original = store._transaction

        @contextmanager
        def pause_after_commit(**kwargs):
            with original(**kwargs) as connection:
                yield connection
            if kwargs.get("recovery"):
                committed.set()
                assert finish.wait(5)

        monkeypatch.setattr(store, "_transaction", pause_after_commit)
        with ThreadPoolExecutor(max_workers=1) as pool:
            recovery = pool.submit(store.recover_before_serving)
            try:
                assert committed.wait(5)
                # The first recovery has released its transaction lock but has
                # not returned. Admission must already be published atomically.
                _, fresh = store.reserve("POST", b"/api/v1/tasks", b"after-recovery", FP)
                assert fresh
                with pytest.raises(RuntimeError, match="Recovery cannot run"):
                    store.recover_before_serving()
            finally:
                finish.set()
            assert recovery.result(timeout=5) == 1


def test_failed_recovery_rolls_back_and_keeps_admission_closed(tmp_path, monkeypatch):
    path = tmp_path / "candidate.sqlite3"
    with CandidateOwner(path) as owner:
        store = CandidateStore(owner)
        store.initialize_empty_candidate()
        create(store)
    with CandidateOwner(path) as owner:
        store = CandidateStore(owner)
        original = store.seal

        def fail_after_seal(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("recovery failure before commit")

        monkeypatch.setattr(store, "seal", fail_after_seal)
        with pytest.raises(RuntimeError, match="recovery failure"):
            store.recover_before_serving()
        with store.snapshot() as connection:
            assert tuple(connection.execute("SELECT status,version FROM tasks").fetchone()) == (
                "submitted",
                1,
            )
            assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 1
            assert connection.execute("SELECT phase FROM operations").fetchone()[0] == "in_progress"
        with pytest.raises(BusinessError, match="recovery_required"):
            store.reserve("POST", b"/api/v1/tasks", b"not-admitted", FP)
        with pytest.raises(BusinessError, match="recovery_required"):
            store.checkpoint()
        monkeypatch.setattr(store, "seal", original)
        assert store.recover_before_serving() == 1
        assert store.checkpoint() == (0, 0, 0)


@pytest.mark.parametrize("drift", ["missing-ledger", "non-wal"])
def test_recovery_rejects_candidate_configuration_drift(tmp_path, drift):
    path = tmp_path / "candidate.sqlite3"
    with CandidateOwner(path) as owner:
        store = CandidateStore(owner)
        store.initialize_empty_candidate()
    with sqlite3.connect(path, isolation_level=None) as connection:
        if drift == "missing-ledger":
            connection.execute("DROP TABLE schema_migrations")
        else:
            assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    connection.close()
    with CandidateOwner(path) as owner:
        store = CandidateStore(owner)
        code = "schema_upgrade_required" if drift == "missing-ledger" else "candidate_wal_required"
        with pytest.raises(BusinessError, match=code):
            store.recover_before_serving()
        with pytest.raises(BusinessError, match="recovery_required"):
            store.reserve("POST", b"/api/v1/tasks", b"not-admitted", FP)


def test_documents_relationship_events_and_seal_share_one_commit(candidate):
    from app.phase3.store import Store

    op, task = create(candidate, kind="budget_review")
    document = "c" * 32
    payload = {"synthetic": "storage-only fixture, not a validated proposal"}

    def publish(connection):
        Store.save(connection, document, "proposal", payload, new=True)
        connection.execute("INSERT INTO task_documents VALUES(?,?,'proposal')", (task, document))
        connection.execute(
            "UPDATE tasks SET status='awaiting_approval',version=3 WHERE id=?", (task,)
        )
        candidate.event(connection, task, "proposal_created", "running", "awaiting_approval")
        candidate.seal(connection, op, 201, b"{}")

    candidate.mark_running(op, 1)
    with pytest.raises(RuntimeError, match="rollback all"):
        with candidate.unit_of_work(op) as connection:
            publish(connection)
            raise RuntimeError("rollback all")
    with candidate.snapshot() as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM task_documents").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM task_events").fetchone()[0] == 2
    with candidate.unit_of_work(op) as connection:
        publish(connection)
    with candidate.snapshot() as connection:
        assert Store.load(connection, document, "proposal") == payload
        assert tuple(
            connection.execute("SELECT task_id,document_id FROM task_documents").fetchone()
        ) == (
            task,
            document,
        )
        assert connection.execute("SELECT phase FROM operations").fetchone()[0] == "completed"
