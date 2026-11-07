"""Readable, structured errors for deterministic data tools."""


class CampaignError(ValueError):
    """Base class for expected local data/tool failures."""

    code = "campaign_error"


class DataSourceError(CampaignError):
    code = "data_source_error"


class DataValidationError(CampaignError):
    code = "data_validation_error"

    def __init__(self, issues: list[dict]) -> None:
        self.issues = issues
        super().__init__(f"Dataset rejected: {len(issues)} validation issue(s).")


class CampaignNotFoundError(CampaignError):
    code = "campaign_not_found"

    def __init__(self, campaign_ids: list[str]) -> None:
        self.campaign_ids = sorted(campaign_ids)
        super().__init__("Unknown campaign ID(s): " + ", ".join(self.campaign_ids))
