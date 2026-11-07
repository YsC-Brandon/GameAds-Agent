"""Independent exact expectations, constraints, evidence freezes and cent conservation."""

from decimal import Decimal
from fractions import Fraction
from random import Random

import pytest
from pydantic import ValidationError

from app.phase3.budget import bounded_allocate
from app.phase3.contracts import BudgetRequest
from app.phase3.store import BusinessError, digest


def changed(request, **updates):
    return BudgetRequest.model_validate(request.model_dump(mode="json") | updates)


def test_independent_expected_default_allocation(business, budget_request):
    proposal = business.propose_budget_reallocation(budget_request)
    allocation = proposal["payload"]["allocation"]
    # 80 floors each; remaining 40 at ROAS weights 2:1 -> 26.67 and 13.33.
    assert [r["proposed_budget"] for r in allocation["allocations"]] == ["106.67", "93.33"]
    assert allocation["original_total"] == allocation["proposed_total"] == "200.00"
    assert proposal["status"] == "pending_approval"
    assert proposal["proposal_sha256"] == digest(proposal["payload"])
    for row in allocation["allocations"]:
        evidence = row["evidence"]
        result = proposal["payload"]["tool_results"][evidence["call_id"]]
        assert result["campaigns"][int(evidence["path"][1])]["totals"] == row["observed_totals"]
    assert proposal["payload"]["tool_calls"][0]["duration_ms"] >= 0


def test_explicit_total_and_cap_redistribution(business, budget_request):
    request = changed(budget_request, total_budget="220.00")
    rows = business.propose_budget_reallocation(request)["payload"]["allocation"]["allocations"]
    assert [r["proposed_budget"] for r in rows] == ["120.00", "100.00"]


def test_no_relative_cap_and_stable_rounding(business, budget_request):
    request = changed(budget_request, max_change_fraction="1")
    rows = business.propose_budget_reallocation(request)["payload"]["allocation"]["allocations"]
    assert [r["proposed_budget"] for r in rows] == ["133.33", "66.67"]


@pytest.mark.parametrize("total", ["0", "159.99", "240.01", "10000"])
def test_infeasible_total_is_not_balanced_with_plug(business, budget_request, total):
    with pytest.raises(BusinessError, match="infeasible_budget_constraints"):
        business.propose_budget_reallocation(changed(budget_request, total_budget=total))
    with business.store.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
        event = connection.execute("SELECT action FROM events").fetchone()[0]
        assert event == "proposal_failed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("current_budget", "-1"),
        ("min_budget", "-0.01"),
        ("max_budget", "1.001"),
        ("current_budget", "NaN"),
        ("current_budget", "Infinity"),
        ("current_budget", True),
    ],
)
def test_invalid_money_rejected(budget_request, field, value):
    data = budget_request.model_dump(mode="json")
    data["campaigns"][0][field] = value
    with pytest.raises(ValidationError):
        BudgetRequest.model_validate(data)


@pytest.mark.parametrize(
    "updates",
    [
        {"currency": "EUR"},
        {"max_change_fraction": "1.1"},
        {"max_change_fraction": True},
        {"min_clicks": True},
        {"start_date": "2026-10-02"},
        {"mode": "real"},
        {"budget_period": {"start_date": "2026-10-01", "end_date": "2026-10-02"}},
    ],
)
def test_invalid_request_scope(budget_request, updates):
    with pytest.raises(ValidationError):
        changed(budget_request, **updates)


def test_duplicate_campaigns_and_inverted_bounds(budget_request):
    data = budget_request.model_dump(mode="json")
    data["campaigns"][1]["campaign_id"] = "A"
    with pytest.raises(ValidationError):
        BudgetRequest.model_validate(data)
    data["campaigns"] = [data["campaigns"][0] | {"min_budget": "201"}]
    with pytest.raises(ValidationError):
        BudgetRequest.model_validate(data)


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"conversions": 0}, "insufficient_conversions"),
        ({"impressions": 500}, "insufficient_impressions"),
        ({"clicks": 10, "conversions": 1}, "insufficient_clicks"),
        ({"spend": "0.00"}, "roas_not_computable"),
        ({"clicks": 2000}, "data_quality_review_required"),
    ],
)
def test_weak_evidence_freezes_budget(
    business, budget_request, record, csv_factory, override, reason
):
    business.data_path = csv_factory(
        [{**record, **override}, {**record, "campaign_id": "B", "campaign_name": "B"}], "weak.csv"
    )
    rows = business.propose_budget_reallocation(budget_request)["payload"]["allocation"][
        "allocations"
    ]
    assert rows[0]["proposed_budget"] == "100.00"
    assert reason in rows[0]["frozen_reasons"]


def test_missing_dates_freeze_and_missing_campaign_reject(business, budget_request):
    proposal = business.propose_budget_reallocation(changed(budget_request, end_date="2026-10-02"))
    assert all(
        "incomplete_date_coverage" in r["frozen_reasons"]
        for r in proposal["payload"]["allocation"]["allocations"]
    )
    data = budget_request.model_dump(mode="json")
    data["campaigns"][0]["campaign_id"] = "missing"
    with pytest.raises(BusinessError, match="campaign_missing"):
        business.propose_budget_reallocation(BudgetRequest.model_validate(data))


def test_frozen_outside_bounds_is_infeasible(business, budget_request):
    data = budget_request.model_dump(mode="json")
    data["min_conversions"] = 100
    data["campaigns"][0]["min_budget"] = "110"
    with pytest.raises(BusinessError, match="frozen_campaign"):
        business.propose_budget_reallocation(BudgetRequest.model_validate(data))


def test_zero_total_zero_weights_and_zero_baseline():
    rows = [
        {"campaign_id": key, "lower": 0, "upper": 100, "weight": Fraction(0)}
        for key in ("A", "B", "C")
    ]
    assert bounded_allocate(0, rows) == {"A": 0, "B": 0, "C": 0}
    assert bounded_allocate(100, rows) == {"A": 34, "B": 33, "C": 33}


def test_exact_cents_property_across_300_seeded_cases():
    random = Random(31)
    for _ in range(300):
        rows = []
        for i in range(random.randint(1, 20)):
            lower = random.randrange(1000)
            rows.append(
                {
                    "campaign_id": str(i),
                    "lower": lower,
                    "upper": lower + random.randrange(1000),
                    "weight": Fraction(random.randrange(100), random.randint(1, 100)),
                }
            )
        total = random.randint(sum(r["lower"] for r in rows), sum(r["upper"] for r in rows))
        result = bounded_allocate(total, rows)
        assert sum(result.values()) == total
        assert all(r["lower"] <= result[r["campaign_id"]] <= r["upper"] for r in rows)
        assert result == bounded_allocate(total, list(reversed(rows)))


def test_input_budget_is_not_observed_spend(business, budget_request):
    proposal = business.propose_budget_reallocation(budget_request)
    row = proposal["payload"]["allocation"]["allocations"][0]
    assert Decimal(row["current_budget"]) == 100
    assert Decimal(row["observed_totals"]["spend"]) == 50
