"""SQLite transactions and integrity-checked local documents; no generated SQL."""

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from uuid import uuid4

from app.phase4.ownership import CandidateOwner, OwnershipError


class BusinessError(ValueError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def now() -> str:
    return datetime.now(UTC).isoformat()


def encode(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def owned_action(function):
    """Acquire ownership before tools, models, files or failure auditing run."""

    @wraps(function)
    def owned(self, *args, **kwargs):
        store = self.store if hasattr(self, "store") else self.reports.store
        with store.admission() as owner:
            directory = owner.generation_directory
            if directory is not None:
                business = self.business if hasattr(self, "business") else self
                reports = self.reports if hasattr(self, "reports") else self
                if hasattr(self, "reports") and not hasattr(business, "data_path"):
                    business = self.reports.business
                if (
                    hasattr(business, "data_path")
                    and Path(business.data_path).resolve() != directory / "dataset.csv"
                ) or (
                    hasattr(reports, "reports_dir")
                    and Path(reports.reports_dir).resolve() != directory / "reports"
                ):
                    raise BusinessError("stored_evidence_integrity_error")
            return function(self, *args, **kwargs)

    return owned


class Store:
    def __init__(self, path: Path, *, owner: CandidateOwner | None = None, lifecycle=False):
        self.path = Path(path).absolute()
        self.owner = owner
        self.epoch = owner.epoch if owner is not None else None
        self._thread = threading.local()
        self._mutex = owner.mutex if owner is not None else threading.RLock()
        self._closed = False
        self._lifecycle = lifecycle
        self._owns_owner = False
        if owner is not None and self.path.resolve() != owner.database:
            raise OwnershipError("Store path differs from owner")

    @contextmanager
    def admission(self):
        if self.owner is not None:
            with self._admission() as owner:
                yield owner
        else:
            with self._mutex:
                with self._admission() as owner:
                    yield owner

    @contextmanager
    def _admission(self):
        current = getattr(self._thread, "owner", None)
        if current is not None:
            with current.admission(self._thread.epoch):
                yield current
            return
        if self._closed:
            raise BusinessError("database_busy", 503)
        temporary = self.owner is None
        owner = self.owner
        try:
            if temporary:
                if os.environ.get("GAMEADS_INSTANCE_ROOT") and not self.path.parent.is_dir():
                    raise OwnershipError("Unbound installed path")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                owner = CandidateOwner(self.path).acquire()
                if self._lifecycle:
                    self.owner, self.epoch = owner, owner.epoch
                    self._owns_owner = True
                    temporary = False
            epoch = owner.epoch if temporary else self.epoch
            with owner.admission(epoch):
                if self._closed:
                    raise BusinessError("database_busy", 503)
                self._thread.owner, self._thread.epoch = owner, epoch
                try:
                    # Version checking is read-only and precedes every business
                    # side effect. v1 requires its explicitly recovered owner.
                    if self.path.exists():
                        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
                        try:
                            version = connection.execute("PRAGMA user_version").fetchone()[0]
                            if version != 0:
                                if version != 1 or temporary or not owner.ready:
                                    raise BusinessError("schema_upgrade_required", 503)
                                from app.phase4.store import CandidateStore

                                CandidateStore._check(connection)
                        finally:
                            connection.close()
                    yield owner
                finally:
                    self._thread.owner = None
        except OwnershipError:
            raise BusinessError("database_busy", 503) from None
        finally:
            if temporary and owner is not None:
                owner.close()

    def close(self):
        """Revoke this facade; its owner is released by the lifecycle manager."""
        self._closed = True
        with self._mutex:
            pass
        if self.owner is not None:
            with self.owner.mutex:
                while self.owner.active_operations:
                    self.owner._drained.wait()
            if self._owns_owner:
                self.owner.close()
                self._owns_owner = False

    @contextmanager
    def transaction(self):
        with self.admission() as owner:
            enlisted = getattr(self._thread, "enlisted", None)
            if enlisted is not None:
                yield enlisted
                return
            with owner.mutex:
                with self._transaction(owner) as connection:
                    yield connection

    @contextmanager
    def enlist(self, connection, task_id):
        """Internal task coordinator: reuse its transaction for legacy documents."""
        with self.admission():
            if getattr(self._thread, "enlisted", None) is not None:
                raise RuntimeError("Already enlisted")
            self._thread.enlisted, self._thread.task_id = connection, task_id
            try:
                yield
            finally:
                self._thread.enlisted, self._thread.task_id = None, None

    @contextmanager
    def _transaction(self, owner):
        if getattr(self._thread, "transaction", False):
            raise BusinessError("database_busy", 503)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        owner.active_connections += 1
        self._thread.transaction = True
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=10000")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS documents ("
                "id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "payload TEXT NOT NULL, sha256 TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY, document_id TEXT NOT NULL, action TEXT NOT NULL, "
                "created_at TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
            if owner.ready:
                owner.warm_reads()
        except BaseException:
            connection.rollback()
            if owner.ready:
                owner.warm_reads()
            raise
        finally:
            connection.close()
            owner.active_connections -= 1
            self._thread.transaction = False

    @staticmethod
    def load(connection, identity: str, kind: str) -> dict:
        row = connection.execute(
            "SELECT payload, sha256 FROM documents WHERE id=? AND kind=?", (identity, kind)
        ).fetchone()
        if row is None:
            raise BusinessError("not_found", 404)
        value = json.loads(row["payload"])
        if digest(value) != row["sha256"]:
            raise BusinessError("stored_evidence_integrity_error")
        return value

    @staticmethod
    def save(connection, identity: str, kind: str, value: dict, *, new=False):
        if new:
            connection.execute(
                "INSERT INTO documents(id,kind,payload,sha256) VALUES(?,?,?,?)",
                (identity, kind, encode(value), digest(value)),
            )
        else:
            connection.execute(
                "UPDATE documents SET payload=?,sha256=? WHERE id=? AND kind=?",
                (encode(value), digest(value), identity, kind),
            )

    @staticmethod
    def event(connection, identity: str, action: str, details: dict):
        connection.execute(
            "INSERT INTO events(document_id,action,created_at,payload) VALUES(?,?,?,?)",
            (identity, action, now(), encode(details)),
        )

    def require_unmanaged(self, connection, identity):
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_documents'"
            ).fetchone()
            and connection.execute(
                "SELECT 1 FROM task_documents WHERE document_id=?", (identity,)
            ).fetchone()
        ):
            task_id = getattr(self._thread, "task_id", None)
            if (
                task_id is None
                or not connection.execute(
                    "SELECT 1 FROM task_documents WHERE document_id=? AND task_id=?",
                    (identity, task_id),
                ).fetchone()
            ):
                raise BusinessError("task_managed_resource")

    def check_unmanaged(self, identity):
        if identity is not None:
            with self.transaction() as connection:
                self.require_unmanaged(connection, identity)

    def get(self, identity: str, kind: str) -> dict:
        with self.read_snapshot() as connection:
            return self.load(connection, identity, kind)

    @contextmanager
    def read_snapshot(self):
        # A GET never creates a database or takes a write transaction. Legacy
        # v0 reads remain available for compatibility; v1 needs its ready owner.
        if self._closed:
            raise BusinessError("database_busy", 503)
        if not self.path.is_file():
            raise BusinessError("schema_upgrade_required", 503)
        if self.owner is None and getattr(self._thread, "owner", None) is None:
            # Legacy read-only inspection needs no writer lease or lock file.
            # In particular, an unopened GET must not create owner.lock/parents.
            connection = sqlite3.connect(
                self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=1
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                if connection.execute("PRAGMA user_version").fetchone()[0] != 0:
                    raise BusinessError("schema_upgrade_required", 503)
                yield connection
            except sqlite3.OperationalError as exc:
                raise BusinessError("database_busy", 503) from exc
            finally:
                connection.close()
            return
        with self.admission() as owner:
            enlisted = getattr(self._thread, "enlisted", None)
            if enlisted is not None:
                yield enlisted
            elif owner.ready and hasattr(owner, "storage"):
                with owner.storage.snapshot() as connection:
                    yield connection
            else:
                connection = sqlite3.connect(
                    self.path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=1
                )
                owner.active_connections += 1
                try:
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA query_only=ON")
                    connection.execute("BEGIN")
                    yield connection
                except sqlite3.OperationalError as exc:
                    raise BusinessError("database_busy", 503) from exc
                finally:
                    connection.close()
                    owner.active_connections -= 1

    def events(self, identity: str, kind: str) -> list[dict]:
        with self.read_snapshot() as connection:
            self.load(connection, identity, kind)
            rows = connection.execute(
                "SELECT id,action,created_at,payload FROM events WHERE document_id=? ORDER BY id",
                (identity,),
            ).fetchall()
            return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]

    def failure(self, action: str, code: str, traces: list[dict]) -> str:
        identity = uuid4().hex
        with self.transaction() as connection:
            self.event(connection, identity, action, {"error_code": code, "tool_calls": traces})
        return identity
