"""Real SQLite transactions enforce review, versioning and exactly-once simulation."""

import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from app.phase3.contracts import ApprovalRequest, RejectionRequest, SimulationRequest
from app.phase3.service import BusinessService
from app.phase3.store import BusinessError, Store


def review(proposal, **extra):
    return {
        "approver": "local-reviewer",
        "confirmed": True,
        "expected_version": proposal["version"],
        "proposal_sha256": proposal["proposal_sha256"],
    } | extra


def simulation(proposal, **extra):
    return SimulationRequest(
        expected_version=proposal["version"], proposal_sha256=proposal["proposal_sha256"], **extra
    )


def test_full_approval_simulation_persists_without_mutating_csv(business, budget_request):
    before = hashlib.sha256(business.data_path.read_bytes()).hexdigest()
    proposal = business.propose_budget_reallocation(budget_request)
    approved = business.approve_budget_proposal(proposal["id"], ApprovalRequest(**review(proposal)))
    restarted = BusinessService(Store(business.store.path), data_path=business.data_path)
    assert restarted.get_budget_proposal(proposal["id"]) == approved
    result = restarted.simulate_budget_changes(proposal["id"], simulation(approved))
    assert result["status"] == "simulated" and result["version"] == 3
    assert result["simulation"]["real_execution"] is False
    assert result["simulation"]["dataset_modified"] is False
    assert result["simulation"]["allocations"] == proposal["payload"]["allocation"]["allocations"]
    assert hashlib.sha256(business.data_path.read_bytes()).hexdigest() == before
    assert [e["action"] for e in business.store.events(proposal["id"], "proposal")] == [
        "proposal_created",
        "approved",
        "simulated",
    ]


@pytest.mark.parametrize("status", ["pending_approval", "rejected", "simulated"])
def test_invalid_simulation_transitions(business, budget_request, status):
    proposal = business.propose_budget_reallocation(budget_request)
    if status == "rejected":
        proposal = business.reject_budget_proposal(
            proposal["id"], RejectionRequest(**review(proposal, reason="Needs more evidence"))
        )
    elif status == "simulated":
        proposal = business.approve_budget_proposal(
            proposal["id"], ApprovalRequest(**review(proposal))
        )
        proposal = business.simulate_budget_changes(proposal["id"], simulation(proposal))
    with pytest.raises(BusinessError, match="invalid_proposal_transition"):
        business.simulate_budget_changes(proposal["id"], simulation(proposal))
    assert business.store.events(proposal["id"], "proposal")[-1]["action"] == "transition_denied"


@pytest.mark.parametrize("kind", ["stale", "digest", "repeat", "rejected"])
def test_stale_or_conflicting_approval_rejected(business, budget_request, kind):
    proposal = business.propose_budget_reallocation(budget_request)
    args = review(proposal)
    if kind == "stale":
        args["expected_version"] = 9
    elif kind == "digest":
        args["proposal_sha256"] = "0" * 64
    elif kind == "repeat":
        updated = business.approve_budget_proposal(proposal["id"], ApprovalRequest(**args))
        args["expected_version"] = updated["version"]
    else:
        updated = business.reject_budget_proposal(
            proposal["id"], RejectionRequest(**args, reason="Rejected")
        )
        args["expected_version"] = updated["version"]
    with pytest.raises(BusinessError):
        business.approve_budget_proposal(proposal["id"], ApprovalRequest(**args))


@pytest.mark.parametrize("confirmed", [False, 1, "true", None])
def test_confirmation_must_be_explicit_true(confirmed):
    with pytest.raises(ValidationError):
        ApprovalRequest(
            approver="reviewer", expected_version=1, proposal_sha256="a" * 64, confirmed=confirmed
        )


def test_cannot_select_real_execution():
    with pytest.raises(ValidationError):
        SimulationRequest(expected_version=2, proposal_sha256="a" * 64, mode="real")


def test_concurrent_approvals_allow_one_winner(business, budget_request):
    proposal = business.propose_budget_reallocation(budget_request)

    def approve(_):
        try:
            return business.approve_budget_proposal(
                proposal["id"], ApprovalRequest(**review(proposal))
            )["status"]
        except BusinessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(approve, range(2)))
    assert sorted(outcomes) == ["approved", "stale_proposal_version"]


def test_concurrent_simulation_runs_once(business, budget_request):
    proposal = business.propose_budget_reallocation(budget_request)
    approved = business.approve_budget_proposal(proposal["id"], ApprovalRequest(**review(proposal)))

    def simulate(_):
        try:
            return business.simulate_budget_changes(proposal["id"], simulation(approved))["status"]
        except BusinessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(simulate, range(2)))
    assert sorted(outcomes) == ["simulated", "stale_proposal_version"]
    assert [e["action"] for e in business.store.events(proposal["id"], "proposal")].count(
        "simulated"
    ) == 1


def test_storage_tampering_detected(business, budget_request):
    proposal = business.propose_budget_reallocation(budget_request)
    with business.store.transaction() as connection:
        connection.execute("UPDATE documents SET payload='{}' WHERE id=?", (proposal["id"],))
    with pytest.raises(BusinessError, match="integrity"):
        business.get_budget_proposal(proposal["id"])
