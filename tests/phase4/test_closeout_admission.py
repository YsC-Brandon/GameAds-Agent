"""Application entry points share managed-generation ownership on real SQLite."""

import json

import pytest

from app.phase3.contracts import ReportRequest
from app.phase3.reports import ReportService
from app.phase3.service import BusinessService
from app.phase3.store import BusinessError, Store
from app.phase4.maintenance import backup, check, sha
from app.phase4.ownership import CandidateOwner, OwnershipError
from app.phase4.store import CandidateStore
from tests.phase4.test_owner_application_integration import DATES, application_data, server
from tests.phase4.test_reliability_generation import generations
from tests.phase4.test_reliability_windows_files import open_without_delete_share

__all__ = ["generations", "application_data"]


def test_real_store_report_maintenance_and_switch_share_one_generation(generations, tmp_path):
    rehearsal, old, new = generations
    old_path = rehearsal.root / "old/database.sqlite3"
    new_path = rehearsal.root / "new/database.sqlite3"
    old_hash = sha(old_path)
    stale = Store(old_path)
    owner = CandidateOwner(old_path).acquire()
    store = CandidateStore(owner)
    store.recover_before_serving()
    facade = Store(old_path, owner=owner)
    with facade.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert store.checkpoint() == (0, 0, 0)
    with pytest.raises(OwnershipError):
        rehearsal.owner.acquire()
    with pytest.raises(OwnershipError):
        CandidateOwner(new_path).acquire()
    with pytest.raises(OwnershipError):
        check(old_path)
    owner.close()
    with rehearsal.locked():
        assert rehearsal.switch(new)["generation"] == new
    # Facades retained across switch cannot reopen the now inactive physical DB.
    for entry in (facade, stale):
        with pytest.raises(BusinessError, match="database_busy"):
            with entry.transaction():
                pytest.fail("stale database admitted")
    business = BusinessService(stale, data_path=tmp_path / "absent.csv")
    reports = ReportService(business, tmp_path / "never-created")
    with pytest.raises(BusinessError, match="database_busy"):
        reports.generate_campaign_report(
            ReportRequest(start_date="2026-10-01", end_date="2026-10-01")
        )
    assert not (tmp_path / "never-created").exists()
    with pytest.raises(OwnershipError):
        backup(old_path, tmp_path / "denied-backup", tmp_path, tmp_path / "absent.csv")
    assert not (tmp_path / "denied-backup").exists()
    assert sha(old_path) == old_hash
    with CandidateOwner(new_path) as current:
        candidate = CandidateStore(current)
        assert candidate.recover_before_serving() == 0
        with candidate.snapshot() as connection:
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_escaped_candidate_connection_prevents_owner_release(tmp_path):
    with CandidateOwner(tmp_path / "database.sqlite3") as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        connection = candidate._connect()
        assert owner.active_connections == 1
        with pytest.raises(OwnershipError, match="active transaction"):
            owner.close()
        with pytest.raises(OwnershipError):
            CandidateOwner(candidate.path).acquire()
        connection.close()
        connection.close()
        assert owner.active_connections == 0


def test_managed_http_service_restart_fences_old_paths(generations, application_data, tmp_path):
    rehearsal, old, new = generations
    old_path = rehearsal.root / "old/database.sqlite3"
    new_path = rehearsal.root / "new/database.sqlite3"
    # Rebuild the fixture backup sets with the synthetic business CSV before
    # preparing managed generations (the generic fixture contains no CSV rows).
    # A separate root avoids mutating registered snapshot manifests.
    from app.phase4.generation import GenerationRehearsal
    from app.phase4.maintenance import initialize

    source = tmp_path / "http-source.sqlite3"
    initialize(source)
    root = tmp_path / "http-instance"
    root.mkdir()
    empty_reports = tmp_path / "empty-reports"
    empty_reports.mkdir()
    for directory in ("old", "new"):
        backup(source, root / directory, empty_reports, application_data)
    rehearsal = GenerationRehearsal(root)
    with rehearsal.locked():
        old = rehearsal.prepare(root / "old")
        new = rehearsal.prepare(root / "new")
        rehearsal.bootstrap(old)
    old_path, new_path = root / "old/database.sqlite3", root / "new/database.sqlite3"
    # The shared server helper fixes reports to tmp_path/reports; use the
    # generation directory as that root so DB/CSV/reports remain one binding.
    # A real server process owns the instance throughout HTTP business execution.
    with server(root / "old", old_path, root / "old/dataset.csv", bound=True) as client:
        result = client.post(
            "/api/v1/tasks",
            json={"kind": "report", "request": DATES},
            headers={"Idempotency-Key": "old-http"},
        )
        assert result.status_code == 201, result.text
        with pytest.raises(OwnershipError):
            rehearsal.owner.acquire()
        with pytest.raises(OwnershipError):
            CandidateOwner(new_path).acquire()
    old_hash = sha(old_path)
    with rehearsal.locked():
        assert rehearsal.resolve()[0]["generation"] == old
        rehearsal.switch(new)
    with pytest.raises(OwnershipError):
        CandidateOwner(old_path).acquire()
    with server(root / "new", new_path, root / "new/dataset.csv", bound=True) as client:
        result = client.post(
            "/api/v1/tasks",
            json={"kind": "report", "request": DATES},
            headers={"Idempotency-Key": "new-http"},
        )
        assert result.status_code == 201, result.text
        task = result.json()["data"]
        assert client.get("/api/v1/tasks/" + task["id"]).status_code == 200
    with server(root / "new", new_path, root / "new/dataset.csv", bound=True) as client:
        assert client.get("/api/v1/tasks/" + task["id"]).json()["data"]["status"] == "completed"
    assert sha(old_path) == old_hash


