"""Explicit period budgets, approval attestations, and safe report requests."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, StringConstraints, model_validator

from app.schemas import AnomalyRequest, Contract, DateRange, Money


def reject_bool(value):
    if isinstance(value, bool):
        raise ValueError("Boolean amounts are not allowed.")
    return value


Amount = Annotated[Money, BeforeValidator(reject_bool)]
Text = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^\x00-\x1f]+$"
    ),
]
Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CampaignBudget(Contract):
    campaign_id: Text
    current_budget: Amount
    min_budget: Amount
    max_budget: Amount

    @model_validator(mode="after")
    def ordered_bounds(self) -> Self:
        if self.min_budget > self.max_budget:
            raise ValueError("min_budget must not exceed max_budget.")
        return self


class BudgetRequest(DateRange):
    """start/end are observation dates; budgets are totals for budget_period."""

    budget_period: DateRange
    currency: Literal["USD"] = "USD"
    campaigns: list[CampaignBudget] = Field(min_length=1, max_length=100)
    total_budget: Amount | None = None
    max_change_fraction: Annotated[Decimal, BeforeValidator(reject_bool)] = Field(
        default=Decimal("0.20"), ge=0, le=1, allow_inf_nan=False, decimal_places=4
    )
    min_impressions: int = Field(default=1000, ge=1, strict=True)
    min_clicks: int = Field(default=30, ge=1, strict=True)
    min_conversions: int = Field(default=5, ge=1, strict=True)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        ids = [row.campaign_id for row in self.campaigns]
        if len(ids) != len(set(ids)):
            raise ValueError("Campaign IDs must be unique.")
        if self.budget_period.start_date <= self.end_date:
            raise ValueError("The budget period must follow the observation period.")
        return self


class ApprovalRequest(Contract):
    approver: Text
    expected_version: int = Field(ge=1, strict=True)
    proposal_sha256: Digest
    confirmed: Literal[True]

    @model_validator(mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if isinstance(value, dict) and value.get("confirmed") is not True:
            raise ValueError("Explicit human confirmation is required.")
        return value


class RejectionRequest(ApprovalRequest):
    reason: Text


class SimulationRequest(Contract):
    expected_version: int = Field(ge=1, strict=True)
    proposal_sha256: Digest
    mode: Literal["simulation"] = "simulation"


class ReportRequest(AnomalyRequest):
    campaign_ids: list[Text] | None = Field(default=None, min_length=1, max_length=100)
    proposal_id: Identity | None = None
    output_format: Literal["markdown"] = "markdown"

    @model_validator(mode="after")
    def unique_campaigns(self) -> Self:
        if self.campaign_ids is not None and len(set(self.campaign_ids)) != len(self.campaign_ids):
            raise ValueError("Campaign IDs must be unique.")
        return self


class BusinessResponse(Contract):
    status: str
    data: dict
