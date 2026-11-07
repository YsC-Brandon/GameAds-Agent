"""Public request boundary with no conversation reuse and no persistent sensitive logs."""

import re
from pathlib import Path
from uuid import uuid4

from app.agent.contracts import AgentResponse, ChatRequest
from app.agent.graph import AgentGraph
from app.agent.provider import ModelProvider, OpenAICompatibleProvider
from app.agent.registry import ToolRegistry
from app.core.config import Settings
from app.services.campaign_service import DEFAULT_DATA_PATH


def redact(value, secret: str):
    """Defense in depth for reflected credentials; no raw request/provider logs are written."""
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
        return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact(key, secret): redact(item, secret) for key, item in value.items()}
    return value


class AgentService:
    def __init__(
        self,
        settings: Settings,
        *,
        provider: ModelProvider | None = None,
        data_path: Path = DEFAULT_DATA_PATH,
    ):
        self.settings = settings
        self.provider = provider or OpenAICompatibleProvider(settings)
        self.data_path = data_path

    def run(self, request: ChatRequest) -> AgentResponse:
        request = ChatRequest.model_validate(request)
        # Do not send an accidentally pasted configured key to the provider in user content.
        secret = self.settings.llm_api_key.get_secret_value()
        safe_request = ChatRequest(query=redact(request.query, secret))
        try:
            response = AgentGraph(self.provider, ToolRegistry(self.settings, self.data_path)).run(
                safe_request
            )
            return AgentResponse.model_validate(redact(response.model_dump(mode="json"), secret))
        except Exception:
            # Last-resort guard. Expected model/tool failures are handled inside the graph.
            return AgentResponse(
                request_id=str(uuid4()),
                status="failed",
                errors=["agent_internal_error"],
                final_answer="任务未完成（agent_internal_error）；没有生成成功分析结论。",
            )