def test_lost_replace_ack_has_specific_frozen_contract_counterexample(
    generations, monkeypatch, record_property
):
    rehearsal, old, new = generations
    import os

    replace = os.replace

    def lost_ack(source, target):
        replace(source, target)  # Actual filesystem replacement, then lost confirmation.
        raise OSError("injected acknowledgement loss after replacement")

    old_hash = sha(rehearsal.root / "old/database.sqlite3")
    with rehearsal.locked():
        monkeypatch.setattr(os, "replace", lost_ack)
        with pytest.raises(OSError):
            rehearsal.switch(new)
        evidence = rehearsal.inspect_pending()
        assert evidence["observed"]["generation"] == new
        assert evidence["old"]["generation"] == old
        assert not evidence["writer_admission"]
    assert sha(rehearsal.root / "old/database.sqlite3") == old_hash
    for directory in ("old", "new"):
        with pytest.raises(OwnershipError, match="recovery required"):
            CandidateOwner(rehearsal.root / directory / "database.sqlite3").acquire()
    record_property("r4_counterexample", json.dumps(evidence))


def test_real_windows_locator_occupation_retains_old_binding(generations, record_property):
    rehearsal, old, new = generations
    active = rehearsal.root / "active.json"
    before = active.read_bytes()
    kernel, handle = open_without_delete_share(active)
    try:
        with rehearsal.locked():
            with pytest.raises(PermissionError) as error:
                rehearsal.switch(new)
            assert error.value.winerror in {5, 32}
            assert active.read_bytes() == before
            evidence = rehearsal.inspect_pending()
            assert evidence["observed"]["generation"] == old
            record_property("occupied_locator", json.dumps(evidence))
    finally:
        assert kernel.CloseHandle(handle)
    with rehearsal.locked():
        assert rehearsal.confirm_observed(old)[0]["generation"] == old


def test_generation_lock_is_released_when_database_lock_object_is_unsafe(generations):
    rehearsal, old, _ = generations
    database = rehearsal.root / "old/database.sqlite3"
    lock = database.with_name(database.name + ".owner.lock")
    alias = lock.with_name("unsafe-lock-alias")
    alias.hardlink_to(lock)
    try:
        with pytest.raises(OwnershipError, match="Unsafe ownership object"):
            CandidateOwner(database).acquire()
        with rehearsal.locked():
            assert rehearsal.resolve()[0]["generation"] == old
    finally:
        alias.unlink()


def test_managed_report_cannot_publish_outside_the_generation(generations, tmp_path):
    rehearsal, _, _ = generations
    database = rehearsal.root / "old/database.sqlite3"
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.recover_before_serving()
        business = BusinessService(
            Store(database, owner=owner), data_path=database.parent / "dataset.csv"
        )
        reports = ReportService(business, tmp_path / "wrong-generation-reports")
        with pytest.raises(BusinessError, match="stored_evidence_integrity_error"):
            reports.generate_campaign_report(ReportRequest(**DATES))
        assert not reports.reports_dir.exists()
        with candidate.snapshot() as connection:
            for table in ("documents", "events", "operations"):
                assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
