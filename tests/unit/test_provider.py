"""Real HTTP adapter exercised with HTTPX transport stubs, not external credentials."""

import json

import httpx
import pytest

from app.agent.provider import ModelError, OpenAICompatibleProvider
from app.core.config import Settings


def settings(**kwargs):
    return Settings(
        _env_file=None, llm_model="mock-model", llm_api_key="synthetic-secret", **kwargs
    )


def response(message=None):
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": message
                or {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "list_campaigns",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            }
        ]
    }


def test_wire_protocol_and_explicit_endpoint():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-secret"
        payload = json.loads(request.content)
        assert payload["messages"][-1]["tool_call_id"] == "prior"
        assert payload["model"] == "mock-model"
        assert payload["tools"] == [{"type": "function"}]
        assert payload["stream"] is False
        assert "thinking" not in payload
        return httpx.Response(200, json=response())

    provider = OpenAICompatibleProvider(settings(), transport=httpx.MockTransport(handler))
    result = provider.complete(
        [{"role": "tool", "tool_call_id": "prior", "content": "{}"}],
        [{"type": "function"}],
    )
    assert result.tool_calls[0].function.name == "list_campaigns"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "url,deepseek",
    [
        ("https://api.deepseek.com", True),
        ("https://api.deepseek.com/v1", True),
        ("https://api.deepseek.com.evil.invalid/v1", False),
        ("https://other.invalid/v1", False),
    ],
)
@pytest.mark.parametrize(
    "choice", [{"type": "function", "function": {"name": "submit_plan"}}, "auto"]
)
def test_deepseek_non_thinking_in_plan_and_followup_rounds(url, deepseek, choice):
    def handler(request):
        payload = json.loads(request.content)
        assert payload["tool_choice"] == choice
        if deepseek:
            assert payload["thinking"] == {"type": "disabled"}
        else:
            assert "thinking" not in payload
        return httpx.Response(200, json=response())

    provider = OpenAICompatibleProvider(
        settings(llm_base_url=url), transport=httpx.MockTransport(handler)
    )
    assert provider.complete([], [], choice).tool_calls


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "model_invalid_request"),
        (401, "model_authentication_failed"),
        (402, "model_insufficient_balance"),
        (403, "model_authentication_failed"),
        (404, "model_endpoint_or_model_not_found"),
        (422, "model_invalid_parameters"),
        (429, "model_rate_limited"),
        (500, "model_upstream_error"),
        (502, "model_upstream_error"),
        (503, "model_upstream_error"),
        (504, "model_upstream_error"),
        (302, "model_http_error"),
    ],
)
def test_http_failures_redacted_and_not_retried(status, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, text="secret-path-and-key", headers={"location": "https://evil.invalid"}
        )

    provider = OpenAICompatibleProvider(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError) as exc:
        provider.complete([], [])
    assert str(exc.value) == code
    assert len(calls) == 1


@pytest.mark.parametrize(
    "exception,code",
    [
        (httpx.ReadTimeout("private"), "model_timeout"),
        (httpx.ConnectError("private"), "model_network_error"),
    ],
)
def test_transport_errors_redacted(exception, code):
    def handler(request):
        raise exception

    with pytest.raises(ModelError, match=code):
        OpenAICompatibleProvider(settings(), transport=httpx.MockTransport(handler)).complete(
            [], []
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"choices": ["invalid"]},
        {"choices": [{"finish_reason": "stop", "message": []}]},
        {"choices": []},
        {"choices": [{"finish_reason": "length"}]},
        response({"role": "user", "content": "bad"}),
        response({"role": "assistant", "tool_calls": [{"id": "x"}]}),
    ],
)
def test_invalid_protocol_is_explicit(payload):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ModelError):
        OpenAICompatibleProvider(settings(), transport=transport).complete([], [])


def test_oversized_response_rejected():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 256001))
    with pytest.raises(ModelError, match="model_response_too_large"):
        OpenAICompatibleProvider(settings(), transport=transport).complete([], [])


@pytest.mark.parametrize("identity", ["call.0:plan/1", "opaque+token=1", "x" * 256])
def test_opaque_call_ids_and_vendor_metadata_preserved(identity):
    payload = response()
    raw = payload["choices"][0]["message"]
    raw["reasoning_content"] = None
    raw["tool_calls"][0]["id"] = identity
    raw["tool_calls"][0]["index"] = 0
    raw["tool_calls"][0]["function"]["vendor_metadata"] = "ignored"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    result = OpenAICompatibleProvider(settings(), transport=transport).complete([], [])
    assert result.tool_calls[0].id == identity
    assert result.wire()["tool_calls"][0]["id"] == identity
    assert "index" not in result.wire()["tool_calls"][0]


@pytest.mark.parametrize(
    "payload,code",
    [
        ({}, "model_invalid_response_structure"),
        ({"choices": []}, "model_empty_choices"),
        ({"choices": [{"finish_reason": "stop", "message": []}]}, "model_invalid_message"),
        (response({"role": "user"}), "model_invalid_message"),
        (response({"role": "assistant", "tool_calls": {}}), "model_invalid_tool_calls"),
        (response({"role": "assistant", "tool_calls": [None]}), "model_invalid_tool_calls"),
    ],
)
def test_protocol_error_categories_are_fixed_and_safe(payload, code):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ModelError) as exc:
        OpenAICompatibleProvider(settings(), transport=transport).complete([], [])
    assert str(exc.value) == code


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("id", "", "model_invalid_tool_call_id"),
        ("id", "x" * 257, "model_invalid_tool_call_id"),
        ("id", "private\nsecret", "model_invalid_tool_call_id"),
        ("id", "private secret", "model_invalid_tool_call_id"),
        ("arguments", {}, "model_invalid_tool_arguments"),
        ("arguments", "private" * 5000, "model_invalid_tool_arguments"),
        ("type", "shell", "model_invalid_tool_calls"),
        ("name", "invalid.name", "model_invalid_tool_calls"),
    ],
)
def test_invalid_executable_fields_are_still_rejected(field, value, code):
    payload = response()
    call = payload["choices"][0]["message"]["tool_calls"][0]
    target = call["function"] if field in {"name", "arguments"} else call
    target[field] = value
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ModelError) as exc:
        OpenAICompatibleProvider(settings(), transport=transport).complete([], [])
    assert str(exc.value) == code


def test_invalid_json_diagnostic_never_exposes_response_body():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"private-upstream-body")
    )
    with pytest.raises(ModelError) as exc:
        OpenAICompatibleProvider(settings(), transport=transport).complete([], [])
    assert str(exc.value) == "model_invalid_json"


def test_missing_key_never_sends_request():
    def forbidden(request):
        pytest.fail("No network call is allowed without model configuration.")

    provider = OpenAICompatibleProvider(
        Settings(_env_file=None), transport=httpx.MockTransport(forbidden)
    )
    with pytest.raises(ModelError, match="model_not_configured"):
        provider.complete([], [])


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://name:password@example.com/v1",
        "https://example.com/v1?key=secret",
    ],
)
def test_unsafe_provider_url_rejected(url):
    with pytest.raises(ModelError):
        OpenAICompatibleProvider(settings(llm_base_url=url)).complete([], [])


def test_context_size_limit():
    with pytest.raises(ModelError, match="context_limit"):
        OpenAICompatibleProvider(settings(agent_max_context_bytes=10000)).complete(
            [{"role": "user", "content": "x" * 10001}],
            [],
        )
