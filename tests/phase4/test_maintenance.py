"""Offline tools on synthetic legacy files; no active-path replacement."""

import json
import os
import subprocess
import sys

import pytest

from app.phase3.store import BusinessError, Store
from app.phase4.maintenance import (
    backup,
    check,
    initialize,
    migrate_candidate,
    restore_candidate,
    sha,
    verify_backup,
)
from app.phase4.ownership import CandidateOwner, OwnershipError
from app.phase4.store import CandidateStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ownership tools")


def test_real_backup_migration_preserves_legacy_rows_and_orphan_event(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    store = Store(database)
    with store.transaction() as connection:
        store.event(connection, "a" * 32, "synthetic_orphan_failure", {"code": "synthetic"})
    dataset = tmp_path / "dataset.csv"
    dataset.write_text("synthetic\n", encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    original = sha(database)
    destination = tmp_path / "backup"
    manifest = backup(database, destination, reports, dataset)
    assert manifest["complete"] is True
    assert verify_backup(destination) == manifest
    restored = tmp_path / "restored"
    assert restore_candidate(destination, restored)["activated"] is False
    assert sha(restored / "database.sqlite3") == manifest["files"]["database.sqlite3"]
    with pytest.raises(FileExistsError):
        restore_candidate(destination, restored)
    assert sha(database) == original
    candidate_path = tmp_path / "candidate.sqlite3"
    assert migrate_candidate(destination, candidate_path)["activated"] is False
    assert sha(database) == original
    assert check(database)["schema_version"] == 0
    assert check(candidate_path)["schema_version"] == 1
    with CandidateOwner(candidate_path) as owner:
        candidate = CandidateStore(owner)
        candidate.recover_before_serving()
        with candidate.snapshot() as connection:
            row = connection.execute("SELECT document_id,action,payload FROM events").fetchone()
            assert tuple(row) == ("a" * 32, "synthetic_orphan_failure", '{"code":"synthetic"}')
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    with pytest.raises(FileExistsError):
        migrate_candidate(destination, candidate_path)


def test_maintenance_rejects_owned_source_and_corrupt_backup(tmp_path):
    database = tmp_path / "candidate.sqlite3"
    initialize(database)
    with CandidateOwner(database):
        with pytest.raises(OwnershipError):
            check(database)
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "manifest.json").write_text(
        json.dumps({"format": 1, "complete": True, "files": {}}), encoding="utf-8"
    )
    with pytest.raises(BusinessError):
        migrate_candidate(backup_dir, tmp_path / "new.sqlite3")
    assert not (tmp_path / "new.sqlite3").exists()


def test_cli_init_check_refuses_overwrite(tmp_path):
    database = tmp_path / "candidate.sqlite3"
    command = [sys.executable, "-m", "app.phase4.maintenance"]
    for verb in ("init", "check"):
        result = subprocess.run(
            command + [verb, "--database", str(database)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == "ok"
    original = sha(database)
    result = subprocess.run(
        command + ["init", "--database", str(database)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert sha(database) == original
