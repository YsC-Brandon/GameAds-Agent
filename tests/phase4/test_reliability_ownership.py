"""Real maintenance connection drain, stale handles and competing owner lifetimes."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.phase4.maintenance import connect, initialize
from app.phase4.ownership import CandidateOwner, OwnershipError


def test_maintenance_handle_prevents_early_unlock_and_is_closed_after_release(tmp_path):
    database = tmp_path / "candidate.sqlite3"
    initialize(database)
    with CandidateOwner(database) as owner:
        connection = connect(database, readonly=False, owner=owner)
        assert owner.active_connections == 1
        with pytest.raises(OwnershipError, match="active transaction"):
            owner.close()
        with pytest.raises(OwnershipError):
            CandidateOwner(database).acquire()
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
        connection.close()
        connection.close()
        assert owner.active_connections == 0
    with CandidateOwner(database):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("PRAGMA user_version=0")


def test_maintenance_thread_handle_drains_before_owner_released(tmp_path):
    database = tmp_path / "candidate.sqlite3"
    initialize(database)
    owner = CandidateOwner(database).acquire()
    opened, release, closing = threading.Event(), threading.Event(), threading.Event()

    def maintenance():
        connection = connect(database, readonly=False, owner=owner)
        try:
            opened.set()
            assert release.wait(5)
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        finally:
            connection.close()

    def shutdown():
        closing.set()
        owner.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            job = pool.submit(maintenance)
            assert opened.wait(5)
            close = pool.submit(shutdown)
            assert closing.wait(5)
            assert not close.done()
            with pytest.raises(OwnershipError):
                CandidateOwner(database).acquire()
            release.set()
            job.result(timeout=5)
            close.result(timeout=5)
        with CandidateOwner(database):
            assert owner.active_connections == 0
    finally:
        release.set()
        if owner._handle is not None:
            owner.close()


def test_wrong_thread_close_does_not_drop_connection_tracking(tmp_path):
    database = tmp_path / "candidate.sqlite3"
    initialize(database)
    with CandidateOwner(database) as owner:
        connection = connect(database, readonly=False, owner=owner)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(connection.close)
                with pytest.raises(sqlite3.ProgrammingError, match="same thread"):
                    future.result(timeout=5)
            assert owner.active_connections == 1
            with pytest.raises(OwnershipError, match="active transaction"):
                owner.close()
            connection.execute("SELECT 1")
        finally:
            connection.close()
        assert owner.active_connections == 0
