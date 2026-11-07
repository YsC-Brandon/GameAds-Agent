"""Actual Windows sharing violations, not mocked file replacement failures."""

import ctypes
import json
from ctypes import wintypes

from app.phase4.maintenance import sha
from tests.phase4.test_acceptance_failures import simulated
from tests.phase4.test_acceptance_state import context, request

__all__ = ["context"]


def open_without_delete_share(path):
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
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel, handle


def test_actual_windows_occupied_report_rename_preserves_task(
    context, monkeypatch, record_property
):
    service, store = context
    identity = simulated(service)
    before = service.get(identity)
    from pathlib import Path

    original = Path.rename
    observations = []

    def occupied(path, destination):
        if path.suffix != ".tmp":
            return original(path, destination)
        kernel, handle = open_without_delete_share(path)
        try:
            try:
                return original(path, destination)
            except OSError as exc:
                observations.append(
                    {
                        "winerror": exc.winerror,
                        "temporary_sha256": sha(path),
                        "destination_exists": destination.exists(),
                    }
                )
                raise
        finally:
            assert kernel.CloseHandle(handle)

    monkeypatch.setattr(Path, "rename", occupied)
    result = service.execute(
        ("POST", f"/api/v1/tasks/{identity}/report", "occupied"),
        request("report", 5),
        task_id=identity,
        action="report",
    )
    assert result[0] == 500 and json.loads(result[1])["code"] == "report_write_failed"
    assert service.get(identity) == before
    assert observations[0]["winerror"] == 32
    assert not observations[0]["destination_exists"]
    assert not list(service.reports.reports_dir.glob("*.md"))
    assert not list(service.reports.reports_dir.glob("*.tmp"))
    record_property("windows_sharing_evidence", json.dumps(observations))


def test_actual_windows_locator_replace_failure_retains_old_physical_binding(
    tmp_path, record_property
):
    old_db, new_db = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    from app.phase4.maintenance import initialize

    initialize(old_db)
    initialize(new_db)
    old, new = tmp_path / "active.json", tmp_path / "candidate.json"
    old.write_text(json.dumps({"generation": "old", "database": old_db.name}))
    new.write_text(json.dumps({"generation": "new", "database": new_db.name}))
    before = {p.name: sha(p) for p in (old_db, new_db, old, new)}
    kernel, handle = open_without_delete_share(old)
    kernel.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    kernel.ReplaceFileW.restype = wintypes.BOOL
    try:
        result = kernel.ReplaceFileW(str(old), str(new), None, 0, None, None)
        error = ctypes.get_last_error()
        assert not result and error == 32
        assert {p.name: sha(p) for p in (old_db, new_db, old, new)} == before
        assert json.loads(old.read_text())["generation"] == "old"
    finally:
        assert kernel.CloseHandle(handle)
    record_property(
        "locator_primitive_evidence",
        json.dumps(
            {
                "winerror": error,
                "before_after": before,
                "scope": "sharing violation only",
                "ambiguous_outcome": "UNPROVEN",
                "power_loss": "UNPROVEN",
            }
        ),
    )
