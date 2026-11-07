"""G01 isolated candidate unit of work, reservation, sealing and restart recovery.

Not a task service: business validation/projections and active-instance admission
remain gated. No automatic initialization, migration, model call or file publish.
"""

import sqlite3
import threading
from contextlib import contextmanager
from uuid import uuid4

from app.phase3.store import BusinessError, encode, now
from app.phase4.ownership import CandidateOwner
from app.phase4.schema import DDL, SCHEMA_DIGEST, catalog, expected_catalog
from app.task_wire import operation_identity


class _ConnectionLease:
    """Keep even an internal escaped connection inside the owner lifetime."""

    def __init__(self, connection, owner):
        self.connection, self.owner = connection, owner
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        # Wrong-thread SQLite close fails before dropping the registered lease.
        self.connection.close()
        if not self.closed:
            self.closed = True
            self.owner.active_connections -= 1
            self.owner.mutex.release()


class CandidateStore:
    def __init__(self, owner: CandidateOwner):
        with owner.mutex:
            owner.require()
            if owner.store_attached:
                raise RuntimeError("One store lifecycle per candidate owner")
            owner.store_attached = True
            self.owner, self.path, self.epoch = owner, owner.database, owner.epoch
            owner.storage = self
        self._admitted = False
        self._thread = threading.local()

    def _require(self):
        self.owner.require()
        if self.epoch is not self.owner.epoch:
            raise RuntimeError("Stale store lifecycle")

    def _connect(self, *, readonly=False):
        self.owner.mutex.acquire()
        try:
            return self._open_connection(readonly=readonly)
        except BaseException:
            self.owner.mutex.release()
            raise

    def _open_connection(self, *, readonly=False):
        self._require()
        # mode=rw never creates a missing DB. Each context owns and closes its
        # connection on the calling thread; no cross-thread connection reuse.
        connection = sqlite3.connect(
            self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
            uri=True,
            timeout=1,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=1000")
            if readonly:
                connection.execute("PRAGMA query_only=ON")
            else:
                connection.execute("PRAGMA synchronous=FULL")
        except BaseException:
            connection.close()
            raise
        self.owner.active_connections += 1
        return _ConnectionLease(connection, self.owner)

    @staticmethod
    def _check(connection):
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise BusinessError("schema_upgrade_required", 503)
        if catalog(connection) != expected_catalog():
            raise BusinessError("schema_upgrade_required", 503)
        row = connection.execute(
            "SELECT migration_digest FROM schema_migrations WHERE version=1"
        ).fetchone()
        if row is None or row[0] != SCHEMA_DIGEST:
            raise BusinessError("schema_upgrade_required", 503)
        if connection.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raise BusinessError("candidate_wal_required", 503)

    def initialize_empty_candidate(self):
        """Explicit empty fixture creation only; refuses ANY existing DB file.

        Failure preserves the partially created candidate for diagnosis. No
        attempt is made to switch this candidate into an active location.
        """
        with self.owner.mutex:
            self._require()
            if getattr(self._thread, "active", False):
                raise RuntimeError("Nested candidate transactions are unsupported")
            with self.path.open("xb"):
                pass
            connection = self._connect()
            try:
                if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise RuntimeError("WAL unavailable")
                # executescript's initial implicit commit occurs BEFORE BEGIN.
                # All DDL, ledger and version writes below share one transaction.
                connection.executescript("BEGIN IMMEDIATE;\n" + DDL)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES(1,?,1,?)", (now(), SCHEMA_DIGEST)
                )
                connection.execute("PRAGMA user_version=1")
                connection.commit()
                self._check(connection)
                self.owner.warm_reads()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
            self._admitted = True
            self.owner.ready = True

    @contextmanager
    def _transaction(self, *, readonly=False, recovery=False):
        with self.owner.mutex:
            self._require()
            if recovery and self._admitted:
                raise RuntimeError("Recovery cannot run alongside admitted handlers")
            if getattr(self._thread, "active", False):
                raise RuntimeError("Nested candidate transactions are unsupported")
            if not readonly and not recovery and not self._admitted:
                raise BusinessError("recovery_required", 503)
            connection = self._connect(readonly=readonly)
            self._thread.active = True
            try:
                connection.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
                self._check(connection)
                yield connection
                connection.commit()
                if not readonly:
                    self.owner.warm_reads()
                if recovery:
                    # Admission and the recovery commit share the ownership lock.
                    # A waiting recovery/handler cannot observe a gap between them.
                    self._admitted = True
                    self.owner.ready = True
            except BaseException:
                connection.rollback()
                if not readonly and self.owner.ready:
                    self.owner.warm_reads()
                raise
            finally:
                try:
                    connection.close()
                finally:
                    self._thread.active = False

    @contextmanager
    def snapshot(self):
        try:
            with self._transaction(readonly=True) as connection:
                yield connection
        except sqlite3.OperationalError as exc:
            raise BusinessError("database_busy", 503) from exc

    @staticmethod
    def event(connection, task_id, action, before, after, *, code=None):
        connection.execute(
            "INSERT INTO task_events SELECT ?,COALESCE(MAX(seq),0)+1,?,?,?,?,? "
            "FROM task_events WHERE task_id=?",
            (task_id, action, before, after, now(), code, task_id),
        )

    def reserve(self, method, path, key, fingerprint, *, task_id=None):
        method, path, key = operation_identity(method, path, key)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM operations WHERE method=? AND path=? AND key=?",
                (method, path, key),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise BusinessError("idempotency_key_reused")
                return dict(existing), False
            if task_id is not None:
                if (
                    connection.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone()
                    is None
                ):
                    raise BusinessError("not_found", 404)
                if connection.execute(
                    "SELECT 1 FROM operations WHERE task_id=? AND phase='in_progress'", (task_id,)
                ).fetchone():
                    raise BusinessError("task_busy")
            identity, timestamp = uuid4().hex, now()
            connection.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?,'in_progress',?,?,NULL,NULL)",
                (identity, method, path, key, fingerprint, task_id, timestamp, timestamp),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM operations WHERE operation_id=?", (identity,)
                ).fetchone()
            ), True

    def lookup(self, method, path, key):
        method, path, key = operation_identity(method, path, key)
        with self.snapshot() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE method=? AND path=? AND key=?", (method, path, key)
            ).fetchone()
            return None if row is None else dict(row)

    def create_submitted(self, operation_id, kind, *, validated_request=None, retry_of=None):
        with self._transaction() as connection:
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if (
                operation is None
                or operation["path"] != "/api/v1/tasks"
                or operation["phase"] != "in_progress"
                or operation["task_id"] is not None
            ):
                raise BusinessError("operation_in_progress")
            identity, timestamp = uuid4().hex, now()
            request = None if validated_request is None else encode(validated_request)
            connection.execute(
                "INSERT INTO tasks(id,schema_version,kind,status,version,created_at,updated_at,"
                "request_fingerprint,validated_request,retry_of) "
                "VALUES(?,1,?,'submitted',1,?,?,?,?,?)",
                (identity, kind, timestamp, timestamp, operation["fingerprint"], request, retry_of),
            )
            self.event(connection, identity, "submitted", None, "submitted")
            connection.execute(
                "UPDATE operations SET task_id=?,updated_at=? WHERE operation_id=?",
                (identity, timestamp, operation_id),
            )
            return identity

    def mark_running(self, operation_id, expected_version):
        with self.unit_of_work(operation_id) as connection:
            task_id = connection.execute(
                "SELECT task_id FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()[0]
            changed = connection.execute(
                "UPDATE tasks SET status='running',version=version+1,updated_at=? "
                "WHERE id=? AND status='submitted' AND version=?",
                (now(), task_id, expected_version),
            ).rowcount
            if changed != 1:
                raise BusinessError("stale_task_version")
            self.event(connection, task_id, "running", "submitted", "running")

    @contextmanager
    def unit_of_work(self, operation_id):
        """One transaction for task/documents/relations/events/seal; no nested commits."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT phase FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or row[0] != "in_progress":
                raise BusinessError("operation_not_active")
            yield connection

    @staticmethod
    def seal(connection, operation_id, http_status, response: bytes, *, interrupted=False):
        if not isinstance(response, bytes):
            raise TypeError("Seal requires exact serialized response bytes")
        changed = connection.execute(
            "UPDATE operations SET phase=?,sealed_http_status=?,response=?,updated_at=? "
            "WHERE operation_id=? AND phase='in_progress'",
            (
                "interrupted" if interrupted else "completed",
                http_status,
                response,
                now(),
                operation_id,
            ),
        ).rowcount
        if changed != 1:
            raise BusinessError("operation_not_active")

    def recover_before_serving(self):
        """Run only on a newly opened owner/store before accepting any work.

        Single transaction: stale creates become interrupted, continuations keep
        their committed state, every stale reservation seals 409. No re-execution.
        """
        count = 0
        # Validate through a read-only transaction before BEGIN IMMEDIATE, and
        # repeat inside the recovery transaction. No speculative repair precedes
        # diagnostics; ownership excludes cooperating writers between snapshots.
        from app.phase4.integrity import validate_records

        try:
            with self.snapshot() as connection:
                validate_records(connection)
        except BusinessError as exc:
            self.recovery_diagnostic = getattr(exc, "diagnostic", None)
            raise
        with self._transaction(recovery=True) as connection:
            try:
                validate_records(connection)
            except BusinessError as exc:
                self.recovery_diagnostic = getattr(exc, "diagnostic", None)
                raise
            for operation in connection.execute(
                "SELECT * FROM operations WHERE phase='in_progress' ORDER BY operation_id"
            ).fetchall():
                task_id = operation["task_id"]
                if task_id is not None and operation["path"] == "/api/v1/tasks":
                    task = connection.execute(
                        "SELECT * FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()
                    if task["status"] not in ("submitted", "running"):
                        raise BusinessError("stored_evidence_integrity_error")
                    connection.execute(
                        "UPDATE tasks SET status='interrupted',version=version+1,updated_at=?,"
                        "error=? WHERE id=?",
                        (
                            now(),
                            encode(
                                {
                                    "code": "operation_interrupted",
                                    "message": "Operation interrupted; inspect current state.",
                                    "retryable": False,
                                }
                            ),
                            task_id,
                        ),
                    )
                    self.event(
                        connection,
                        task_id,
                        "interrupted",
                        task["status"],
                        "interrupted",
                        code="external_outcome_unknown" if task["status"] == "running" else None,
                    )
                body = encode(
                    {
                        "status": "error",
                        "code": "operation_interrupted",
                        "message": "Operation interrupted; inspect current state.",
                        "task_id": task_id,
                        "retryable": False,
                    }
                ).encode()
                self.seal(connection, operation["operation_id"], 409, body, interrupted=True)
                count += 1
        return count

    def checkpoint(self):
        """Explicit maintenance under the same ownership; never part of a GET."""
        with self.owner.mutex:
            self._require()
            if getattr(self._thread, "active", False):
                raise RuntimeError("Nested candidate transactions are unsupported")
            if not self._admitted:
                raise BusinessError("recovery_required", 503)
            connection = self._connect()
            try:
                self._check(connection)
                result = tuple(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
                if result[0] != 0:
                    raise BusinessError("database_busy", 503)
                self.owner.warm_reads()
                return result
            finally:
                connection.close()
