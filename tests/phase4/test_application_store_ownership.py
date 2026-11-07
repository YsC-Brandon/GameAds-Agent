"""Integration characterization for real application writers vs G01 ownership."""

import os

import pytest

from app.phase3.store import BusinessError, Store, digest, encode
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows candidate ownership")


def test_phase3_store_cannot_commit_while_candidate_owner_lock_is_held(tmp_path):
    database = tmp_path / "synthetic-candidate.sqlite3"
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()

        legacy = Store(database)
        with pytest.raises(BusinessError, match="database_busy"):
            with legacy.transaction() as connection:
                legacy.save(
                    connection,
                    "a" * 32,
                    "report",
                    {"synthetic": True},
                    new=True,
                )

        with candidate.snapshot() as connection:
            row = connection.execute(
                "SELECT payload,sha256 FROM documents WHERE id=?", ("a" * 32,)
            ).fetchone()
            assert row is None
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_explicit_owner_compatibility_store_shares_candidate_lifecycle(tmp_path):
    database = tmp_path / "synthetic-candidate.sqlite3"
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        legacy = Store(database, owner=owner)
        with legacy.transaction() as connection:
            legacy.save(connection, "a" * 32, "report", {"synthetic": True}, new=True)
            assert owner.active_connections == 1
        assert owner.active_connections == 0
        with candidate.snapshot() as connection:
            row = connection.execute("SELECT payload,sha256 FROM documents").fetchone()
            assert tuple(row) == (encode({"synthetic": True}), digest({"synthetic": True}))
        assert candidate.checkpoint() == (0, 0, 0)
    with pytest.raises(BusinessError, match="database_busy"):
        with legacy.transaction():
            pytest.fail("released owner admitted a transaction")
