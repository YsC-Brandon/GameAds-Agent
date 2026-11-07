"""Real backup/restore process interruption preserves physical source and diagnostics."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.phase4.maintenance import backup, restore_candidate, sha, verify_backup
from tests.phase4.test_acceptance_state import context

__all__ = ["context"]

CHILD = r"""
import json,os,sys,shutil
from pathlib import Path
from app.phase4.maintenance import backup,restore_candidate
phase=sys.argv[1]
database,destination,reports,data=map(Path,sys.argv[2:6])
original=shutil.copyfile
def copy(source,target,*a,**kw):
 result=original(source,target,*a,**kw)
 if Path(target).is_relative_to(destination):
  print(json.dumps({'pid':os.getpid(),'target':str(target)}),flush=True)
  sys.stdin.readline()
 return result
shutil.copyfile=copy
if phase=='backup':backup(database,destination,reports,data)
else:restore_candidate(database,destination)
raise AssertionError('boundary not reached')
"""


@pytest.mark.parametrize("phase", ["backup", "restore"])
def test_real_interruption_repeat_recovery_preserves_physical_set(
    context, tmp_path, phase, record_property
):
    service, store = context
    store.owner.close()
    child = None
    try:
        source = store.path
        if phase == "restore":
            source = tmp_path / "backup"
            backup(store.path, source, service.reports.reports_dir, service.business.data_path)
        before = {
            p.name: sha(p)
            for p in tmp_path.glob(store.path.name + "*")
            if not p.name.endswith(".lock")
        }
        destination = tmp_path / "partial"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        with (tmp_path / "copy-child.stderr.log").open("wb") as stderr:
            child = subprocess.Popen(
                [
                    sys._base_executable,
                    "-u",
                    "-c",
                    CHILD,
                    phase,
                    str(source),
                    str(destination),
                    str(service.reports.reports_dir),
                    str(service.business.data_path),
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
            assert ack["pid"] == child.pid
            child.kill()
            child.wait(timeout=5)
        assert not (destination / "manifest.json").exists()
        partial = {
            p.relative_to(destination).as_posix(): sha(p)
            for p in destination.rglob("*")
            if p.is_file()
        }
        for _ in range(2):
            with pytest.raises(FileExistsError):
                if phase == "backup":
                    backup(
                        source, destination, service.reports.reports_dir, service.business.data_path
                    )
                else:
                    restore_candidate(source, destination)
            assert partial == {
                p.relative_to(destination).as_posix(): sha(p)
                for p in destination.rglob("*")
                if p.is_file()
            }
        complete = tmp_path / "fresh-recovery"
        if phase == "backup":
            backup(source, complete, service.reports.reports_dir, service.business.data_path)
        else:
            restore_candidate(source, complete)
        assert verify_backup(complete)["complete"]
        assert before == {
            p.name: sha(p)
            for p in tmp_path.glob(store.path.name + "*")
            if not p.name.endswith(".lock")
        }
        record_property(
            "copy_kill",
            json.dumps(
                {
                    "ack": ack,
                    "physical": before,
                    "retained_partial": partial,
                    "r4_old_active": "UNPROVEN",
                    "power_loss": "UNPROVEN",
                }
            ),
        )
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()
        store.owner.acquire()
