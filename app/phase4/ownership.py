"""Cooperative Windows writer ownership; retain lock until all connections drain."""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class OwnershipError(RuntimeError):
    pass


class CandidateOwner:
    """Trusted local Windows candidate, not isolation from uncooperative writers.

    Never unlink the lock file: its existence is not ownership. Resolved path
    identity/ACL, hard-link aliases and deployment roots still require U04.
    """

    def __init__(self, database: Path, *, _control=False):
        database = Path(database)
        if not database.is_absolute() or not database.parent.is_dir():
            raise ValueError("Candidate requires an existing absolute local root")
        if str(database).startswith("\\\\"):
            raise ValueError("Network candidates are unsupported")
        for part in (database, *database.parents):
            if part.exists() and part.lstat().st_file_attributes & 0x400:
                raise ValueError("Reparse points are unsupported")
        self.database = database.resolve()
        self._control = _control
        if self.database.exists() and self.database.stat().st_nlink != 1:
            raise ValueError("Hard-linked databases are unsupported")
        self.mutex = threading.RLock()
        self._drained = threading.Condition(self.mutex)
        self.active_operations = 0
        self._handle = None
        self._pid = None
        self.epoch = None
        self.store_attached = False
        self.active_connections = 0
        self._closing = threading.Event()
        self._thread = threading.local()
        self.ready = False
        self._generation_owner = None
        self.generation_directory = None
        self._read_anchor = None

    def warm_reads(self):
        """Materialize WAL read marks in the write/startup lifecycle, before GET.

        No transaction is retained. The sole cross-thread connection is internal,
        query-only, and used/closed exclusively under the owner mutex. Business
        connections still belong to their calling thread and tracked leases.
        """
        with self.mutex:
            self.require()
            if self._read_anchor is None:
                self._read_anchor = sqlite3.connect(
                    self.database.as_uri() + "?mode=rw",
                    uri=True,
                    isolation_level=None,
                    check_same_thread=False,
                    timeout=1,
                )
                self._read_anchor.execute("PRAGMA query_only=ON")
            self._read_anchor.execute("SELECT count(*) FROM tasks").fetchone()

    def _bind_generation(self):
        """A managed physical path shares the instance lock for its entire lifetime.

        The sidecar is installed before bootstrap, while both paths are offline.
        Trusted-root/ACL requirements apply to it exactly as to the locator.
        Standalone databases retain the explicit-path cooperative owner protocol.
        """
        binding = self.database.with_name(self.database.name + ".generation.json")
        configured = os.environ.get("GAMEADS_INSTANCE_ROOT")
        if self._control:
            return
        if configured and not binding.exists():
            raise OwnershipError("Explicit database is not bound to the installed instance")
        if not binding.exists():
            # Missing metadata must not turn a previously managed physical path
            # into an unrestricted standalone writer.
            root = self.database.parent.parent
            if (root / "active.json").exists() and any(root.glob("*.generation.json")):
                raise OwnershipError("Missing generation binding")
            return
        try:
            record = json.loads(binding.read_text(encoding="utf-8"))
            root = Path(record["root"])
            if configured and root.resolve() != Path(configured).resolve():
                raise OwnershipError("Database belongs to another installed instance")
            instance = CandidateOwner(root / "instance", _control=True).acquire()
            self._generation_owner = instance
            if (root / "pending.json").exists():
                raise OwnershipError("Generation recovery required")
            from app.phase4.generation import GenerationRehearsal

            rehearsal = GenerationRehearsal(root)
            rehearsal.owner = instance
            locator, manifest = rehearsal.resolve()
            generation = record["generation"]
            if (
                locator["generation"] != generation
                or manifest["generation"] != generation
                or (root / manifest["directory"] / "database.sqlite3").resolve() != self.database
            ):
                raise OwnershipError("Inactive or inconsistent generation")
            self.generation_directory = self.database.parent
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise OwnershipError("Invalid generation binding") from exc

    def _release_generation(self):
        if self._generation_owner is not None:
            self._generation_owner.close()
            self._generation_owner = None
            self.generation_directory = None

    def acquire(self):
        if os.name != "nt":
            raise OwnershipError("Only local Windows candidates are validated")
        import msvcrt

        with self.mutex:
            if self._handle is not None:
                raise OwnershipError("Owner already acquired")
            try:
                self._bind_generation()
            except BaseException:
                self._release_generation()
                raise
            lock = self.database.with_name(self.database.name + ".owner.lock")
            handle = None
            try:
                if lock.exists() and (lock.is_symlink() or lock.stat().st_nlink != 1):
                    raise OwnershipError("Unsafe ownership object")
                try:
                    handle = lock.open("x+b")
                    handle.write(b"\0")
                    handle.flush()
                except FileExistsError:
                    handle = lock.open("r+b")
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except BaseException as exc:
                if handle is not None:
                    handle.close()
                self._release_generation()
                if isinstance(exc, OSError):
                    raise OwnershipError("Candidate already owned") from exc
                raise
            self._handle, self._pid = handle, os.getpid()
            self.epoch = object()
            self.store_attached = False
            self.ready = False
            self._closing.clear()
        return self

    def _release_lock(self):
        import msvcrt

        if self._handle is not None:
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            self._handle.close()
        self._handle, self._pid = None, None

    def require(self):
        if self._handle is None or self._pid != os.getpid():
            raise OwnershipError("Candidate ownership required")
        if self._closing.is_set() and not getattr(self._thread, "depth", 0):
            raise OwnershipError("Owner is draining")

    @contextmanager
    def admission(self, epoch):
        """Register an entire business action, including file publication.

        A closing owner rejects queued newcomers. Already admitted actions may
        finish their nested transactions on the same thread before lock release.
        """
        with self.mutex:
            self.require()
            if epoch is not self.epoch:
                raise OwnershipError("Stale owner lifecycle")
            self._thread.depth = getattr(self._thread, "depth", 0) + 1
            self.active_operations += 1
        try:
            yield self
        finally:
            with self.mutex:
                self._thread.depth -= 1
                self.active_operations -= 1
                self._drained.notify_all()

    def close(self):
        # Store read/write connections hold this same mutex for their entire
        # lifetime. Closing waits for them before releasing the OS byte lock.
        if getattr(self._thread, "depth", 0):
            raise OwnershipError("Cannot release ownership inside an admitted operation")
        self._closing.set()
        with self.mutex:
            if self._handle is None or self._pid != os.getpid():
                raise OwnershipError("Candidate ownership required")
            while self.active_operations:
                self._drained.wait()
            if self.active_connections:
                self._closing.clear()
                raise OwnershipError("Cannot release ownership inside an active transaction")
            if self._read_anchor is not None:
                self._read_anchor.close()
                self._read_anchor = None
            self._release_lock()
            self._release_generation()
            self.ready = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()
