"""Deterministic demo provider for local acceptance; never contacts a model."""

import json

from app.agent.contracts import ModelMessage


class OfflineProvider:
    def __init__(self):
        self.requests = 0

    def complete(self, messages, tools, tool_choice="auto"):
        step = self.requests
        self.requests += 1
        if step == 0:
            name, arguments = (
                "submit_plan",
                {
                    "task_type": "summary",
                    "date_range": {"start_date": "2026-10-01", "end_date": "2026-10-01"},
                    "steps": ["summarize_campaign_performance"],
                },
            )
        elif step == 1:
            name, arguments = (
                "summarize_campaign_performance",
                {"start_date": "2026-10-01", "end_date": "2026-10-01"},
            )
        else:
            call_id = next(
                call["id"]
                for message in reversed(messages)
                if message.get("role") == "assistant"
                for call in message.get("tool_calls", [])
                if call.get("function", {}).get("name") == "summarize_campaign_performance"
            )
            name, arguments = (
                "finish_analysis",
                {
                    "references": [{"call_id": call_id, "path": []}],
                    "interpretations": [{"call_id": call_id, "kind": "performance"}],
                },
            )
        return ModelMessage(
            tool_calls=[
                {
                    "id": "offline-" + str(step),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        )
