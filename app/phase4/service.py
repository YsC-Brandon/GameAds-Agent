"""Synchronous task coordinator; documents, events and sealed results share commits."""

import hashlib
import json
import sqlite3

from app.phase3.contracts import ApprovalRequest, RejectionRequest, ReportRequest, SimulationRequest
from app.phase3.reports import ReportService
from app.phase3.service import BusinessService
from app.phase3.store import BusinessError, Store, digest, encode, now, owned_action

MESSAGE = "Request could not be completed. Check inputs/configuration."
RETRY_STATES = {
    "failed",
    "needs_clarification",
    "unsupported",
    "insufficient_evidence",
    "interrupted",
}


def failure(code, task_id=None, retryable=False):
    return {
        "status": "error",
        "code": code,
        "message": MESSAGE,
        "task_id": task_id,
        "retryable": retryable,
    }


class TaskService:
    def __init__(self, candidate, *, data_path, reports_dir, agent, thresholds=None):
        self.candidate = candidate
        self.store = Store(candidate.path, owner=candidate.owner)
        self.business = BusinessService(self.store, data_path=data_path, thresholds=thresholds)
        self.reports = ReportService(self.business, reports_dir)
        self.agent = agent

    @staticmethod
    def record(connection, identity):
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise BusinessError("not_found", 404)
        document = dict(row)
        relations = {
            r["role"]: r["document_id"]
            for r in connection.execute(
                "SELECT role,document_id FROM task_documents WHERE task_id=?", (identity,)
            )
        }
        active = connection.execute(
            "SELECT operation_id FROM operations WHERE task_id=? AND phase='in_progress'",
            (identity,),
        ).fetchone()
        return {
            key: document[key]
            for key in (
                "id",
                "kind",
                "status",
                "version",
                "created_at",
                "updated_at",
                "retry_of",
                "source_sha256",
            )
        } | {
            "proposal_id": relations.get("proposal"),
            "report_id": relations.get("report"),
            "agent_result": json.loads(document["agent_result"])
            if document["agent_result"]
            else None,
            "error": json.loads(document["error"]) if document["error"] else None,
            "active_operation_id": active[0] if active else None,
        }

    def get(self, identity):
        with self.candidate.snapshot() as connection:
            return {"status": "ok", "data": self.record(connection, identity)}

    def page(self, *, kind=None, status=None, limit=50, offset=0, task_id=None, collection=None):
        with self.candidate.snapshot() as connection:
            if task_id is not None:
                self.record(connection, task_id)
            if collection == "events":
                rows = [
                    dict(r)
                    for r in connection.execute(
                        "SELECT * FROM task_events WHERE task_id=? ORDER BY seq LIMIT ? OFFSET ?",
                        (task_id, limit + 1, offset),
                    )
                ]
            elif collection == "reports":
                rows = []
                for r in connection.execute(
                    "SELECT document_id FROM task_documents WHERE task_id=? AND role='report'",
                    (task_id,),
                ):
                    doc = self.store.load(connection, r[0], "report")
                    rows.append(
                        {
                            k: doc[k]
                            for k in ("id", "created_at", "content_sha256", "evidence_sha256")
                        }
                        | {"task_id": task_id}
                    )
                rows = rows[offset : offset + limit + 1]
            else:
                where, params = [], []
                for field, value in (("kind", kind), ("status", status)):
                    if value is not None:
                        where.append(field + "=?")
                        params.append(value)
                sql = "SELECT id FROM tasks" + (" WHERE " + " AND ".join(where) if where else "")
                rows = [
                    self.record(connection, r[0])
                    for r in connection.execute(
                        sql + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                        params + [limit + 1, offset],
                    )
                ]
            return {
                "items": rows[:limit],
                "limit": limit,
                "offset": offset,
                "has_more": len(rows) > limit,
            }

    def lookup(self, method, path, key):
        with self.candidate.snapshot() as connection:
            row = connection.execute(
                "SELECT operation_id,method,path,task_id,phase,created_at,updated_at,"
                "sealed_http_status "
                "FROM operations WHERE method=? AND path=? AND key=?",
                (method, path, key),
            ).fetchone()
            if row is None:
                raise BusinessError("not_found", 404)
            return {"status": "ok", "data": dict(row)}

    def _seal(self, connection, operation, status, body):
        if status < 400:
            row = connection.execute(
                "SELECT task_id FROM operations WHERE operation_id=?", (operation,)
            ).fetchone()
            if row and row[0]:
                task = connection.execute(
                    "SELECT validated_request,agent_result FROM tasks WHERE id=?", (row[0],)
                ).fetchone()
                evidence = sum(len(value.encode("utf-8")) for value in task if value is not None)
                evidence += sum(
                    len(item[0].encode("utf-8"))
                    for item in connection.execute(
                        "SELECT d.payload FROM documents d JOIN task_documents t "
                        "ON t.document_id=d.id WHERE t.task_id=?",
                        (row[0],),
                    )
                )
                if evidence > 2 * 1024 * 1024:
                    raise BusinessError("result_too_large", 413)
        raw = encode(body).encode("utf-8")
        self.candidate.seal(connection, operation, status, raw)
        return status, raw

    def _edge(self, connection, task, target, *, action=None, code=None):
        changed = connection.execute(
            "UPDATE tasks SET status=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (target, now(), task["id"], task["version"]),
        ).rowcount
        if changed != 1:
            raise BusinessError("stale_task_version")
        self.candidate.event(
            connection, task["id"], action or target, task["status"], target, code=code
        )

    @owned_action
    def execute(self, identity, body, *, task_id=None, action=None):
        method, path, key = identity
        fingerprint = digest(body.model_dump(mode="json"))
        try:
            operation, fresh = self.candidate.reserve(
                method, path.encode("ascii"), key.encode("ascii"), fingerprint, task_id=task_id
            )
        except sqlite3.OperationalError as exc:
            raise BusinessError("database_busy", 503) from exc
        if not fresh:
            if operation["phase"] == "in_progress":
                return 409, encode(failure("operation_in_progress", operation["task_id"])).encode()
            return operation["sealed_http_status"], operation["response"]
        op = operation["operation_id"]
        if task_id is None:
            return self._create(op, body)
        return self._continue(op, task_id, action, body)

    def _create(self, op, body):
        published = None
        try:
            if body.retry_of:
                previous = self.get(body.retry_of)["data"]
                if previous["status"] not in RETRY_STATES or previous["kind"] != body.kind:
                    raise BusinessError("invalid_retry_of")
            task_id = self.candidate.create_submitted(
                op,
                body.kind,
                validated_request=None
                if body.kind == "analysis"
                else body.request.model_dump(mode="json"),
                retry_of=body.retry_of,
            )
            self.candidate.mark_running(op, 1)
            # Agent/model calls run with ownership but without an SQLite write transaction.
            agent_result = self.agent.run(body.request) if body.kind == "analysis" else None
            with self.candidate.unit_of_work(op) as connection:
                task = self.record(connection, task_id)
                with self.store.enlist(connection, task_id):
                    if body.kind == "analysis":
                        result = agent_result.model_dump(mode="json")
                        # Persist structured plans/tool evidence, not model prose
                        # that can repeat the original private natural-language input.
                        result["final_answer"] = "Inspect the structured plan and tool evidence."
                        result["insights"] = []
                        result["errors"] = []
                        if len(encode(result).encode()) > 2 * 1024 * 1024:
                            raise BusinessError("evidence_too_large", 413)
                        target = agent_result.status
                        if target not in {
                            "completed",
                            "needs_clarification",
                            "unsupported",
                            "insufficient_evidence",
                        }:
                            raise BusinessError(
                                "model_not_configured"
                                if target == "model_unavailable"
                                else "agent_failed",
                                503 if target == "model_unavailable" else 502,
                            )
                        connection.execute(
                            "UPDATE tasks SET agent_result=? WHERE id=?", (encode(result), task_id)
                        )
                    elif body.kind == "budget_review":
                        proposal = self.business.propose_budget_reallocation(body.request)
                        if len(encode(proposal).encode()) > 2 * 1024 * 1024:
                            raise BusinessError("evidence_too_large", 413)
                        connection.execute(
                            "INSERT INTO task_documents VALUES(?,?,'proposal')",
                            (task_id, proposal["id"]),
                        )
                        connection.execute(
                            "UPDATE tasks SET source_sha256=? WHERE id=?",
                            (proposal["payload"]["source"]["dataset_sha256"], task_id),
                        )
                        target = "awaiting_approval"
                    else:
                        report = self.reports.generate_campaign_report(
                            ReportRequest.model_validate(body.request.model_dump(mode="json"))
                        )
                        published = report
                        self._link_report(connection, task_id, report)
                        target = "completed"
                self._edge(connection, task, target)
                status = 201 if target in {"completed", "awaiting_approval"} else 422
                if status == 201:
                    result = {
                        "status": "ok",
                        "data": self.record(connection, task_id) | {"active_operation_id": None},
                    }
                else:
                    result = failure(target, task_id)
                return self._seal(connection, op, status, result)
        except BusinessError as exc:
            return self._fail(op, exc, published=published)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return self._fail(op, BusinessError("internal_error", 500), published=published)

    def _link_report(self, connection, task_id, report):
        if len(encode(report).encode()) > 2 * 1024 * 1024:
            raise BusinessError("evidence_too_large", 413)
        connection.execute(
            "INSERT INTO task_documents VALUES(?,?,'report')", (task_id, report["id"])
        )
        connection.execute(
            "UPDATE tasks SET source_sha256=? WHERE id=?",
            (report["source"]["dataset_sha256"], task_id),
        )
        task = self.record(connection, task_id)
        self.candidate.event(
            connection, task_id, "report_available", task["status"], task["status"]
        )

    def _continue(self, op, task_id, action, body):
        published = None
        try:
            with self.candidate.unit_of_work(op) as connection:
                task = self.record(connection, task_id)
                required = {
                    "approve": "awaiting_approval",
                    "reject": "awaiting_approval",
                    "simulate": "approved",
                    "report": "simulated",
                }[action]
                if task["kind"] != "budget_review" or task["status"] != required:
                    raise BusinessError("invalid_task_transition")
                if body.expected_task_version != task["version"]:
                    raise BusinessError("stale_task_version")
                with self.store.enlist(connection, task_id):
                    if action == "report":
                        proposal = self.store.load(connection, task["proposal_id"], "proposal")
                        if (
                            hashlib.sha256(self.business.data_path.read_bytes()).hexdigest()
                            != (proposal["payload"]["source"]["dataset_sha256"])
                        ):
                            raise BusinessError("dataset_changed_during_analysis", 409)
                        request = proposal["payload"]["request"]
                        report = self.reports.generate_campaign_report(
                            ReportRequest(
                                start_date=request["start_date"],
                                end_date=request["end_date"],
                                campaign_ids=[r["campaign_id"] for r in request["campaigns"]],
                                proposal_id=proposal["id"],
                            )
                        )
                        published = report
                        self._link_report(connection, task_id, report)
                        target = "completed"
                    else:
                        types = {
                            "approve": ApprovalRequest,
                            "reject": RejectionRequest,
                            "simulate": SimulationRequest,
                        }
                        request = types[action].model_validate(
                            body.model_dump(exclude={"expected_task_version"})
                        )
                        functions = {
                            "approve": self.business.approve_budget_proposal,
                            "reject": self.business.reject_budget_proposal,
                            "simulate": self.business.simulate_budget_changes,
                        }
                        proposal = functions[action](task["proposal_id"], request)
                        target = {
                            "approve": "approved",
                            "reject": "rejected",
                            "simulate": "simulated",
                        }[action]
                self._edge(connection, task, target)
                result = {
                    "status": "ok",
                    "data": self.record(connection, task_id) | {"active_operation_id": None},
                }
                return self._seal(connection, op, 200, result)
        except BusinessError as exc:
            return self._fail(op, exc, published=published)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return self._fail(op, BusinessError("internal_error", 500), published=published)

    def _fail(self, op, exc, *, published=None):
        # Task errors use r4 statuses; never alter the legacy service contract.
        mapped = {
            "evidence_too_large": ("result_too_large", 413),
            "report_too_large": ("result_too_large", 413),
            "data_source_error": ("data_source_error", 500),
            "data_validation_error": ("data_validation_error", 500),
            "tool_failed": ("tool_failed", 502),
            "dataset_changed_during_analysis": ("dataset_changed_during_analysis", 409),
            "proposal_report_evidence_mismatch": ("report_proposal_scope_mismatch", 409),
            "agent_report_evidence_mismatch": ("agent_report_evidence_mismatch", 502),
            "agent_report_missing_citation": ("agent_report_missing_citation", 502),
        }
        if exc.code in mapped:
            exc = BusinessError(*mapped[exc.code])
        # unit_of_work deliberately rejects a sealed operation before yielding.
        # Resolve a lost COMMIT acknowledgement through a fresh read first;
        # otherwise that guard prevents returning the already durable result.
        with self.candidate.snapshot() as connection:
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (op,)
            ).fetchone()
            if operation["phase"] != "in_progress":
                return operation["sealed_http_status"], operation["response"]
        with self.candidate.unit_of_work(op) as connection:
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (op,)
            ).fetchone()
            # A commit may have succeeded even when its caller saw an error.
            # Never delete a registered report or overwrite an already sealed result.
            if operation["phase"] != "in_progress":
                return operation["sealed_http_status"], operation["response"]
            cleanup = getattr(exc, "report_cleanup", None)
            if cleanup is not None:
                self.store.event(connection, cleanup["id"], "report_cleanup", cleanup)
            if (
                published is not None
                and not connection.execute(
                    "SELECT 1 FROM documents WHERE id=?", (published["id"],)
                ).fetchone()
            ):
                cleanup = "removed"
                try:
                    self.reports._path(published["id"]).unlink(missing_ok=True)
                except OSError:
                    cleanup = "retained"
                self.store.event(
                    connection, published["id"], "report_cleanup", {"outcome": cleanup}
                )
            task_id = operation["task_id"]
            if task_id:
                task = self.record(connection, task_id)
                if task["status"] == "running" and operation["path"] == "/api/v1/tasks":
                    self._edge(connection, task, "failed", code=exc.code)
                    connection.execute(
                        "UPDATE tasks SET error=? WHERE id=?",
                        (
                            encode({"code": exc.code, "message": MESSAGE, "retryable": False}),
                            task_id,
                        ),
                    )
                else:
                    self.candidate.event(
                        connection, task_id, "denied", task["status"], task["status"], code=exc.code
                    )
            return self._seal(connection, op, exc.status, failure(exc.code, task_id))
