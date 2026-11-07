"""Aggregate counts and exact decimal money before calculating any ratios."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from app.core.exceptions import DataValidationError
from app.schemas import CampaignRecord, ComparisonRequest, DateRange
from app.services.campaign_service import (
    DEFAULT_DATA_PATH,
    MAX_CSV_ROWS,
    CampaignService,
    coverage,
    period_metadata,
    quality_warnings,
    validate_records,
)

METRIC_DEFINITIONS = {
    "ctr": ("clicks", "impressions", "ratio"),
    "cvr": ("conversions", "clicks", "ratio"),
    "cpc": ("spend", "clicks", "USD/click"),
    "cpa": ("spend", "conversions", "USD/conversion"),
    "roas": ("revenue", "spend", "multiple"),
}


def _aggregate(records: list[CampaignRecord]) -> dict:
    totals = {
        field: sum(
            (getattr(row, field) for row in records),
            Decimal(0) if field in {"spend", "revenue"} else 0,
        )
        for field in ("impressions", "clicks", "conversions", "spend", "revenue")
    }
    warnings = quality_warnings(records)
    bad_clicks = any(row.clicks > row.impressions for row in records)
    metrics = {}
    for name, (numerator, denominator, unit) in METRIC_DEFINITIONS.items():
        if not records:
            status = "no_data"
        elif bad_clicks and name in {"ctr", "cvr", "cpc"}:
            status = "invalid_data"
        elif totals[denominator] == 0:
            status = "zero_denominator"
        else:
            status = "ok"
        metrics[name] = {
            "value": float(Decimal(totals[numerator]) / Decimal(totals[denominator]))
            if status == "ok"
            else None,
            "status": status,
            "definition": f"{numerator} / {denominator}",
            "unit": unit,
        }
    return {
        "status": "no_data" if not records else "data_quality_warning" if warnings else "ok",
        "currency": "USD",
        "row_count": len(records),
        "campaign_count": len({row.campaign_id for row in records}),
        "observed_period": {
            "start_date": min(row.date for row in records).isoformat() if records else None,
            "end_date": max(row.date for row in records).isoformat() if records else None,
        },
        "totals": {
            key: format(value, ".2f") if isinstance(value, Decimal) else value
            for key, value in totals.items()
        },
        "metrics": metrics,
        "data_quality_warnings": warnings,
    }


def calculate_campaign_metrics(data: list[dict | CampaignRecord]) -> dict:
    if not isinstance(data, list) or len(data) > MAX_CSV_ROWS:
        raise DataValidationError([{"code": "expected_record_list_up_to_100000_rows"}])
    return _aggregate(validate_records(data))


def campaign_summaries(records: list[CampaignRecord], request: DateRange) -> list[dict]:
    groups = defaultdict(list)
    for record in records:
        groups[record.campaign_id].append(record)
    results = []
    for campaign_id, group in sorted(groups.items()):
        result = _aggregate(group)
        result.update(
            campaign_id=campaign_id,
            campaign_name=group[0].campaign_name,
            coverage=coverage(group, request),
        )
        if not result["coverage"]["complete"]:
            result["status"] = "data_quality_warning"
            result["data_quality_warnings"].append(
                {"code": "incomplete_date_coverage", "campaign_id": campaign_id}
            )
        results.append(result)
    return results


def summarize_campaign_performance(
    start_date: str | date, end_date: str | date, *, data_path: Path = DEFAULT_DATA_PATH
) -> dict:
    request = DateRange(start_date=start_date, end_date=end_date)
    records = CampaignService(data_path).select(request)
    campaigns = campaign_summaries(records, request)
    overall = _aggregate(records)
    overall["coverage"] = coverage(records, request)
    incomplete_campaigns = [
        row["campaign_id"] for row in campaigns if not row["coverage"]["complete"]
    ]
    if incomplete_campaigns:
        overall["status"] = "data_quality_warning"
        overall["data_quality_warnings"].append(
            {"code": "incomplete_campaign_coverage", "campaign_ids": incomplete_campaigns}
        )
    if not overall["coverage"]["complete"]:
        overall["data_quality_warnings"].append({"code": "incomplete_date_coverage"})
        if records:
            overall["status"] = "data_quality_warning"
    return {
        "status": overall["status"],
        "period": period_metadata(request),
        "overall": overall,
        "campaigns": campaigns,
    }


def compare_campaigns(
    start_date: str | date,
    end_date: str | date,
    metric: str,
    top_n: int = 5,
    order: str = "desc",
    *,
    data_path: Path = DEFAULT_DATA_PATH,
) -> dict:
    request = ComparisonRequest(
        start_date=start_date, end_date=end_date, metric=metric, top_n=top_n, order=order
    )
    records = CampaignService(data_path).select(request)
    candidates = campaign_summaries(records, request)
    eligible = []
    excluded = []
    for result in candidates:
        value = result["metrics"][request.metric]
        if value["status"] == "ok":
            eligible.append(result)
        else:
            excluded.append({"campaign_id": result["campaign_id"], "reason": value["status"]})
    sign = 1 if request.order == "asc" else -1
    numerator, denominator, unit = METRIC_DEFINITIONS[request.metric]
    eligible.sort(
        key=lambda item: (
            sign * Fraction(item["totals"][numerator]) / Fraction(item["totals"][denominator]),
            item["campaign_id"],
        )
    )
    return {
        "status": "no_data" if not records else "no_computable_values" if not eligible else "ok",
        "period": period_metadata(request),
        "metric": request.metric,
        "metric_definition": f"{numerator} / {denominator}",
        "unit": unit,
        "order": request.order,
        "top_n": request.top_n,
        "sample_rows": len(records),
        "eligible_campaign_count": len(eligible),
        "rankings": eligible[: request.top_n],
        "excluded_campaigns": excluded,
        "data_quality_warnings": [
            warning for row in candidates for warning in row["data_quality_warnings"]
        ],
    }
