"""Strict CSV loading and inclusive filtering; invalid rows are never dropped."""

import csv
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from app.core.config import PROJECT_ROOT
from app.core.exceptions import CampaignNotFoundError, DataSourceError, DataValidationError
from app.schemas import CampaignQuery, CampaignRecord, DateRange

DEFAULT_DATA_PATH = PROJECT_ROOT / "data" / "sample_campaigns.csv"
# A wheel has no repository-level data directory. Keep source-checkout behavior
# unchanged; install only the audited synthetic dataset as package data.
if not (PROJECT_ROOT / "pyproject.toml").is_file():
    DEFAULT_DATA_PATH = Path(__file__).resolve().parents[1] / "resources" / "sample_campaigns.csv"
CSV_COLUMNS = tuple(CampaignRecord.model_fields)
MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_CSV_ROWS = 100_000


def validate_records(data: list[dict | CampaignRecord]) -> list[CampaignRecord]:
    records = []
    issues = []
    seen = set()
    metadata = {}
    for position, raw in enumerate(data, start=1):
        try:
            record = CampaignRecord.model_validate(raw)
        except ValidationError as exc:
            for error in exc.errors(include_input=False, include_context=False, include_url=False):
                issues.append(
                    {"record": position, "field": list(error["loc"]), "code": error["type"]}
                )
            continue
        key = (record.date, record.campaign_id)
        if key in seen:
            issues.append({"record": position, "code": "duplicate_campaign_date"})
        seen.add(key)
        identity = (record.campaign_name, record.platform, record.game_genre, record.currency)
        if record.campaign_id in metadata and metadata[record.campaign_id] != identity:
            issues.append({"record": position, "code": "inconsistent_campaign_metadata"})
        metadata[record.campaign_id] = identity
        records.append(record)
    if issues:
        raise DataValidationError(issues)
    return records


def quality_warnings(records: list[CampaignRecord]) -> list[dict]:
    warnings = []
    for record in records:
        codes = []
        if record.clicks > record.impressions:
            codes.append("clicks_exceed_impressions")
        if record.conversions > record.clicks:
            codes.append("conversions_exceed_clicks_attribution_review")
        for code in codes:
            warnings.append(
                {"code": code, "campaign_id": record.campaign_id, "date": record.date.isoformat()}
            )
    return warnings


def load_campaign_data(path: Path = DEFAULT_DATA_PATH) -> list[CampaignRecord]:
    path = Path(path)
    try:
        if path.suffix.lower() != ".csv" or path.stat().st_size > MAX_CSV_BYTES:
            raise DataSourceError("Expected a local CSV file no larger than 10 MiB.")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            header = next(reader, [])
            if len(header) != len(CSV_COLUMNS) or set(header) != set(CSV_COLUMNS):
                raise DataValidationError([{"record": 0, "code": "invalid_csv_header"}])
            rows = []
            for position, values in enumerate(reader, start=1):
                if position > MAX_CSV_ROWS:
                    raise DataSourceError("CSV exceeds the 100,000-record local data limit.")
                if len(values) != len(header):
                    raise DataValidationError(
                        [{"record": position, "code": "invalid_column_count"}]
                    )
                rows.append(dict(zip(header, values, strict=True)))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise DataSourceError(
            "Cannot read the local UTF-8 CSV. Check its path and format."
        ) from exc
    return validate_records(rows)


class CampaignService:
    def __init__(self, data_path: Path = DEFAULT_DATA_PATH) -> None:
        self.records = load_campaign_data(data_path)
        self.frame = pd.DataFrame([row.model_dump() for row in self.records], columns=CSV_COLUMNS)

    def select(self, request: DateRange) -> list[CampaignRecord]:
        frame = self.frame
        campaign_ids = request.campaign_ids if isinstance(request, CampaignQuery) else None
        if campaign_ids is not None:
            unknown = sorted(set(campaign_ids) - set(frame["campaign_id"]))
            if unknown:
                raise CampaignNotFoundError(unknown)
            frame = frame.loc[frame["campaign_id"].isin(campaign_ids)]
        frame = frame.loc[frame["date"].between(request.start_date, request.end_date)]
        frame = frame.sort_values(["date", "campaign_id"], kind="stable")
        return [self.records[index] for index in frame.index]

    def list_campaigns(self) -> list[dict]:
        result = []
        for campaign_id, group in self.frame.groupby("campaign_id", sort=True):
            row = group.iloc[0]
            result.append(
                {
                    "campaign_id": campaign_id,
                    "campaign_name": row["campaign_name"],
                    "platform": row["platform"],
                    "game_genre": row["game_genre"],
                    "currency": row["currency"],
                    "first_date": min(group["date"]).isoformat(),
                    "last_date": max(group["date"]).isoformat(),
                    "row_count": len(group),
                }
            )
        return result


def period_metadata(request: DateRange) -> dict:
    return {
        **request.model_dump(mode="json", include={"start_date", "end_date"}),
        "inclusive": True,
    }


def coverage(records: list[CampaignRecord], request: DateRange) -> dict:
    observed = len({row.date for row in records})
    expected = (request.end_date - request.start_date).days + 1
    return {"observed_days": observed, "expected_days": expected, "complete": observed == expected}
