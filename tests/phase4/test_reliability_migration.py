"""Legacy-state migration and corrupt-source rejection with physical hash checks."""

import json
import sqlite3
from contextlib import closing

import pytest

from app.phase3.contracts import ApprovalRequest, BudgetRequest, RejectionRequest, SimulationRequest
from app.phase3.service import BusinessService
from app.phase3.store import BusinessError, Store
from app.phase4.maintenance import backup, check, migrate_candidate, restore_candidate, sha
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
from tests.phase4.test_owner_application_integration import budget


@pytest.mark.parametrize("state", ["pending_approval", "approved", "rejected", "simulated"])
def test_migrate_all_legacy_proposal_states_preserves_original_physical_file(
    tmp_path, csv_factory, record, state, record_property
):
    source = tmp_path / "legacy.sqlite3"
    data = csv_factory([record])
    reports = tmp_path / "reports"
    reports.mkdir()
    business = BusinessService(Store(source), data_path=data)
    proposal = business.propose_budget_reallocation(BudgetRequest.model_validate(budget()))
    review = {
        "expected_version": 1,
        "proposal_sha256": proposal["proposal_sha256"],
        "confirmed": True,
        "approver": "synthetic",
    }
    if state == "rejected":
        proposal = business.reject_budget_proposal(
            proposal["id"], RejectionRequest(**review, reason="synthetic")
        )
    elif state in {"approved", "simulated"}:
        proposal = business.approve_budget_proposal(proposal["id"], ApprovalRequest(**review))
        if state == "simulated":
            proposal = business.simulate_budget_changes(
                proposal["id"],
                SimulationRequest(expected_version=2, proposal_sha256=proposal["proposal_sha256"]),
            )
    with business.store.transaction() as connection:
        business.store.event(connection, "f" * 32, "orphan_failure", {"code": "synthetic"})
    before = sha(source)
    with closing(sqlite3.connect(source)) as connection:
        rows = {
            table: connection.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid").fetchall()
            for table in ("documents", "events")
        }
    directory = tmp_path / "backup"
    backup(source, directory, reports, data)
    candidate = tmp_path / "candidate.sqlite3"
    assert migrate_candidate(directory, candidate)["activated"] is False
    assert sha(source) == before
    with CandidateOwner(candidate) as owner:
        store = CandidateStore(owner)
        assert store.recover_before_serving() == 0
        with store.snapshot() as connection:
            for table, expected in rows.items():
                assert [
                    tuple(row)
                    for row in connection.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")
                ] == expected
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        restored_business = BusinessService(Store(candidate, owner=owner), data_path=data)
        assert restored_business.get_budget_proposal(proposal["id"]) == proposal
    restored = tmp_path / "restored"
    assert restore_candidate(directory, restored)["activated"] is False
    assert sha(source) == before
    assert check(source)["schema_version"] == 0
    record_property(
        "migration_evidence",
        json.dumps(
            {
                "state": state,
                "source_sha256": before,
                "rows": rows,
                "candidate_only": True,
                "power_loss": "UNPROVEN",
            }
        ),
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "json",
        "digest",
        "unknown-kind",
        "future-version",
        "extra-table",
        "extra-trigger",
        "invalid-event",
    ],
)
def test_corrupt_legacy_source_is_not_repaired_or_migrated(tmp_path, corruption, record_property):
    source = tmp_path / "legacy.sqlite3"
    store = Store(source)
    with store.transaction() as connection:
        store.event(connection, "f" * 32, "orphan_failure", {})
    with closing(sqlite3.connect(source, isolation_level=None)) as connection:
        if corruption in {"json", "digest", "unknown-kind"}:
            connection.execute(
                "INSERT INTO documents VALUES(?,?,?,?)",
                (
                    "a" * 32,
                    "unknown" if corruption == "unknown-kind" else "proposal",
                    "{" if corruption == "json" else "{}",
                    "b" * 64,
                ),
            )
        elif corruption == "future-version":
            connection.execute("PRAGMA user_version=2")
        elif corruption == "extra-table":
            connection.execute("CREATE TABLE unexpected(id)")
        elif corruption == "extra-trigger":
            connection.execute(
                "CREATE TRIGGER unexpected AFTER INSERT ON events BEGIN SELECT 1; END"
            )
        else:
            connection.execute("UPDATE events SET payload='{' ")
    before = sha(source)
    data = tmp_path / "synthetic.csv"
    data.write_text("synthetic\n")
    reports = tmp_path / "reports"
    reports.mkdir()
    destination = tmp_path / "backup"
    with pytest.raises((BusinessError, ValueError, KeyError)):
        backup(source, destination, reports, data)
    assert sha(source) == before
    assert not destination.exists()
    record_property(
        "migration_rejection",
        json.dumps(
            {"corruption": corruption, "source_before_after": before, "backup_created": False}
        ),
    )
