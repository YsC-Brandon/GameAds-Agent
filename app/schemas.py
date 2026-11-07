"""Validated data and request contracts, independent of an LLM or web API."""

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


def parse_date(value: object) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Use a date in YYYY-MM-DD format.")
    return date.fromisoformat(value)


ISODate = Annotated[date, BeforeValidator(parse_date)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Count = Annotated[int, Field(ge=0, le=10**12)]
Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2, allow_inf_nan=False)]
MetricName = Literal["ctr", "cvr", "cpc", "cpa", "roas"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class CampaignRecord(Contract):
    date: ISODate
    campaign_id: Label
    campaign_name: Label
    platform: Label
    game_genre: Label
    impressions: Count
    clicks: Count
    conversions: Count
    spend: Money
    revenue: Money
    currency: Literal["USD"]

    @field_validator("impressions", "clicks", "conversions", "spend", "revenue", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Boolean values are not advertising amounts or counts.")
        return value


class DateRange(Contract):
    start_date: ISODate
    end_date: ISODate

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date.")
        return self


class CampaignQuery(DateRange):
    campaign_ids: list[Label] | None = Field(default=None, min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=100, ge=1, le=1000, strict=True)

    @field_validator("campaign_ids")
    @classmethod
    def unique_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("campaign_ids must not contain duplicates.")
        return value


class ComparisonRequest(DateRange):
    metric: MetricName
    top_n: int = Field(default=5, ge=1, le=100, strict=True)
    order: Literal["asc", "desc"] = "desc"


class AnomalyRequest(DateRange):
    baseline_days: int = Field(default=7, ge=1, le=365, strict=True)

    @model_validator(mode="after")
    def valid_baseline(self) -> Self:
        if self.start_date.toordinal() <= self.baseline_days:
            raise ValueError("The baseline would fall before the earliest supported date.")
        return self
