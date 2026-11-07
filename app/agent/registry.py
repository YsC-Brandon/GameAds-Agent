"""Allowlisted tools: model arguments never choose data paths or supply raw facts."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel, Field, ValidationError

from app.agent.contracts import DetailRequest, ToolCall, ToolTrace
from app.core.config import Settings
from app.core.exceptions import CampaignError
from app.schemas import AnomalyRequest, CampaignQuery, ComparisonRequest, Contract, DateRange
from app.services.campaign_service import DEFAULT_DATA_PATH
from app.tools.analytics_tools import compare_campaigns, summarize_campaign_performance
from app.tools.anomaly_tools import detect_campaign_anomalies
from app.tools.campaign_tools import get_campaign_detail, list_campaigns, query_campaigns


class EmptyRequest(Contract):
    """No model-controlled parameters for campaign enumeration."""


class QueryRequest(CampaignQuery):
    limit: int = Field(default=100, ge=1, le=100, strict=True)


def function_schema(name: str, description: str, schema: type[BaseModel]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema.model_json_schema(),
        },
    }


@dataclass(frozen=True)
class ToolDefinition:
    schema: type[BaseModel]
    function: Callable[..., dict]
    description: str


class ToolRegistry:
    """One request's read-only capabilities and server-owned dataset configuration."""

    def __init__(self, settings: Settings, data_path: Path = DEFAULT_DATA_PATH):
        self.settings = settings
        self.data_path = data_path
        self.tools = {
            "list_campaigns": ToolDefinition(
                EmptyRequest,
                list_campaigns,
                "List available campaign IDs and observed date coverage.",
            ),
            "query_campaigns": ToolDefinition(
                QueryRequest,
                query_campaigns,
                "Read campaign-day records with inclusive dates. Paginated; not a full aggregate.",
            ),
            "get_campaign_detail": ToolDefinition(
                DetailRequest, get_campaign_detail, "Read one campaign's daily records; paginated."
            ),
            "summarize_campaign_performance": ToolDefinition(
                DateRange,
                summarize_campaign_performance,
                "Compute all five metrics from full local data, overall and per campaign.",
            ),
            "compare_campaigns": ToolDefinition(
                ComparisonRequest,
                compare_campaigns,
                "Rank campaigns by a computed metric. Use asc for lowest and desc for highest.",
            ),
            "detect_campaign_anomalies": ToolDefinition(
                AnomalyRequest,
                detect_campaign_anomalies,
                "Screen CTR decline, CPA rise and low ROAS with evidence gates; not causal.",
            ),
        }

    def schemas(self) -> list[dict]:
        return [
            function_schema(name, tool.description, tool.schema)
            for name, tool in self.tools.items()
        ]

    def execute(
        self, call: ToolCall, *, allowed: list[str], period: DateRange | None
    ) -> tuple[ToolTrace, dict]:
        started = perf_counter()
        arguments = None
        code = None
        result = None
        try:
            if call.function.name not in self.tools or call.function.name not in allowed:
                code = "tool_not_allowed"
            else:
                tool = self.tools[call.function.name]
                request = tool.schema.model_validate_json(call.function.arguments)
                arguments = request.model_dump(mode="json")
                if isinstance(request, DateRange) and (
                    period is None
                    or request.start_date != period.start_date
                    or request.end_date != period.end_date
                ):
                    code = "tool_date_mismatch"
                else:
                    kwargs = request.model_dump()
                    kwargs["data_path"] = self.data_path
                    if call.function.name == "detect_campaign_anomalies":
                        kwargs["thresholds"] = self.settings.anomaly
                    result = tool.function(**kwargs)
                    if len(json.dumps(result, allow_nan=False).encode()) > 150_000:
                        code = "tool_result_too_large"
        except ValidationError:
            code = "invalid_tool_arguments"
        except CampaignError as exc:
            code = exc.code
        except Exception:
            # No exception text: it may contain paths, dataset values, or credentials.
            code = "tool_execution_failed"
        if code:
            result = {"status": "error", "code": code}
        trace = ToolTrace(
            call_id=call.id,
            tool_name=call.function.name,
            arguments=arguments,
            status="error" if code else "success",
            error_code=code,
            result_status=result.get("status"),
            duration_ms=(perf_counter() - started) * 1000,
        )
        return trace, result
