"""One independent natural-language task; may call a configured model API."""

import argparse
import json

from pydantic import ValidationError

from app.agent.contracts import ChatRequest
from app.agent.service import AgentService
from app.core.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="Task with explicit dates; this can incur provider charges.")
    args = parser.parse_args()
    try:
        result = AgentService(Settings()).run(ChatRequest(query=args.query))
    except ValidationError:
        print(json.dumps({"status": "error", "code": "invalid_request_or_configuration"}))
        return 1
    print(result.model_dump_json(indent=2))
    return 0 if result.status in {"completed", "needs_clarification", "unsupported"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
