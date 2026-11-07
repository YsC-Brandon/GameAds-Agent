"""Experimental selector evidence is scoped separately from frozen G01/G09 PASS."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.phase3.store import BusinessError
from app.phase4.generation import GenerationRehearsal
from app.phase4.maintenance import backup, initialize, sha
from app.phase4.ownership import OwnershipError


@pytest.fixture
def generations(tmp_path):
    source = tmp_path / "source.sqlite3"
    initialize(source)
    data = tmp_path / "dataset.csv"
    data.write_text("synthetic\n")
    reports = tmp_path / "reports"
    reports.mkdir()
    root = tmp_path / "instance"
    root.mkdir()
    backup(source, root / "old", reports, data)
    backup(source, root / "new", reports, data)
    rehearsal = GenerationRehearsal(root)
    with rehearsal.locked():
        old = rehearsal.prepare(root / "old")
        new = rehearsal.prepare(root / "new")
        rehearsal.bootstrap(old)
    return rehearsal, old, new


def test_rehearsal_success_has_one_locator_and_preserves_old_physical_set(generations):
    rehearsal, old, new = generations
    before = {p.name: sha(p) for p in (rehearsal.root / "old").rglob("*") if p.is_file()}
    with rehearsal.locked():
        with pytest.raises(OwnershipError):
            GenerationRehearsal(rehearsal.root).owner.acquire()
        assert rehearsal.resolve()[0]["generation"] == old
        decision = rehearsal.switch(new)
        assert rehearsal.resolve()[0] == decision
    reopened = GenerationRehearsal(rehearsal.root)
    with reopened.locked():
        assert reopened.resolve()[0]["generation"] == new
    assert {p.name: sha(p) for p in (rehearsal.root / "old").rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("outcome", ["before-replace", "after-replace", "lost-completion"])
def test_unknown_rehearsal_outcome_is_diagnosed_without_writer_admission(
    generations, monkeypatch, outcome, record_property
):
    rehearsal, old, new = generations
    original_replace = os.replace
    original_unlink = Path.unlink

    def replace(source, destination):
        if outcome == "before-replace":
            raise PermissionError("synthetic replacement rejection")
        original_replace(source, destination)
        if outcome == "after-replace":
            raise OSError("replacement happened; acknowledgement unknown")

    def unlink(path, *args, **kwargs):
        if path.name == "pending.json" and outcome == "lost-completion":
            raise PermissionError("synthetic pending cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr("pathlib.Path.unlink", unlink)
    old_hash = sha(rehearsal.root / "old/database.sqlite3")
    with rehearsal.locked():
        with pytest.raises(OSError):
            rehearsal.switch(new)
        with pytest.raises(BusinessError, match="generation_recovery_required"):
            rehearsal.resolve()
        evidence = rehearsal.inspect_pending()
        assert evidence["observed"]["generation"] == (old if outcome == "before-replace" else new)
        assert not evidence["writer_admission"]
        assert sha(rehearsal.root / "old/database.sqlite3") == old_hash
    with GenerationRehearsal(rehearsal.root).locked() as reopened:
        assert reopened.inspect_pending() == evidence
        with pytest.raises(BusinessError, match="generation_recovery_required"):
            reopened.resolve()
    record_property(
        "generation_evidence",
        json.dumps(
            {
                "fault": outcome,
                "diagnostic": evidence,
                "old_database_sha256": old_hash,
                "r4_ambiguous_guarantee": "UNPROVEN",
            }
        ),
    )


def test_explicit_recovery_confirmation_is_repeatable_after_cleanup_failure(
    generations, monkeypatch
):
    rehearsal, old, new = generations
    original_replace = os.replace
    original_unlink = Path.unlink

    def lost_ack(source, destination):
        original_replace(source, destination)
        raise OSError("lost acknowledgement")

    with rehearsal.locked():
        with monkeypatch.context() as fault:
            fault.setattr(os, "replace", lost_ack)
            with pytest.raises(OSError):
                rehearsal.switch(new)
        with pytest.raises(BusinessError, match="generation_confirmation_mismatch"):
            rehearsal.confirm_observed(old)

        def fail_cleanup(path, *args, **kwargs):
            if path.name == "pending.json":
                raise OSError("recovery cleanup failed")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(Path, "unlink", fail_cleanup)
            with pytest.raises(OSError):
                rehearsal.confirm_observed(new)
        with pytest.raises(BusinessError, match="generation_recovery_required"):
            rehearsal.resolve()
    with GenerationRehearsal(rehearsal.root).locked() as reopened:
        assert reopened.confirm_observed(new)[0]["generation"] == new
        assert reopened.resolve()[0]["generation"] == new


GENERATION_CHILD = r"""
import json, os, sys
from pathlib import Path
import app.phase4.generation as module
root, generation, boundary = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
original_write, original_replace, original_unlink = module._write, os.replace, Path.unlink
def stop():
    print(json.dumps({'pid':os.getpid(),'boundary':boundary}),flush=True)
    sys.stdin.readline()
def write(path,payload):
    result = original_write(path,payload)
    if ((boundary=='intent' and path.name=='pending.json') or
        (boundary=='candidate' and path.suffix=='.tmp') or
        (boundary=='complete' and path.name.endswith('.complete.json'))): stop()
    return result
def replace(source,destination):
    result = original_replace(source,destination)
    if boundary=='replace': stop()
    return result
def unlink(path,*a,**kw):
    result = original_unlink(path,*a,**kw)
    if boundary=='released' and path.name=='pending.json': stop()
    return result
module._write, os.replace, Path.unlink = write, replace, unlink
with module.GenerationRehearsal(root).locked() as rehearsal:
    rehearsal.switch(generation)
raise AssertionError('unreached boundary')
"""


@pytest.mark.parametrize("boundary", ["intent", "candidate", "replace", "complete", "released"])
def test_generation_process_interruption_selects_one_observed_binding(
    generations, tmp_path, boundary, record_property
):
    rehearsal, old, new = generations
    old_path = rehearsal.root / "old/database.sqlite3"
    before = sha(old_path)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    with (tmp_path / "generation-child.stderr.log").open("wb") as stderr:
        child = subprocess.Popen(
            [
                sys._base_executable,
                "-u",
                "-c",
                GENERATION_CHILD,
                str(rehearsal.root),
                new,
                boundary,
            ],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(child.stdout.readline)
                try:
                    ack = json.loads(pending.result(timeout=15))
                except BaseException:
                    child.kill()
                    child.wait(timeout=5)
                    raise
            assert ack == {"pid": child.pid, "boundary": boundary}
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()
    with GenerationRehearsal(rehearsal.root).locked() as reopened:
        if boundary == "released":
            observed = reopened.resolve()[0]
            assert observed["generation"] == new
            diagnostic = {"pending": False}
        else:
            with pytest.raises(BusinessError, match="generation_recovery_required"):
                reopened.resolve()
            diagnostic = reopened.inspect_pending()
            observed = diagnostic["observed"]
            expected = old if boundary in {"intent", "candidate"} else new
            assert observed["generation"] == expected
            assert reopened.confirm_observed(expected)[0] == observed
        assert reopened.resolve()[0] == observed
    assert sha(old_path) == before
    record_property(
        "generation_process_evidence",
        json.dumps(
            {
                "ack": ack,
                "diagnostic": diagnostic,
                "observed": observed,
                "old_physical_sha256": before,
                "power_loss": "UNPROVEN",
                "r4_ambiguous_outcome": "UNPROVEN",
            }
        ),
    )
