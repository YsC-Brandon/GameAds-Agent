"""Offline candidate tools. Never activate, replace or downgrade a database."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from app.phase3.store import BusinessError, digest, now
from app.phase4.ownership import CandidateOwner, OwnershipError
from app.phase4.schema import DDL, SCHEMA_DIGEST, catalog, expected_catalog
from app.phase4.store import CandidateStore


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_file(path):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith("\\\\"):
        raise ValueError("An absolute local path is required")
    for part in (path, *path.parents):
        if part.exists() and (part.is_symlink() or part.lstat().st_file_attributes & 0x400):
            raise ValueError("Reparse points are unsupported")
    if path.exists() and path.is_file() and path.stat().st_nlink != 1:
        raise ValueError("Hard links are unsupported")
    return path


class _OwnedConnection(sqlite3.Connection):
    """Keep maintenance handles inside the same owner/connection lifetime."""

    _owner = None

    def close(self):
        owner = self._owner
        # A wrong-thread close raises without closing SQLite. Keep ownership
        # registered until the actual owning thread closes its handle.
        super().close()
        if owner is not None:
            self._owner = None
            owner.active_connections -= 1
            owner.mutex.release()


def connect(path, *, readonly=True, owner=None):
    if not readonly:
        if owner is None:
            raise OwnershipError("Maintenance writes require an explicit owner")
        owner.require()
    if owner is not None and path.resolve() != owner.database:
        raise OwnershipError("Maintenance path differs from owner")
    if owner is not None:
        owner.mutex.acquire()
    connection = None
    try:
        if owner is not None:
            owner.require()
        connection = sqlite3.connect(
            path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
            uri=True,
            isolation_level=None,
            timeout=1,
            factory=_OwnedConnection,
        )
        if owner is not None:
            connection._owner = owner
            owner.active_connections += 1
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA synchronous=FULL")
        return connection
    except BaseException:
        if connection is not None:
            connection.close()
        elif owner is not None:
            owner.mutex.release()
        raise


def inspect(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise BusinessError("stored_evidence_integrity_error")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise BusinessError("stored_evidence_integrity_error")
    if version == 1:
        CandidateStore._check(connection)
        from app.phase4.integrity import validate_records

        validate_records(connection)
    elif version == 0:
        expected = [row for row in expected_catalog() if row[2] in {"documents", "events"}]
        if catalog(connection) != expected:
            raise BusinessError("schema_upgrade_required", 503)
        tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if tables != {"documents", "events"}:
            raise BusinessError("schema_upgrade_required", 503)
        columns = {
            "documents": ["id", "kind", "payload", "sha256"],
            "events": ["id", "document_id", "action", "created_at", "payload"],
        }
        for table, expected in columns.items():
            if [r[1] for r in connection.execute(f"PRAGMA table_info({table})")] != expected:
                raise BusinessError("schema_upgrade_required", 503)
    else:
        raise BusinessError("schema_upgrade_required", 503)
    reports = []
    for row in connection.execute("SELECT id,kind,payload,sha256 FROM documents"):
        payload = json.loads(row["payload"])
        if row["kind"] not in {"proposal", "report"} or digest(payload) != row["sha256"]:
            raise BusinessError("stored_evidence_integrity_error")
        if row["kind"] == "proposal":
            if digest(payload["payload"]) != payload["proposal_sha256"]:
                raise BusinessError("stored_evidence_integrity_error")
        else:
            reports.append(payload)
    for row in connection.execute("SELECT payload FROM events"):
        json.loads(row[0])
    return {"version": version, "reports": reports}


def check(database):
    database = local_file(database)
    with CandidateOwner(database), closing(connect(database)) as connection:
        result = inspect(connection)
        return {"schema_version": result["version"], "integrity": "ok"}


def initialize(database):
    database = local_file(database)
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        candidate.checkpoint()
    return {"schema_version": 1, "candidate_only": True, "activated": False}


@contextmanager
def source_snapshot(database):
    """Recover a quiescent source only on a copy; preserve original WAL/SHM bytes.

    U04 must exclude uncooperative writers. The cooperative lock alone cannot
    make an arbitrary live file copy consistent.
    """
    with CandidateOwner(database), tempfile.TemporaryDirectory(prefix="gameads-backup-") as work:
        copied = Path(work) / "database.sqlite3"
        before = {}
        for suffix in ("", "-wal", "-shm", "-journal"):
            original = local_file(database.with_name(database.name + suffix))
            if original.exists():
                before[suffix] = sha(original)
                shutil.copyfile(original, copied.with_name(copied.name + suffix))
        if "" not in before:
            raise FileNotFoundError(database)
        try:
            with CandidateOwner(copied) as owner:
                with closing(connect(copied, readonly=False, owner=owner)) as connection:
                    yield connection
        finally:
            after = {
                suffix: sha(database.with_name(database.name + suffix))
                for suffix in ("", "-wal", "-shm", "-journal")
                if database.with_name(database.name + suffix).exists()
            }
            if after != before:
                raise BusinessError("stored_evidence_integrity_error")


def backup(database, destination, reports_dir, dataset):
    """A new complete synthetic evidence set under the same service owner lock."""
    database, destination, reports_dir, dataset = map(
        local_file, (database, destination, reports_dir, dataset)
    )
    with source_snapshot(database) as source:
        source.execute("BEGIN")
        info = inspect(source)
        destination.mkdir()  # Exclusive: preserve any existing backup/failed run.
        target = destination / "database.sqlite3"
        with target.open("xb"):
            pass
        with (
            CandidateOwner(target) as target_owner,
            closing(connect(target, readonly=False, owner=target_owner)) as output,
        ):
            source.backup(output)
            if info["version"] == 1:
                output.execute("PRAGMA journal_mode=WAL")
            inspect(output)
        files = []
        report_output = destination / "reports"
        report_output.mkdir()
        for report in info["reports"]:
            identity = report["id"]
            if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
                raise BusinessError("stored_evidence_integrity_error")
            filename = identity + ".md"
            original = local_file(reports_dir / filename)
            if sha(original) != report["content_sha256"]:
                raise BusinessError("report_file_integrity_error")
            if digest(report["tool_results"]) != report["evidence_sha256"]:
                raise BusinessError("stored_evidence_integrity_error")
            shutil.copyfile(original, report_output / filename)
            files.append("reports/" + filename)
        shutil.copyfile(dataset, destination / "dataset.csv")
        files += ["database.sqlite3", "dataset.csv"]
        manifest = {
            "format": 1,
            "created_at": now(),
            "schema_version": info["version"],
            "files": {name: sha(destination / name) for name in files},
            "complete": True,
            "power_loss_durability": "UNPROVEN",
        }
        for name in files:
            with (destination / name).open("r+b") as content:
                os.fsync(content.fileno())
        with (destination / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        return manifest


def verify_backup(directory):
    directory = local_file(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("complete") is not True:
        raise BusinessError("stored_evidence_integrity_error")
    for name, expected in manifest["files"].items():
        path = local_file(directory / name)
        if path.resolve().is_relative_to(directory.resolve()) is False or sha(path) != expected:
            raise BusinessError("stored_evidence_integrity_error")
    if not {"database.sqlite3", "dataset.csv"}.issubset(manifest["files"]):
        raise BusinessError("stored_evidence_integrity_error")
    # Hashing only the listed entries is insufficient: a truncated manifest
    # could silently omit a registered report and still pass every listed hash.
    with closing(connect(directory / "database.sqlite3")) as connection:
        info = inspect(connection)
    expected_files = {"database.sqlite3", "dataset.csv"}
    for report in info["reports"]:
        identity = report["id"]
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise BusinessError("stored_evidence_integrity_error")
        name = "reports/" + identity + ".md"
        expected_files.add(name)
        if (
            manifest["files"].get(name) != report["content_sha256"]
            or digest(report["tool_results"]) != report["evidence_sha256"]
        ):
            raise BusinessError("stored_evidence_integrity_error")
    if set(manifest["files"]) != expected_files or manifest["schema_version"] != info["version"]:
        raise BusinessError("stored_evidence_integrity_error")
    return manifest


def report_inventory(database, reports_dir):
    """Read-only diagnosis; never delete orphan files or fabricate registration."""
    database, reports_dir = map(local_file, (database, reports_dir))
    with CandidateOwner(database), closing(connect(database)) as connection:
        info = inspect(connection)
        expected = {r["id"] + ".md": r["content_sha256"] for r in info["reports"]}
        actual = {p.name: p for p in reports_dir.glob("*.md")}
        missing = sorted(set(expected) - set(actual))
        orphan = sorted(set(actual) - set(expected))
        damaged = sorted(
            name
            for name in set(actual) & set(expected)
            if sha(local_file(actual[name])) != expected[name]
        )
        return {
            "missing": missing,
            "orphan": orphan,
            "damaged": damaged,
            "temporary": sorted(p.name for p in reports_dir.glob("*.tmp")),
        }


def migrate_candidate(backup_dir, database):
    """Build a v1 copy. No source writes, active locator or implicit activation."""
    backup_dir, database = map(local_file, (backup_dir, database))
    manifest = verify_backup(backup_dir)
    source_path = backup_dir / "database.sqlite3"
    with CandidateOwner(source_path), CandidateOwner(database) as target_owner:
        verify_backup(backup_dir)
        with closing(connect(source_path)) as source:
            info = inspect(source)
            with database.open("xb"):
                pass
            with closing(connect(database, readonly=False, owner=target_owner)) as output:
                source.backup(output)
                output.execute("PRAGMA journal_mode=WAL")
                output.execute("PRAGMA synchronous=FULL")
                if info["version"] == 0:
                    # Legacy tables are retained byte-for-byte at the row level.
                    additions = DDL[DDL.index("CREATE TABLE schema_migrations") :]
                    try:
                        output.executescript("BEGIN IMMEDIATE;\n" + additions)
                        output.execute(
                            "INSERT INTO schema_migrations VALUES(1,?,0,?)", (now(), SCHEMA_DIGEST)
                        )
                        output.execute("PRAGMA user_version=1")
                        output.commit()
                    except BaseException:
                        output.rollback()
                        raise
                inspect(output)
                for table in ("documents", "events"):
                    if [
                        tuple(r) for r in source.execute(f"SELECT * FROM {table} ORDER BY rowid")
                    ] != [
                        tuple(r) for r in output.execute(f"SELECT * FROM {table} ORDER BY rowid")
                    ]:
                        raise BusinessError("stored_evidence_integrity_error")
                output.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        "candidate_only": True,
        "activated": False,
        "source_version": manifest["schema_version"],
    }


def restore_candidate(backup_dir, destination):
    """Restore a verified set into a new directory; never replace active data."""
    backup_dir, destination = map(local_file, (backup_dir, destination))
    with CandidateOwner(backup_dir / "database.sqlite3"):
        manifest = verify_backup(backup_dir)
        destination.mkdir()
        for name in manifest["files"]:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup_dir / name, target)
            if sha(target) != manifest["files"][name]:
                raise BusinessError("stored_evidence_integrity_error")
        # A restored v1 snapshot may reopen its own WAL; no active locator changes.
        database = destination / "database.sqlite3"
        with (
            CandidateOwner(database) as target_owner,
            closing(connect(database, readonly=False, owner=target_owner)) as connection,
        ):
            if manifest["schema_version"] == 1:
                connection.execute("PRAGMA journal_mode=WAL")
            inspect(connection)
        # Publish the complete marker only after the whole restored set passed.
        for name in manifest["files"]:
            with (destination / name).open("r+b") as content:
                os.fsync(content.fileno())
        with (destination / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    return {"candidate_only": True, "activated": False, "restored_files": len(manifest["files"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "check"):
        sub.add_parser(name).add_argument("--database", type=Path, required=True)
    backup_parser = sub.add_parser("backup")
    for name in ("database", "destination", "reports-dir", "dataset"):
        backup_parser.add_argument("--" + name, type=Path, required=True)
    migrate = sub.add_parser("migrate-candidate")
    migrate.add_argument("--backup-dir", type=Path, required=True)
    migrate.add_argument("--database", type=Path, required=True)
    restore = sub.add_parser("restore-candidate")
    restore.add_argument("--backup-dir", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    inventory = sub.add_parser("report-inventory")
    inventory.add_argument("--database", type=Path, required=True)
    inventory.add_argument("--reports-dir", type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    functions = {
        "init": initialize,
        "check": check,
        "backup": backup,
        "migrate-candidate": migrate_candidate,
        "restore-candidate": restore_candidate,
        "report-inventory": report_inventory,
    }
    try:
        result = functions[command](**args)
    except (BusinessError, OwnershipError, OSError, ValueError, sqlite3.Error, KeyError):
        print(json.dumps({"status": "error", "code": "maintenance_failed"}))
        return 1
    print(json.dumps({"status": "ok", "data": result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
