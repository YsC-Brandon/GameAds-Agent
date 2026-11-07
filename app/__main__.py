"""Local deterministic tools only: no model, server, or advertising account access."""

import argparse
import json
from pathlib import Path

from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import CampaignError, DataValidationError
from app.services.campaign_service import DEFAULT_DATA_PATH
from app.tools.analytics_tools import compare_campaigns, summarize_campaign_performance
from app.tools.anomaly_tools import detect_campaign_anomalies
from app.tools.campaign_tools import get_campaign_detail, list_campaigns, query_campaigns


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-path", type=Path, default=DEFAULT_DATA_PATH)
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="List available campaigns.")
    for name in ("query", "detail", "metrics", "compare", "anomalies"):
        command = commands.add_parser(name)
        command.add_argument("--start-date", required=True)
        command.add_argument("--end-date", required=True)
        if name in {"query", "detail"}:
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=100)
            if name == "query":
                command.add_argument("--campaign-id", action="append", dest="campaign_ids")
            else:
                command.add_argument("--campaign-id", required=True)
        if name == "compare":
            command.add_argument("--metric", required=True)
            command.add_argument("--top-n", type=int, default=5)
            command.add_argument("--order", default="desc")
        if name == "anomalies":
            command.add_argument("--baseline-days", type=int, default=7)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        settings = Settings()
    except ValidationError:
        print("Configuration check failed. Check GAMEADS_* values against .env.example.")
        return 1
    if args.command is None:
        print("GameAds-Agent: configuration loaded successfully.")
        print("This command makes no model request and starts no web server.")
        print("Deterministic subcommands are available; use python -m app.cli for Agent tasks.")
        return 0
    options = vars(args).copy()
    command = options.pop("command")
    functions = {
        "list": list_campaigns,
        "query": query_campaigns,
        "detail": get_campaign_detail,
        "metrics": summarize_campaign_performance,
        "compare": compare_campaigns,
        "anomalies": detect_campaign_anomalies,
    }
    if command == "anomalies":
        options["thresholds"] = settings.anomaly
    try:
        result = functions[command](**options)
    except ValidationError as exc:
        result = {
            "status": "error",
            "code": "invalid_arguments",
            "issues": [
                {"field": list(error["loc"]), "code": error["type"]}
                for error in exc.errors(
                    include_input=False, include_context=False, include_url=False
                )
            ],
        }
    except CampaignError as exc:
        result = {"status": "error", "code": exc.code, "message": str(exc)}
        if isinstance(exc, DataValidationError):
            result["issues"] = exc.issues
    else:
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
