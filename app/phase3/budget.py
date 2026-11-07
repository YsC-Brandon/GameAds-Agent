"""Exact bounded allocation using observed ROAS; never a prediction or real execution."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from fractions import Fraction

from app.phase3.contracts import BudgetRequest
from app.phase3.store import BusinessError


def cents(value: Decimal | str) -> int:
    return int(Decimal(value) * 100)


def money(value: int) -> str:
    return format(Decimal(value) / 100, ".2f")


def bounded_allocate(total: int, rows: list[dict]) -> dict[str, int]:
    """Water-fill above lower bounds; exact fractions and stable largest remainders."""
    if total < sum(r["lower"] for r in rows) or total > sum(r["upper"] for r in rows):
        raise BusinessError("infeasible_budget_constraints", 422)
    allocated = {r["campaign_id"]: r["lower"] for r in rows}
    remaining = total - sum(allocated.values())
    while remaining:
        active = [r for r in rows if allocated[r["campaign_id"]] < r["upper"]]
        weights = {r["campaign_id"]: r["weight"] for r in active}
        if not sum(weights.values()):
            weights = {r["campaign_id"]: Fraction(1) for r in active}
        denominator = sum(weights.values())
        shares = {key: remaining * value / denominator for key, value in weights.items()}
        capped = [
            r
            for r in active
            if shares[r["campaign_id"]] >= r["upper"] - allocated[r["campaign_id"]]
        ]
        if capped:
            for row in capped:
                key = row["campaign_id"]
                remaining -= row["upper"] - allocated[key]
                allocated[key] = row["upper"]
            continue
        floors = {key: value.numerator // value.denominator for key, value in shares.items()}
        for key, value in floors.items():
            allocated[key] += value
        remaining -= sum(floors.values())
        for key in sorted(shares, key=lambda key: (-(shares[key] - floors[key]), key))[:remaining]:
            allocated[key] += 1
        remaining = 0
    return allocated


def allocate_budget(request: BudgetRequest, summary: dict, call_id: str) -> dict:
    request = BudgetRequest.model_validate(request)
    observed = {row["campaign_id"]: (index, row) for index, row in enumerate(summary["campaigns"])}
    rows = []
    for campaign in sorted(request.campaigns, key=lambda row: row.campaign_id):
        if campaign.campaign_id not in observed:
            raise BusinessError("campaign_missing_in_observation", 422)
        index, result = observed[campaign.campaign_id]
        totals = result["totals"]
        reasons = []
        if not result["coverage"]["complete"]:
            reasons.append("incomplete_date_coverage")
        if result["data_quality_warnings"]:
            reasons.append("data_quality_review_required")
        for field, limit in (
            ("impressions", request.min_impressions),
            ("clicks", request.min_clicks),
            ("conversions", request.min_conversions),
        ):
            if totals[field] < limit:
                reasons.append("insufficient_" + field)
        if result["metrics"]["roas"]["status"] != "ok":
            reasons.append("roas_not_computable")
        current = cents(campaign.current_budget)
        fraction = request.max_change_fraction
        lower = max(
            cents(campaign.min_budget),
            int((current * (1 - fraction)).to_integral_value(rounding=ROUND_CEILING)),
        )
        upper = min(
            cents(campaign.max_budget),
            int((current * (1 + fraction)).to_integral_value(rounding=ROUND_FLOOR)),
        )
        if reasons:
            if not lower <= current <= upper:
                raise BusinessError("frozen_campaign_conflicts_with_bounds", 422)
            lower = upper = current
        if lower > upper:
            raise BusinessError("infeasible_campaign_bounds", 422)
        weight = (
            Fraction(totals["revenue"]) / Fraction(totals["spend"]) if not reasons else Fraction(0)
        )
        rows.append(
            {
                "campaign_id": campaign.campaign_id,
                "current": current,
                "lower": lower,
                "upper": upper,
                "weight": weight,
                "frozen_reasons": reasons,
                "evidence": {"call_id": call_id, "path": ["campaigns", str(index)]},
                "observed_totals": totals,
            }
        )
    original = sum(row["current"] for row in rows)
    total = original if request.total_budget is None else cents(request.total_budget)
    allocations = bounded_allocate(total, rows)
    return {
        "method": "bounded_roas_waterfill_v1",
        "formula": (
            "lower bounds + remaining budget weighted by observed revenue/spend; "
            "cap and redistribute; largest remainder cents, campaign_id ties"
        ),
        "zero_weight_policy": (
            "Equal residual allocation only when all remaining eligible weights are zero."
        ),
        "original_total": money(original),
        "proposed_total": money(total),
        "currency": "USD",
        "allocations": [
            {
                "campaign_id": row["campaign_id"],
                "current_budget": money(row["current"]),
                "proposed_budget": money(allocations[row["campaign_id"]]),
                "delta": money(allocations[row["campaign_id"]] - row["current"]),
                "effective_min": money(row["lower"]),
                "effective_max": money(row["upper"]),
                "weight_numerator": row["weight"].numerator,
                "weight_denominator": row["weight"].denominator,
                "frozen_reasons": row["frozen_reasons"],
                "evidence": row["evidence"],
                "observed_totals": row["observed_totals"],
            }
            for row in rows
        ],
        "limitations": [
            "Synthetic/demo data; not real advertising performance or investment advice.",
            "Historical ROAS is not marginal return; no uplift or future revenue is predicted.",
            "Caller-supplied period budgets are not observed spend; "
            "new zero-budget campaigns cannot scale under relative limits.",
            "Evidence thresholds and the default 20% adjustment cap are "
            "illustrative safety rules, not calibrated policy.",
        ],
    }
