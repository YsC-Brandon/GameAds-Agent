"""Read-only cross-record checks performed before any restart recovery mutation."""

import json
import logging
from datetime import datetime

from app.phase3.contracts import BudgetRequest
from app.phase3.store import BusinessError, digest
from app.phase4.contracts import TaskError, TaskRecord, TaskReportRequest, TaskResponse
from app.task_wire import operation_identity

# Frozen r4 §3.4 plus existing internal/offline rejection codes. This validates
# persisted outcomes; it does not change the response model or stored bytes.
ERROR_STATUSES = {
    404: {"not_found", "report_file_missing"},
    409: {
        "stale_task_version",
        "stale_proposal_version",
        "proposal_digest_mismatch",
        "invalid_task_transition",
        "invalid_proposal_transition",
        "approval_required",
        "invalid_retry_of",
        "operation_interrupted",
        "dataset_changed_during_analysis",
        "report_proposal_scope_mismatch",
        "stored_evidence_integrity_error",
        "proposal_integrity_error",
        "report_file_integrity_error",
        "task_managed_resource",
        "unsafe_reports_directory",
        "unsafe_report_path",
    },
    413: {"result_too_large"},
    422: {
        "infeasible_budget_constraints",
        "infeasible_campaign_bounds",
        "frozen_campaign_conflicts_with_bounds",
        "campaign_missing_in_observation",
        "report_campaign_limit",
        "report_campaign_missing",
        "needs_clarification",
        "unsupported",
        "insufficient_evidence",
    },
    500: {"internal_error", "report_write_failed", "data_source_error", "data_validation_error"},
    502: {
        "agent_failed",
        "tool_failed",
        "agent_report_evidence_mismatch",
        "agent_report_missing_citation",
    },
    503: {
        "model_not_configured",
        "model_unavailable",
        "database_busy",
        "service_busy",
        "schema_upgrade_required",
    },
}


def reject(table, identity, invariant):
    error = BusinessError("stored_evidence_integrity_error")
    error.diagnostic = {"table": table, "identity": identity, "invariant": invariant}
    error.add_note(json.dumps(error.diagnostic, sort_keys=True))
    logging.getLogger(__name__).error("Startup integrity rejection: %s", error.diagnostic)
    raise error


