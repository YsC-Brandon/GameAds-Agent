"""Transparent heuristic screening; these rules are not causal diagnoses."""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.core.config import AnomalyThresholds, Settings
from app.schemas import AnomalyRequest, DateRange
from app.services.campaign_service import (
    DEFAULT_DATA_PATH,
    CampaignService,
    coverage,
    period_metadata,
)
from app.tools.analytics_tools import METRIC_DEFINITIONS, _aggregate


def _value(summary: dict, metric: str) -> Decimal | None:
    if summary["metrics"][metric]["status"] != "ok":
        return None
    numerator, denominator, _ = METRIC_DEFINITIONS[metric]
    return Decimal(str(summary["totals"][numerator])) / Decimal(str(summary["totals"][denominator]))


def _evidence(summary: dict, metric: str, thresholds: AnomalyThresholds) -> list[str]:
    reasons = []
    if not summary["coverage"]["complete"]:
        reasons.append("incomplete_date_coverage")
    if summary["data_quality_warnings"]:
        reasons.append("data_quality_review_required")
    if summary["totals"]["impressions"] < thresholds.min_impressions:
        reasons.append("insufficient_impressions")
    if summary["totals"]["clicks"] < thresholds.min_clicks:
        reasons.append("insufficient_clicks")
    if metric == "cpa" and summary["totals"]["conversions"] < thresholds.min_conversions:
        reasons.append("insufficient_conversions")
    if summary["metrics"][metric]["status"] != "ok":
        reasons.append(summary["metrics"][metric]["status"])
    return reasons


def _evaluate(
    rule: str, metric: str, current: dict, baseline: dict, thresholds: AnomalyThresholds
) -> dict:
    current_value = _value(current, metric)
    baseline_value = _value(baseline, metric)
    change = None
    if current_value is not None and baseline_value is not None and baseline_value != 0:
        change = (current_value - baseline_value) / baseline_value
    reasons = ["current:" + reason for reason in _evidence(current, metric, thresholds)]
    if rule != "roas_below_floor":
        reasons.extend("baseline:" + reason for reason in _evidence(baseline, metric, thresholds))
        if baseline_value == 0:
            reasons.append("baseline:zero_baseline_value")
    threshold = {
        "ctr_drop": thresholds.ctr_drop_fraction,
        "cpa_increase": thresholds.cpa_increase_fraction,
        "roas_below_floor": thresholds.roas_floor,
    }[rule]
    triggered = False
    if not reasons:
        boundary = Decimal(str(threshold))
        if rule == "ctr_drop":
            triggered = change < -boundary
        elif rule == "cpa_increase":
            triggered = change > boundary
        else:
            triggered = current_value < boundary
    return {
        "rule": rule,
        "metric": metric,
        "status": "insufficient_evidence"
        if reasons
        else "triggered"
        if triggered
        else "not_triggered",
        "current_value": float(current_value) if current_value is not None else None,
        "baseline_value": float(baseline_value) if baseline_value is not None else None,
        "relative_change": float(change) if change is not None else None,
        "threshold": threshold,
        "comparison": {
            "ctr_drop": "relative_change < -threshold",
            "cpa_increase": "relative_change > threshold",
            "roas_below_floor": "current_value < threshold",
        }[rule],
        "evidence_reasons": reasons,
        "baseline_required": rule != "roas_below_floor",
    }


def detect_campaign_anomalies(
    start_date: str | date,
    end_date: str | date,
    baseline_days: int = 7,
    *,
    data_path: Path = DEFAULT_DATA_PATH,
    thresholds: AnomalyThresholds | None = None,
) -> dict:
    request = AnomalyRequest(start_date=start_date, end_date=end_date, baseline_days=baseline_days)
    thresholds = (
        Settings().anomaly if thresholds is None else AnomalyThresholds.model_validate(thresholds)
    )
    baseline_request = DateRange(
        start_date=request.start_date - timedelta(days=request.baseline_days),
        end_date=request.start_date - timedelta(days=1),
    )
    service = CampaignService(data_path)
    current_rows = service.select(request)
    baseline_rows = service.select(baseline_request)
    # Include baseline-only campaigns so missing current evidence is visible.
    identities = {row.campaign_id: row.campaign_name for row in baseline_rows + current_rows}
    campaigns = []
    for campaign_id, name in sorted(identities.items()):
        summaries = []
        for rows, period in ((current_rows, request), (baseline_rows, baseline_request)):
            selected = [row for row in rows if row.campaign_id == campaign_id]
            summary = _aggregate(selected)
            summary["coverage"] = coverage(selected, period)
            summaries.append(summary)
        current, baseline = summaries
        rules = [
            _evaluate(rule, metric, current, baseline, thresholds)
            for rule, metric in (
                ("ctr_drop", "ctr"),
                ("cpa_increase", "cpa"),
                ("roas_below_floor", "roas"),
            )
        ]
        statuses = {rule["status"] for rule in rules}
        campaigns.append(
            {
                "campaign_id": campaign_id,
                "campaign_name": name,
                "status": "alert"
                if "triggered" in statuses
                else "insufficient_evidence"
                if "insufficient_evidence" in statuses
                else "ok",
                "current": current,
                "baseline": baseline,
                "rules": rules,
            }
        )
    return {
        "status": "ok" if current_rows else "no_data",
        "method": "heuristic_rules",
        "disclaimer": "Heuristic screening only, not statistical significance or causal diagnosis.",
        "current_period": period_metadata(request),
        "baseline_period": period_metadata(baseline_request),
        "thresholds": thresholds.model_dump(),
        "campaigns": campaigns,
    }
