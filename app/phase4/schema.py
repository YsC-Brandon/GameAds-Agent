"""Frozen-r4 logical candidate schema; never execute against an active v0 DB."""

import hashlib
import sqlite3
from functools import cache

DDL = """
CREATE TABLE documents(id TEXT PRIMARY KEY,kind TEXT NOT NULL,
 payload TEXT NOT NULL,sha256 TEXT NOT NULL);
CREATE TABLE events(id INTEGER PRIMARY KEY,document_id TEXT NOT NULL,
 action TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE schema_migrations(
 version INTEGER PRIMARY KEY CHECK(version=1),applied_at TEXT NOT NULL,
 source_version INTEGER NOT NULL CHECK(source_version IN (0,1)),
 migration_digest TEXT NOT NULL CHECK(length(migration_digest)=64
 AND migration_digest NOT GLOB '*[^0-9a-f]*'));
CREATE TABLE tasks(
 id TEXT PRIMARY KEY NOT NULL CHECK(length(id)=32 AND id NOT GLOB '*[^0-9a-f]*'),
 schema_version INTEGER NOT NULL CHECK(schema_version=1),
 kind TEXT NOT NULL CHECK(kind IN ('analysis','budget_review','report')),
 status TEXT NOT NULL CHECK(status IN ('submitted','running','awaiting_approval',
 'approved','simulated','completed','rejected','failed','needs_clarification',
 'unsupported','insufficient_evidence','interrupted')),
 version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>=1),
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 retry_of TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
 request_fingerprint TEXT NOT NULL CHECK(length(request_fingerprint)=64
 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
 validated_request TEXT,agent_result TEXT,error TEXT,source_sha256 TEXT,
 CHECK(retry_of IS NULL OR retry_of<>id),
 CHECK((kind='analysis' AND validated_request IS NULL) OR
 (kind IN ('budget_review','report') AND validated_request IS NOT NULL)),
 CHECK(kind='analysis' OR agent_result IS NULL),
 CHECK(status NOT IN ('awaiting_approval','approved','simulated','rejected')
 OR kind='budget_review'),
 CHECK(status NOT IN ('needs_clarification','unsupported','insufficient_evidence')
 OR kind='analysis'),
 CHECK(source_sha256 IS NULL OR (length(source_sha256)=64
 AND source_sha256 NOT GLOB '*[^0-9a-f]*')));
CREATE TABLE task_documents(
 task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
 document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
 role TEXT NOT NULL CHECK(role IN ('proposal','report')),
 PRIMARY KEY(task_id,role),UNIQUE(document_id));
CREATE TABLE task_events(
 task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
 seq INTEGER NOT NULL CHECK(typeof(seq)='integer' AND seq>=1),
 action TEXT NOT NULL,from_status TEXT CHECK(from_status IS NULL OR from_status IN (
 'submitted','running','awaiting_approval','approved','simulated','completed','rejected',
 'failed','needs_clarification','unsupported','insufficient_evidence','interrupted')),
 to_status TEXT NOT NULL CHECK(to_status IN ('submitted','running','awaiting_approval',
 'approved','simulated','completed','rejected','failed','needs_clarification','unsupported',
 'insufficient_evidence','interrupted')),
 created_at TEXT NOT NULL,code TEXT,PRIMARY KEY(task_id,seq));
CREATE TABLE operations(
 operation_id TEXT PRIMARY KEY NOT NULL CHECK(length(operation_id)=32
 AND operation_id NOT GLOB '*[^0-9a-f]*'),
 method TEXT NOT NULL CHECK(method='POST'),path TEXT NOT NULL COLLATE BINARY,
 key TEXT NOT NULL COLLATE BINARY CHECK(length(key) BETWEEN 1 AND 128
 AND key NOT GLOB '*[^A-Za-z0-9._:-]*'),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^0-9a-f]*'),
 task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
 phase TEXT NOT NULL CHECK(phase IN ('in_progress','completed','interrupted')),
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,sealed_http_status INTEGER,response BLOB,
 UNIQUE(method,path,key),
 CHECK(path='/api/v1/tasks' OR (task_id IS NOT NULL AND path IN (
 '/api/v1/tasks/'||task_id||'/approve','/api/v1/tasks/'||task_id||'/reject',
 '/api/v1/tasks/'||task_id||'/simulate','/api/v1/tasks/'||task_id||'/report'))),
 CHECK((phase='in_progress' AND sealed_http_status IS NULL AND response IS NULL) OR
 (phase='completed' AND sealed_http_status IS NOT NULL
 AND typeof(sealed_http_status)='integer' AND sealed_http_status BETWEEN 200 AND 599
 AND response IS NOT NULL AND typeof(response)='blob') OR
 (phase='interrupted' AND sealed_http_status IS NOT NULL AND sealed_http_status=409
 AND response IS NOT NULL AND typeof(response)='blob')));
CREATE UNIQUE INDEX operation_single_occupant ON operations(task_id)
 WHERE task_id IS NOT NULL AND phase='in_progress';
CREATE INDEX task_history ON tasks(created_at DESC,id DESC);
CREATE INDEX operation_recovery ON operations(phase,operation_id);
CREATE TRIGGER operation_identity_immutable BEFORE UPDATE ON operations
 WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.method IS NOT OLD.method
 OR NEW.path IS NOT OLD.path OR NEW.key IS NOT OLD.key
 OR NEW.fingerprint IS NOT OLD.fingerprint OR NEW.created_at IS NOT OLD.created_at
 OR (NEW.task_id IS NOT OLD.task_id AND NOT(OLD.phase='in_progress'
 AND OLD.path='/api/v1/tasks' AND OLD.task_id IS NULL AND NEW.task_id IS NOT NULL))
 BEGIN SELECT RAISE(ABORT,'operation_identity_immutable'); END;
CREATE TRIGGER operation_sealed_immutable BEFORE UPDATE ON operations
 WHEN OLD.phase IN ('completed','interrupted')
 BEGIN SELECT RAISE(ABORT,'operation_sealed_immutable'); END;
CREATE TRIGGER task_request_immutable BEFORE UPDATE ON tasks
 WHEN NEW.id IS NOT OLD.id OR NEW.kind IS NOT OLD.kind
 OR NEW.schema_version IS NOT OLD.schema_version OR NEW.created_at IS NOT OLD.created_at
 OR NEW.retry_of IS NOT OLD.retry_of OR NEW.request_fingerprint IS NOT OLD.request_fingerprint
 OR NEW.validated_request IS NOT OLD.validated_request
 BEGIN SELECT RAISE(ABORT,'task_request_immutable'); END;
"""

