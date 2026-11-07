"""Valid SQL fields in inconsistent combinations must reject before recovery writes."""

import sqlite3

import pytest

from app.phase3.store import BusinessError
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
from tests.phase4.test_acceptance_state import context, create_budget

__all__ = ["context"]


@pytest.mark.parametrize(
    "fault,invariant",
    [
        ("version", "history_state_version"),
        ("state", "history_state_version"),
        ("event-chain", "sequence_or_chain"),
        ("event-transition", "state_transition"),
        ("missing-create", "unique_create_and_occupant"),
        ("fingerprint", "create_fingerprint"),
        ("missing-proposal", "sealed_response"),
        ("request-shape", "record_shape"),
        ("request-fingerprint", "record_shape"),
        ("event-action", "state_transition"),
        ("timestamp", "record_shape"),
        ("proposal-status", "business_record"),
        ("source", "proposal_source"),
        ("error-shape", "record_shape"),
        ("event-json", "event_payload"),
        ("sealed-status", "sealed_response"),
        ("sealed-task", "sealed_response"),
        ("sealed-version", "sealed_response"),
        ("duplicate-create", "unique_create_and_occupant"),
    ],
)
def test_corrupted_combinations_reject_with_locator_and_unchanged_rows(
    context, fault, invariant, record_property
):
    service, original = context
    task, proposal = create_budget(service)
    original.owner.close()
    try:
        connection = sqlite3.connect(original.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # Inject disk corruption while restoring the exact frozen schema catalog.
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        for row in triggers:
            connection.execute('DROP TRIGGER "' + row["name"] + '"')
        if fault == "version":
            connection.execute("UPDATE tasks SET version=version+1")
        elif fault == "state":
            connection.execute("UPDATE tasks SET status='approved'")
        elif fault == "source":
            connection.execute("UPDATE tasks SET source_sha256=?", ("f" * 64,))
        elif fault == "error-shape":
            connection.execute("UPDATE tasks SET error='{}'")
        elif fault == "request-shape":
            connection.execute("UPDATE tasks SET validated_request='{}'")
        elif fault == "request-fingerprint":
            connection.execute(
                "UPDATE tasks SET validated_request=json_set(validated_request,'$.min_clicks',99)"
            )
        elif fault == "timestamp":
            connection.execute("UPDATE tasks SET updated_at='2026-10-01'")
        elif fault == "event-action":
            connection.execute("UPDATE task_events SET action='approved' WHERE seq=2")
        elif fault == "event-json":
            connection.execute("UPDATE events SET payload='[]'")
        elif fault == "event-chain":
            connection.execute("UPDATE task_events SET from_status='approved' WHERE seq=2")
        elif fault == "event-transition":
            connection.execute("UPDATE task_events SET to_status='rejected' WHERE seq=2")
        elif fault == "missing-create":
            connection.execute("DELETE FROM operations")
        elif fault == "fingerprint":
            connection.execute("UPDATE operations SET fingerprint=?", ("f" * 64,))
        elif fault == "missing-proposal":
            connection.execute("DELETE FROM task_documents")
        elif fault == "proposal-status":
            from app.phase3.store import Store

            proposal["status"] = "approved"
            Store.save(connection, proposal["id"], "proposal", proposal)
        elif fault == "sealed-status":
            connection.execute("UPDATE operations SET sealed_http_status=409")
        elif fault == "sealed-task":
            connection.execute(
                "UPDATE operations SET response=CAST("
                "json_set(CAST(response AS TEXT),'$.data.id',?) AS BLOB)",
                ("f" * 32,),
            )
        elif fault == "sealed-version":
            connection.execute(
                "UPDATE operations SET response=CAST("
                "json_set(CAST(response AS TEXT),'$.data.version',99) AS BLOB)"
            )
        else:
            connection.execute(
                "INSERT INTO operations SELECT ?,method,path,'duplicate',fingerprint,"
                "task_id,phase,created_at,updated_at,sealed_http_status,response FROM operations",
                ("f" * 32,),
            )
        for row in triggers:
            connection.execute(row["sql"])
        tables = ("tasks", "operations", "task_events", "documents", "task_documents")
        before = {t: [tuple(r) for r in connection.execute(f"SELECT * FROM {t}")] for t in tables}
        connection.close()
        physical_before = original.path.read_bytes()
        with CandidateOwner(original.path) as owner:
            candidate = CandidateStore(owner)
            with pytest.raises(BusinessError, match="stored_evidence_integrity_error") as error:
                candidate.recover_before_serving()
            diagnostic = candidate.recovery_diagnostic
            assert diagnostic == error.value.diagnostic
            assert diagnostic["invariant"] == invariant
            if fault == "event-json":
                assert diagnostic["table"] == "events" and diagnostic["identity"] == 1
            elif diagnostic["table"] == "operations":
                assert diagnostic["identity"] in {r[0] for r in before["operations"]}
            else:
                assert diagnostic["identity"] in {task["id"], proposal["id"]}
            assert not owner.ready
            with candidate.snapshot() as connection:
                after = {
                    t: [tuple(r) for r in connection.execute(f"SELECT * FROM {t}")] for t in tables
                }
            assert after == before
            record_property("recovery_rejection", diagnostic)
        assert original.path.read_bytes() == physical_before
    finally:
        original.owner.acquire()
