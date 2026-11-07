"""Kill actual continuation interpreters at acknowledged transaction windows."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.phase4.maintenance import report_inventory
from app.phase4.ownership import CandidateOwner
from app.phase4.service import TaskService
from app.phase4.store import CandidateStore
from tests.phase4.test_acceptance_state import context, create_budget, request

__all__ = ["context"]

CHILD = r"""
import json, os, sys, sqlite3
from pathlib import Path
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
from app.phase4.service import TaskService
from app.phase4.contracts import TaskApproval,TaskRejection,TaskSimulation,TaskPublish
database,data,reports=map(Path,sys.argv[1:4])
task,action,boundary=sys.argv[4:7]
body={'approve':TaskApproval,'reject':TaskRejection,'simulate':TaskSimulation,
      'report':TaskPublish}[action].model_validate_json(sys.argv[7])
def stop():
 print(json.dumps({'pid':os.getpid(),'boundary':boundary}),flush=True)
 sys.stdin.readline()
with CandidateOwner(database) as owner:
 store=CandidateStore(owner)
 if boundary=='recovery':
  original=store.seal
  def seal(*a,**kw):
   original(*a,**kw);stop()
  store.seal=seal
  store.recover_before_serving()
  raise AssertionError('no interrupted reservation')
 store.recover_before_serving()
 service=TaskService(store,data_path=data,reports_dir=reports,agent=None)
 if boundary=='before-request':stop()
 if boundary=='reserve':
  original=store.reserve
  def reserve(*a,**kw):
   result=original(*a,**kw);stop();return result
  store.reserve=reserve
 if boundary=='fsync':
  original=os.fsync
  def fsync(fd):
   original(fd);stop()
  os.fsync=fsync
 if boundary=='rename':
  original=Path.rename
  def rename(path,target):
   result=original(path,target)
   if path.suffix=='.tmp':stop()
   return result
  Path.rename=rename
 if boundary=='register':
  original=service.store.save
  def save(connection,identity,kind,value,**kw):
   result=original(connection,identity,kind,value,**kw)
   if kind=='report':stop()
   return result
  service.store.save=save
 if boundary in ('before-business','after-business'):
  name={'approve':'approve_budget_proposal','reject':'reject_budget_proposal',
        'simulate':'simulate_budget_changes','report':'generate_campaign_report'}[action]
  target=service.reports if action=='report' else service.business
  original=getattr(target,name)
  def business(*a,**kw):
   if boundary=='before-business':stop()
   result=original(*a,**kw)
   if boundary=='after-business':stop()
   return result
  setattr(target,name,business)
 if boundary in ('before-seal','after-seal'):
  original=store.seal
  def seal(*a,**kw):
   if boundary=='before-seal':stop()
   result=original(*a,**kw)
   if boundary=='after-seal':stop()
   return result
  store.seal=seal
 if boundary in ('before-commit','after-commit'):
  original=sqlite3.connect
  class Fault(sqlite3.Connection):
   def commit(self):
    row=self.execute("SELECT phase FROM operations WHERE key='kill'").fetchone()
    hit=row is not None and row[0]=='completed'
    if hit and boundary=='before-commit':stop()
    super().commit()
    if hit and boundary=='after-commit':stop()
  def connect(*a,**kw):
   kw['factory']=Fault
   return original(*a,**kw)
  sqlite3.connect=connect
 service.execute(('POST',f'/api/v1/tasks/{task}/{action}','kill'),body,task_id=task,action=action)
 if boundary=='response':stop()
 raise AssertionError('unreached '+boundary)