# Reject REPLACE as well as ordinary UPDATE/DELETE: replacement must not erase
# sealed results, ownership, append-only history or immutable migration records.
for table, identity in (
    (
        "operations",
        "operation_id=NEW.operation_id OR (method=NEW.method AND path=NEW.path AND key=NEW.key)",
    ),
    ("tasks", "id=NEW.id"),
    ("task_events", "task_id=NEW.task_id AND seq=NEW.seq"),
    ("task_documents", "document_id=NEW.document_id OR (task_id=NEW.task_id AND role=NEW.role)"),
    ("schema_migrations", "version=NEW.version"),
):
    DDL += f"""
CREATE TRIGGER {table}_no_reinsert BEFORE INSERT ON {table}
 WHEN EXISTS(SELECT 1 FROM {table} WHERE {identity})
 BEGIN SELECT RAISE(ABORT,'immutable_identity'); END;
CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
 BEGIN SELECT RAISE(ABORT,'immutable_record'); END;
"""
for table in ("task_events", "task_documents", "schema_migrations"):
    DDL += f"""
CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
 BEGIN SELECT RAISE(ABORT,'immutable_record'); END;
"""

SCHEMA_DIGEST = hashlib.sha256(DDL.encode()).hexdigest()


def catalog(connection):
    rows = []
    for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ):
        kind, name, table, sql = row
        # Phase 3's equivalent legacy DDL includes different whitespace. Retain
        # the source table SQL in migrated files; compare its known definition.
        if kind == "table" and name in {"documents", "events"}:
            sql = "".join(sql.split())
        rows.append((kind, name, table, sql))
    return rows


@cache
def expected_catalog():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(DDL)
        return catalog(connection)
    finally:
        connection.close()
