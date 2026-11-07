"""Local business operations; human transitions are never registered as LLM tools."""

import hashlib
import time
from pathlib import Path
from uuid import uuid4

from app.core.config import AnomalyThresholds
from app.core.exceptions import CampaignError
from app.phase3.budget import allocate_budget
from app.phase3.contracts import ApprovalRequest, BudgetRequest, RejectionRequest, SimulationRequest
from app.phase3.store import BusinessError, Store, digest, now, owned_action
from app.services.campaign_service import DEFAULT_DATA_PATH
from app.tools.analytics_tools import summarize_campaign_performance


class ToolRun:
    """Capture only validated arguments, actual outputs, elapsed time, and safe errors."""

    def __init__(self):
        self.traces: list[dict] = []
        self.results: dict[str, dict] = {}

    def call(self, function, arguments: dict, **server_options) -> tuple[str, dict]:
        identity, start = uuid4().hex, time.perf_counter()
        trace = {"call_id": identity, "tool_name": function.__name__, "arguments": arguments}
        try:
            result = function(**arguments, **server_options)
        except Exception as exc:
            code = exc.code if isinstance(exc, (CampaignError, BusinessError)) else "tool_failed"
            trace.update(status="error", error_code=code)
            raise BusinessError(code, 422) from None
        else:
            self.results[identity] = result
            trace.update(
                status="success", error_code=None, result_status=result.get("status", "ok")
            )
            return identity, result
        finally:
            trace["duration_ms"] = (time.perf_counter() - start) * 1000
            self.traces.append(trace)


class BusinessService:
    def __init__(
        self,
        store: Store,
        *,
        data_path: Path = DEFAULT_DATA_PATH,
        thresholds: AnomalyThresholds | None = None,
    ):
        self.store = store
        self.data_path = Path(data_path)
        self.thresholds = thresholds or AnomalyThresholds()

    @owned_action
    def propose_budget_reallocation(self, request: BudgetRequest) -> dict:
        request = BudgetRequest.model_validate(request)
        run = ToolRun()
        try:
            source_before = hashlib.sha256(self.data_path.read_bytes()).hexdigest()
            call_id, summary = run.call(
                summarize_campaign_performance,
                {
                    "start_date": request.start_date.isoformat(),
                    "end_date": request.end_date.isoformat(),
                },
                data_path=self.data_path,
            )
            if hashlib.sha256(self.data_path.read_bytes()).hexdigest() != source_before:
                raise BusinessError("dataset_changed_during_analysis")
            allocation = allocate_budget(request, summary, call_id)
        except (BusinessError, OSError) as exc:
            code = exc.code if isinstance(exc, BusinessError) else "data_source_error"
            self.store.failure("proposal_failed", code, run.traces)
            raise BusinessError(code, 422) from None
        identity = uuid4().hex
        payload = {
            "request": request.model_dump(mode="json"),
            "allocation": allocation,
            "tool_calls": run.traces,
            "tool_results": run.results,
            "source": {"dataset_sha256": source_before, "kind": "synthetic_demo_csv"},
        }
        proposal = {
            "id": identity,
            "status": "pending_approval",
            "version": 1,
            "proposal_sha256": digest(payload),
            "created_at": now(),
            "mode": "simulation",
            "payload": payload,
            "approval": None,
            "simulation": None,
        }
        with self.store.transaction() as connection:
            self.store.save(connection, identity, "proposal", proposal, new=True)
            self.store.event(
                connection,
                identity,
                "proposal_created",
                {
                    "version": 1,
                    "proposal_sha256": proposal["proposal_sha256"],
                },
            )
        return proposal

    def get_budget_proposal(self, identity: str) -> dict:
        return self.store.get(identity, "proposal")

    def _transition(self, identity: str, request, target: str) -> dict:
        failure = None
        with self.store.transaction() as connection:
            self.store.require_unmanaged(connection, identity)
            proposal = self.store.load(connection, identity, "proposal")
            required = "approved" if target == "simulated" else "pending_approval"
            if digest(proposal["payload"]) != proposal["proposal_sha256"]:
                failure = "proposal_integrity_error"
            elif request.proposal_sha256 != proposal["proposal_sha256"]:
                failure = "proposal_digest_mismatch"
            elif request.expected_version != proposal["version"]:
                failure = "stale_proposal_version"
            elif proposal["status"] != required:
                failure = "invalid_proposal_transition"
            if failure:
                self.store.event(
                    connection,
                    identity,
                    "transition_denied",
                    {
                        "requested_status": target,
                        "error_code": failure,
                    },
                )
            else:
                timestamp = now()
                if target == "simulated":
                    if not proposal["approval"] or proposal["approval"]["decision"] != "approved":
                        raise BusinessError("approval_required")
                    proposal["simulation"] = {
                        "id": uuid4().hex,
                        "mode": "simulation",
                        "executed_at": timestamp,
                        "real_execution": False,
                        "dataset_modified": False,
                        "proposal_sha256": proposal["proposal_sha256"],
                        "allocations": proposal["payload"]["allocation"]["allocations"],
                        "message": (
                            "Local simulation only; "
                            "no advertising platform or performance forecast."
                        ),
                    }
                else:
                    proposal["approval"] = {
                        "approver": request.approver,
                        "decided_at": timestamp,
                        "decision": target,
                        "reviewed_version": request.expected_version,
                        "proposal_sha256": request.proposal_sha256,
                        "identity_assurance": "local_self_attested_not_authenticated",
                        "reason": request.reason if target == "rejected" else None,
                    }
                proposal["status"] = target
                proposal["version"] += 1
                self.store.save(connection, identity, "proposal", proposal)
                self.store.event(
                    connection,
                    identity,
                    target,
                    {
                        "version": proposal["version"],
                        "proposal_sha256": proposal["proposal_sha256"],
                        "approval": proposal["approval"],
                    },
                )
        if failure:
            raise BusinessError(failure)
        return proposal

    @owned_action
    def approve_budget_proposal(self, identity: str, request: ApprovalRequest) -> dict:
        return self._transition(identity, ApprovalRequest.model_validate(request), "approved")

    @owned_action
    def reject_budget_proposal(self, identity: str, request: RejectionRequest) -> dict:
        return self._transition(identity, RejectionRequest.model_validate(request), "rejected")

    @owned_action
    def simulate_budget_changes(self, identity: str, request: SimulationRequest) -> dict:
        return self._transition(identity, SimulationRequest.model_validate(request), "simulated")
