"""OpenAI-compatible HTTP transport. Never expose upstream bodies or exceptions."""

import json
from typing import Protocol

import httpx
from pydantic import ValidationError

from app.agent.contracts import ModelMessage
from app.core.config import Settings


class ModelError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ModelProvider(Protocol):
    def complete(
        self, messages: list[dict], tools: list[dict], tool_choice: str | dict = "auto"
    ) -> ModelMessage: ...


def model_configured(settings: Settings) -> bool:
    return bool(settings.llm_model.strip() and settings.llm_api_key.get_secret_value().strip())


class OpenAICompatibleProvider:
    """Bounded non-streaming chat completions; no retries or redirects that can repeat billing."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    def complete(self, messages, tools, tool_choice="auto") -> ModelMessage:
        if not model_configured(self.settings):
            raise ModelError("model_not_configured")
        base = self.settings.llm_base_url
        if base.username or base.password or base.query or base.fragment:
            raise ModelError("invalid_provider_url")
        # Remote cleartext credentials are not allowed. HTTP is for loopback testing only.
        if base.scheme != "https" and base.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ModelError("insecure_provider_url")
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "stream": False,
        }
        # DeepSeek defaults to thinking mode, which rejects named tool_choice.
        # Keep every round non-thinking; other providers must not receive this extension.
        if base.host == "api.deepseek.com":
            payload["thinking"] = {"type": "disabled"}
        if len(json.dumps(payload).encode()) > self.settings.agent_max_context_bytes:
            raise ModelError("context_limit")
        try:
            with httpx.Client(
                timeout=self.settings.llm_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "POST",
                    str(base).rstrip("/") + "/chat/completions",
                    headers={
                        "Authorization": "Bearer " + self.settings.llm_api_key.get_secret_value()
                    },
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        code = {
                            400: "model_invalid_request",
                            401: "model_authentication_failed",
                            402: "model_insufficient_balance",
                            403: "model_authentication_failed",
                            404: "model_endpoint_or_model_not_found",
                            422: "model_invalid_parameters",
                            429: "model_rate_limited",
                            500: "model_upstream_error",
                            502: "model_upstream_error",
                            503: "model_upstream_error",
                            504: "model_upstream_error",
                        }.get(response.status_code, "model_http_error")
                        raise ModelError(code)
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 256_000:
                            raise ModelError("model_response_too_large")
            data = json.loads(body)
            if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
                raise ModelError("model_invalid_response_structure")
            if not data["choices"]:
                raise ModelError("model_empty_choices")
            choice = data["choices"][0]
            if not isinstance(choice, dict):
                raise ModelError("model_invalid_response_structure")
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise ModelError("model_incomplete_response")
            raw = choice.get("message")
            if not isinstance(raw, dict):
                raise ModelError("model_invalid_message")
            calls = raw.get("tool_calls")
            if calls is None:
                calls = []
            if not isinstance(calls, list):
                raise ModelError("model_invalid_tool_calls")
            normalized = []
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                    raise ModelError("model_invalid_tool_calls")
                normalized.append(
                    {
                        "id": call.get("id"),
                        "type": call.get("type"),
                        "function": {
                            "name": call["function"].get("name"),
                            "arguments": call["function"].get("arguments"),
                        },
                    }
                )
            # Ignore vendor-specific metadata, but validate all executable fields.
            return ModelMessage.model_validate(
                {
                    "role": raw.get("role"),
                    "content": raw.get("content"),
                    "tool_calls": normalized,
                }
            )
        except httpx.TimeoutException:
            raise ModelError("model_timeout") from None
        except httpx.RequestError:
            raise ModelError("model_network_error") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ModelError("model_invalid_json") from None
        except ValidationError as exc:
            # Only fixed categories cross the boundary, never rejected values or vendor keys.
            code = "model_invalid_message"
            for error in exc.errors(include_input=False, include_context=False, include_url=False):
                loc = error["loc"]
                if loc and loc[0] == "tool_calls":
                    code = "model_invalid_tool_calls"
                    if len(loc) >= 3 and loc[2] == "id":
                        code = "model_invalid_tool_call_id"
                    elif len(loc) >= 4 and loc[2:4] == ("function", "arguments"):
                        code = "model_invalid_tool_arguments"
                    break
            raise ModelError(code) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("model_protocol_error") from None
