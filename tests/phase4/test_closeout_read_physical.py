"""GET must not create WAL/SHM or alter any application file, including read marks."""

import hashlib

from fastapi.testclient import TestClient

from app.main import create_app
from app.phase4.contracts import CREATE_ADAPTER
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
from tests.phase4.test_acceptance_state import context
from tests.phase4.test_owner_application_integration import DATES

__all__ = ["context"]


def physical(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        if p.is_file()
        else None
        for p in root.rglob("*")
        if not p.name.endswith(".lock")
    }


def exercise_gets(store, data, reports, root):
    app = create_app(
        data_path=data, business_db_path=store.path, reports_dir=reports, storage_owner=store.owner
    )
    before = physical(root)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        for path in ("/api/v1/tasks", "/api/v1/reports", "/api/v1/tasks/" + "f" * 32):
            response = client.get(path)
            assert response.status_code == (404 if path.endswith("f" * 32) else 200)
            assert physical(root) == before
    assert physical(root) == before


def test_first_get_and_post_write_gets_leave_physical_files_unchanged(context, tmp_path):
    service, store = context
    exercise_gets(store, service.business.data_path, service.reports.reports_dir, tmp_path)
    # TestClient shutdown closes its facade, not the externally supplied owner.
    result = service.execute(
        ("POST", "/api/v1/tasks", "report"),
        CREATE_ADAPTER.validate_python({"kind": "report", "request": DATES}),
    )
    assert result[0] == 201
    exercise_gets(store, service.business.data_path, service.reports.reports_dir, tmp_path)
    assert store.checkpoint() == (0, 0, 0)
    exercise_gets(store, service.business.data_path, service.reports.reports_dir, tmp_path)
    store.owner.close()
    try:
        with CandidateOwner(store.path) as owner:
            restarted = CandidateStore(owner)
            assert restarted.recover_before_serving() == 0
            exercise_gets(
                restarted, service.business.data_path, service.reports.reports_dir, tmp_path
            )
            assert owner._read_anchor is not None
    finally:
        store.owner.acquire()


def test_persistent_read_anchor_closes_before_releasing_owner(tmp_path):
    with CandidateOwner(tmp_path / "database.sqlite3") as owner:
        store = CandidateStore(owner)
        store.initialize_empty_candidate()
        anchor = owner._read_anchor
        assert anchor is not None and owner.active_connections == 0
    assert owner._read_anchor is None
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        anchor.execute("SELECT 1")