def validate_records(connection):
    """Reject inconsistencies; never infer missing history or rewrite sealed bytes.

    Diagnostics identify records and invariants without copying private payloads.
    Safe in-progress reservations are retained for the existing recovery protocol.
    """
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        reject("database", None, "sqlite_integrity")
    foreign = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign:
        reject(foreign[0], foreign[1], "foreign_keys")
    tasks = {r["id"]: dict(r) for r in connection.execute("SELECT * FROM tasks")}
    documents = {}
    for row in connection.execute("SELECT * FROM documents"):
        try:
            value = json.loads(row["payload"])
            valid = isinstance(value, dict) and digest(value) == row["sha256"]
        except (TypeError, ValueError):
            valid = False
        if not valid or row["kind"] not in {"proposal", "report"}:
            reject("documents", row["id"], "payload_digest_kind")
        if value.get("id", row["id"]) != row["id"]:
            reject("documents", row["id"], "document_identity")
        try:
            if row["kind"] == "proposal":
                if digest(value["payload"]) != value["proposal_sha256"]:
                    raise ValueError
                expected_version = {
                    "pending_approval": 1,
                    "approved": 2,
                    "rejected": 2,
                    "simulated": 3,
                }[value["status"]]
                if value["version"] != expected_version:
                    raise ValueError
                approval, simulation = value["approval"], value["simulation"]
                if expected_version == 1:
                    if approval is not None or simulation is not None:
                        raise ValueError
                elif (
                    not isinstance(approval, dict)
                    or approval["decision"]
                    != ("approved" if value["status"] == "simulated" else value["status"])
                    or approval["proposal_sha256"] != value["proposal_sha256"]
                    or approval["reviewed_version"] != 1
                ):
                    raise ValueError
                if value["status"] == "simulated":
                    if (
                        not isinstance(simulation, dict)
                        or simulation["proposal_sha256"] != value["proposal_sha256"]
                        or simulation["mode"] != "simulation"
                        or simulation["real_execution"] is not False
                        or simulation["dataset_modified"] is not False
                    ):
                        raise ValueError
                elif simulation is not None:
                    raise ValueError
            elif (
                value["status"] != "generated"
                or value["filename"] != row["id"] + ".md"
                or digest(value["tool_results"]) != value["evidence_sha256"]
            ):
                raise ValueError
            if row["kind"] == "report" or "payload" in value:
                source = value["source"] if row["kind"] == "report" else value["payload"]["source"]
                checksum = source["dataset_sha256"]
                if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
                    raise ValueError
        except (ValueError, TypeError, KeyError):
            reject("documents", row["id"], "business_record")
        documents[row["id"]] = (row["kind"], value)
    for row in connection.execute("SELECT * FROM events"):
        try:
            if not isinstance(json.loads(row["payload"]), dict):
                raise ValueError
        except (ValueError, TypeError):
            reject("events", row["id"], "event_payload")
    relations = {identity: {} for identity in tasks}
    for row in connection.execute("SELECT * FROM task_documents"):
        kind, value = documents[row["document_id"]]
        task_kind = tasks[row["task_id"]]["kind"]
        if (
            kind != row["role"]
            or task_kind == "analysis"
            or (kind == "proposal" and task_kind != "budget_review")
        ):
            reject("task_documents", row["task_id"], "role_kind")
        relations[row["task_id"]][kind] = value
    operations = [dict(r) for r in connection.execute("SELECT * FROM operations")]
    success_snapshots = {identity: [] for identity in tasks}
    creates = {identity: [] for identity in tasks}
    active = {identity: [] for identity in tasks}
    for row in operations:
        identity = row["operation_id"]
        try:
            operation_identity(row["method"], row["path"].encode(), row["key"].encode())
        except (ValueError, UnicodeError, BusinessError):
            reject("operations", identity, "wire_identity")
        task_id = row["task_id"]
        if row["path"] == "/api/v1/tasks":
            if task_id:
                creates[task_id].append(row)
                if row["fingerprint"] != tasks[task_id]["request_fingerprint"]:
                    reject("operations", identity, "create_fingerprint")
        elif not task_id or row["path"].split("/")[-2] != task_id:
            reject("operations", identity, "continuation_task_identity")
        if row["phase"] == "in_progress":
            if row["response"] is not None or row["sealed_http_status"] is not None:
                reject("operations", identity, "active_seal")
            if task_id:
                active[task_id].append(row)
            continue
        try:
            body = json.loads(row["response"])
            status = row["sealed_http_status"]
            if not isinstance(body, dict) or not isinstance(status, int):
                raise ValueError
            if 200 <= status < 300:
                TaskResponse.model_validate(body)
                data = body.get("data")
                if (
                    body.get("status") != "ok"
                    or not isinstance(data, dict)
                    or data.get("id") != task_id
                ):
                    raise ValueError
                if data.get("active_operation_id") is not None:
                    raise ValueError
                if data.get("version", 0) > tasks[task_id]["version"]:
                    raise ValueError
                if (
                    data["kind"] != tasks[task_id]["kind"]
                    or data["retry_of"] != tasks[task_id]["retry_of"]
                    or data["created_at"] != tasks[task_id]["created_at"]
                ):
                    raise ValueError
                for role in ("proposal", "report"):
                    if data[role + "_id"] is not None and data[role + "_id"] != relations[
                        task_id
                    ].get(role, {}).get("id"):
                        raise ValueError
                if row["path"] == "/api/v1/tasks":
                    expected = (
                        "awaiting_approval" if data["kind"] == "budget_review" else "completed"
                    )
                    if status != 201 or data["status"] != expected:
                        raise ValueError
                elif (
                    status != 200
                    or data["status"]
                    != {
                        "approve": "approved",
                        "reject": "rejected",
                        "simulate": "simulated",
                        "report": "completed",
                    }[row["path"].rsplit("/", 1)[1]]
                ):
                    raise ValueError
                success_snapshots[task_id].append(data)
            elif body.get("status") != "error" or body.get("task_id") != task_id:
                raise ValueError
            else:
                TaskError.model_validate(body)
                code = body["code"]
                if body["retryable"] is not False or code not in ERROR_STATUSES.get(status, set()):
                    raise ValueError
                if (code == "operation_interrupted") != (row["phase"] == "interrupted"):
                    raise ValueError
                if task_id is None:
                    if row["path"] != "/api/v1/tasks" or code not in {
                        "invalid_retry_of",
                        "operation_interrupted",
                        "internal_error",
                    }:
                        raise ValueError
                elif row["path"] == "/api/v1/tasks":
                    task = tasks[task_id]
                    if code in {"needs_clarification", "unsupported", "insufficient_evidence"}:
                        if task["kind"] != "analysis" or task["status"] != code:
                            raise ValueError
                    elif code == "operation_interrupted":
                        if task["status"] != "interrupted":
                            raise ValueError
                    elif task["status"] != "failed":
                        raise ValueError
                    if task["status"] in {"failed", "interrupted"}:
                        error = json.loads(task["error"])
                        if error["code"] != code or error["retryable"] is not False:
                            raise ValueError
                elif code in {
                    "needs_clarification",
                    "unsupported",
                    "insufficient_evidence",
                    "invalid_retry_of",
                }:
                    raise ValueError
                if task_id is not None and code != "operation_interrupted":
                    evidence = connection.execute(
                        "SELECT 1 FROM task_events WHERE task_id=? AND code=? "
                        "AND action IN ('denied','failed')",
                        (task_id, code),
                    ).fetchone()
                    if evidence is None and code not in {
                        "needs_clarification",
                        "unsupported",
                        "insufficient_evidence",
                    }:
                        raise ValueError
            if row["phase"] == "interrupted" and (
                status != 409 or body.get("code") != "operation_interrupted"
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            reject("operations", identity, "sealed_response")
    edges = {
        "submitted": {"running", "interrupted"},
        "running": {
            "awaiting_approval",
            "completed",
            "failed",
            "needs_clarification",
            "unsupported",
            "insufficient_evidence",
            "interrupted",
        },
        "awaiting_approval": {"approved", "rejected"},
        "approved": {"simulated"},
        "simulated": {"completed"},
    }
    for identity, task in tasks.items():
        try:
            for field in ("created_at", "updated_at"):
                if datetime.fromisoformat(task[field]).utcoffset() is None:
                    raise ValueError
            if task["kind"] != "analysis":
                request = json.loads(task["validated_request"])
                model = BudgetRequest if task["kind"] == "budget_review" else TaskReportRequest
                if model.model_validate(request).model_dump(mode="json") != request:
                    raise ValueError
                if (
                    digest({"kind": task["kind"], "request": request, "retry_of": task["retry_of"]})
                    != task["request_fingerprint"]
                ):
                    raise ValueError
            TaskRecord.model_validate(
                {
                    key: task[key]
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
                }
                | {
                    "proposal_id": relations[identity].get("proposal", {}).get("id"),
                    "report_id": relations[identity].get("report", {}).get("id"),
                    "agent_result": json.loads(task["agent_result"])
                    if task["agent_result"]
                    else None,
                    "error": json.loads(task["error"]) if task["error"] else None,
                    "active_operation_id": active[identity][0]["operation_id"]
                    if active[identity]
                    else None,
                }
            )
        except (ValueError, TypeError):
            reject("tasks", identity, "record_shape")
        if task["status"] in {"failed", "interrupted"}:
            if task["error"] is None or json.loads(task["error"])["retryable"] is not False:
                reject("tasks", identity, "failure_result")
        if task["kind"] == "analysis":
            if task["status"] in {
                "completed",
                "needs_clarification",
                "unsupported",
                "insufficient_evidence",
            } and (
                task["agent_result"] is None
                or json.loads(task["agent_result"])["status"] != task["status"]
            ):
                reject("tasks", identity, "analysis_result")
        elif task["agent_result"] is not None:
            reject("tasks", identity, "unexpected_agent_result")
        if len(creates[identity]) != 1 or len(active[identity]) > 1:
            reject("tasks", identity, "unique_create_and_occupant")
        if task["status"] in {"submitted", "running"} and (
            not active[identity] or active[identity][0]["path"] != "/api/v1/tasks"
        ):
            reject("tasks", identity, "unfinished_create_occupant")
        if (
            active[identity]
            and active[identity][0]["path"] == "/api/v1/tasks"
            and task["status"] not in {"submitted", "running"}
        ):
            reject("tasks", identity, "active_create_status")
        prior = task["retry_of"]
        visited = {identity}
        while prior:
            if (
                prior in visited
                or tasks[prior]["kind"] != task["kind"]
                or tasks[prior]["status"]
                not in {
                    "failed",
                    "needs_clarification",
                    "unsupported",
                    "insufficient_evidence",
                    "interrupted",
                }
            ):
                reject("tasks", identity, "retry_cycle_or_kind")
            visited.add(prior)
            prior = tasks[prior]["retry_of"]
        history = connection.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY seq", (identity,)
        ).fetchall()
        state, version = None, 0
        states_at_version = {}
        for seq, event in enumerate(history, 1):
            target = event["to_status"]
            if event["seq"] != seq or event["from_status"] != state:
                reject("task_events", identity, "sequence_or_chain")
            if state is None:
                valid = target == "submitted" and event["action"] == "submitted"
            elif target == state:
                valid = event["action"] in {"denied", "report_available"}
            else:
                valid = target in edges.get(state, set()) and event["action"] == target
            if not valid:
                reject("task_events", identity, "state_transition")
            if target != state:
                version += 1
                states_at_version[version] = target
            state = target
        if state != task["status"] or version != task["version"]:
            reject("tasks", identity, "history_state_version")
        for sealed in success_snapshots[identity]:
            if states_at_version.get(sealed["version"]) != sealed["status"]:
                reject("operations", creates[identity][0]["operation_id"], "sealed_history")
        linked = relations[identity]
        if task["kind"] == "budget_review" and task["status"] in {
            "awaiting_approval",
            "approved",
            "rejected",
            "simulated",
            "completed",
        }:
            proposal = linked.get("proposal")
            expected = (
                "pending_approval" if task["status"] == "awaiting_approval" else task["status"]
            )
            if expected == "completed":
                expected = "simulated"
            if not proposal or proposal.get("status") != expected:
                reject("task_documents", identity, "proposal_state")
            if task["source_sha256"] != proposal["payload"]["source"]["dataset_sha256"]:
                reject("task_documents", identity, "proposal_source")
            if proposal["payload"].get("request") != json.loads(task["validated_request"]):
                reject("task_documents", identity, "proposal_request")
        if "report" in linked:
            report = linked["report"]
            if task["source_sha256"] != report["source"]["dataset_sha256"]:
                reject("task_documents", identity, "report_source")
            if task["kind"] == "budget_review" and (
                report["proposal"] is None
                or report["proposal"]["id"] != linked.get("proposal", {}).get("id")
            ):
                reject("task_documents", identity, "report_proposal")
        if (
            task["status"] == "completed"
            and task["kind"] in {"report", "budget_review"}
            and "report" not in linked
        ):
            reject("task_documents", identity, "completed_report_missing")
        for field in ("validated_request", "agent_result", "error"):
            if task[field] is not None:
                try:
                    if not isinstance(json.loads(task[field]), dict):
                        raise ValueError
                except (ValueError, TypeError):
                    reject("tasks", identity, field + "_json")
