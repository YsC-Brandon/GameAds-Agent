"""Validated local campaign queries with deterministic order and pagination."""

from datetime import date
from pathlib import Path

from app.schemas import CampaignQuery
from app.services.campaign_service import (
    DEFAULT_DATA_PATH,
    CampaignService,
    period_metadata,
    quality_warnings,
)


def list_campaigns(*, data_path: Path = DEFAULT_DATA_PATH) -> dict:
    service = CampaignService(data_path)
    campaigns = service.list_campaigns()
    return {
        "status": "ok" if campaigns else "no_data",
        "campaign_count": len(campaigns),
        "campaigns": campaigns,
        "data_quality_warnings": quality_warnings(service.records),
    }


def query_campaigns(
    start_date: str | date,
    end_date: str | date,
    campaign_ids: list[str] | None = None,
    *,
    offset: int = 0,
    limit: int = 100,
    data_path: Path = DEFAULT_DATA_PATH,
) -> dict:
    request = CampaignQuery(
        start_date=start_date,
        end_date=end_date,
        campaign_ids=campaign_ids,
        offset=offset,
        limit=limit,
    )
    records = CampaignService(data_path).select(request)
    page = records[request.offset : request.offset + request.limit]
    return {
        "status": "ok" if records else "no_data",
        "period": period_metadata(request),
        "currency": "USD",
        "total_records": len(records),
        "returned_records": len(page),
        "offset": request.offset,
        "limit": request.limit,
        "has_more": request.offset + len(page) < len(records),
        "records": [record.model_dump(mode="json") for record in page],
        "data_quality_warnings": quality_warnings(records),
    }


def get_campaign_detail(
    campaign_id: str,
    start_date: str | date,
    end_date: str | date,
    *,
    offset: int = 0,
    limit: int = 100,
    data_path: Path = DEFAULT_DATA_PATH,
) -> dict:
    return query_campaigns(
        start_date, end_date, [campaign_id], offset=offset, limit=limit, data_path=data_path
    )