"""


@pytest.mark.parametrize(
    "action,boundary",
    [
        (action, boundary)
        for action in ("approve", "reject", "simulate", "report")
        for boundary in [
            "before-request",
            "reserve",
            "before-business",
            "after-business",
            "before-seal",
            "after-seal",
            "before-commit",
            "after-commit",
            "response",
            "recovery",
        ]
    ]
    + [("report", boundary) for boundary in ("fsync", "rename", "register")],
)
def test_continuation_process_kill_matrix(context, tmp_path, action, boundary, record_property):
    service, original = context
    task, proposal = create_budget(service)
    task_id = task["id"]
    if action in {"simulate", "report"}:
        result = service.execute(
            ("POST", f"/api/v1/tasks/{task_id}/approve", "setup-approve"),
            request("approve", sha=proposal["proposal_sha256"]),
            task_id=task_id,
            action="approve",
        )
        assert result[0] == 200
    if action == "report":
        result = service.execute(
            ("POST", f"/api/v1/tasks/{task_id}/simulate", "setup-simulate"),
            request("simulate", 4, 2, proposal["proposal_sha256"]),
            task_id=task_id,
            action="simulate",
        )
        assert result[0] == 200
    before = service.get(task_id)["data"]
    body = request(
        action, before["version"], 2 if action == "simulate" else 1, proposal["proposal_sha256"]
    )
    identity = ("POST", f"/api/v1/tasks/{task_id}/{action}", "kill")
    if boundary == "recovery":
        from app.phase3.store import digest

        original.reserve(
            "POST",
            identity[1].encode(),
            b"kill",
            digest(body.model_dump(mode="json")),
            task_id=task_id,
        )
    original.owner.close()
    child = None
    try:
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        with (tmp_path / "continuation.stderr.log").open("wb") as stderr:
            child = subprocess.Popen(
                [
                    sys._base_executable,
                    "-u",
                    "-c",
                    CHILD,
                    str(original.path),
                    str(service.business.data_path),
                    str(service.reports.reports_dir),
                    task_id,
                    action,
                    boundary,
                    body.model_dump_json(),
                ],
                cwd=tmp_path,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(child.stdout.readline)
                try:
                    ack = json.loads(pending.result(timeout=20))
                except BaseException:
                    child.kill()
                    child.wait(timeout=5)
                    raise
            assert ack == {"pid": child.pid, "boundary": boundary}
            child.kill()
            child.wait(timeout=5)
        with CandidateOwner(original.path) as owner:
            candidate = CandidateStore(owner)
            committed = boundary in {"after-commit", "response"}
            absent = boundary == "before-request"
            assert candidate.recover_before_serving() == (0 if committed or absent else 1)
            restarted = TaskService(
                candidate,
                data_path=service.business.data_path,
                reports_dir=service.reports.reports_dir,
                agent=None,
            )
            current = restarted.get(task_id)["data"]
            assert current["active_operation_id"] is None
            assert current["version"] == before["version"] + int(committed)
            assert current["status"] == (
                {
                    "approve": "approved",
                    "reject": "rejected",
                    "simulate": "simulated",
                    "report": "completed",
                }[action]
                if committed
                else before["status"]
            )
            with candidate.snapshot() as connection:
                tables = (
                    "tasks",
                    "operations",
                    "task_events",
                    "documents",
                    "events",
                    "task_documents",
                )
                persisted = {
                    t: [tuple(r) for r in connection.execute(f"SELECT * FROM {t}")] for t in tables
                }
            operation = candidate.lookup("POST", identity[1].encode(), b"kill")
            if absent:
                assert operation is None
            else:
                assert operation["phase"] == ("completed" if committed else "interrupted")
                replay = restarted.execute(identity, body, task_id=task_id, action=action)
                assert replay == (operation["sealed_http_status"], operation["response"])
                assert replay[0] == (200 if committed else 409)
                with candidate.snapshot() as connection:
                    assert persisted == {
                        t: [tuple(r) for r in connection.execute(f"SELECT * FROM {t}")]
                        for t in tables
                    }
            if committed and action == "report":
                report = restarted.reports.get_report(current["report_id"])
                assert report["content"]
            record_property(
                "continuation_kill",
                json.dumps(
                    {
                        "ack": ack,
                        "state": current,
                        "operation_phase": None if absent else operation["phase"],
                        "power_loss": "UNPROVEN",
                    }
                ),
            )
        inventory = report_inventory(original.path, service.reports.reports_dir)
        assert not inventory["missing"] and not inventory["damaged"]
        assert len(inventory["temporary"]) == int(action == "report" and boundary == "fsync")
        assert len(inventory["orphan"]) == int(
            action == "report"
            and boundary
            in {
                "rename",
                "register",
                "after-business",
                "before-seal",
                "after-seal",
                "before-commit",
            }
        )
        record_property("continuation_files", json.dumps(inventory))
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()
        original.owner.acquire()
