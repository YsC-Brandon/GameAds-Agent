"""Local task HTTP client. Never opens SQLite or automatically starts a service."""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.phase4.contracts import (
    CREATE_ADAPTER,
    TaskApproval,
    TaskPublish,
    TaskRejection,
    TaskSimulation,
)
from app.phase4.service import failure
from app.task_wire import operation_identity


def identity(value):
    if re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise argparse.ArgumentTypeError("Expected a lowercase hexadecimal ID")
    return value


def base_url(value):
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid local service URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise argparse.ArgumentTypeError("Only a local service URL is supported")
    return value.rstrip("/")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--base-url", type=base_url, default="http://127.0.0.1:8000")
    result.add_argument("--timeout", type=float, default=30)
    commands = result.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--request-json", required=True)
    create.add_argument("--idempotency-key", required=True)
    create.add_argument("--allow-model-call", action="store_true")
    listing = commands.add_parser("list")
    listing.add_argument("--kind", choices=["analysis", "budget_review", "report"])
    listing.add_argument("--status")
    for name in ("get", "events", "reports", "report-get"):
        command = commands.add_parser(name)
        command.add_argument("task_id" if name != "report-get" else "report_id", type=identity)
        if name in {"events", "reports"}:
            command.add_argument("--limit", type=int, default=50)
            command.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    lookup = commands.add_parser("operation-get")
    lookup.add_argument("--method", choices=["POST"], required=True)
    lookup.add_argument("--path", required=True)
    lookup.add_argument("--idempotency-key", required=True)
    for name in ("approve", "reject", "simulate", "report"):
        command = commands.add_parser(name)
        command.add_argument("task_id", type=identity)
        command.add_argument("--expected-task-version", type=int, required=True)
        command.add_argument("--idempotency-key", required=True)
        if name in {"approve", "reject", "simulate"}:
            command.add_argument("--expected-version", type=int, required=True)
            command.add_argument("--proposal-sha256", required=True)
        if name in {"approve", "reject"}:
            command.add_argument("--approver", required=True)
            command.add_argument("--confirm", action="store_true", required=True)
        if name == "reject":
            command.add_argument("--reason", required=True)
        if name == "simulate":
            command.add_argument("--mode", choices=["simulation"], default="simulation")
    return result


def request_options(args):
    values = vars(args)
    command = args.command
    method, path, body, params = "GET", "/api/v1/tasks", None, {}
    if command == "create":
        if args.request_json == "-":
            raw = sys.stdin.buffer.read(64 * 1024 + 1)
        else:
            file = Path(args.request_json)
            if not file.is_absolute():
                raise ValueError("Absolute input file required")
            with file.open("rb") as stream:
                raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("Input too large")
        model = CREATE_ADAPTER.validate_json(raw)
        if model.kind == "analysis" and not args.allow_model_call:
            raise ValueError("Explicit model intent is required")
        body, method = model.model_dump(mode="json"), "POST"
    elif command == "operation-get":
        operation_identity(
            args.method, args.path.encode("ascii"), args.idempotency_key.encode("ascii")
        )
        path += "/operations/lookup"
        params = {"method": args.method, "path": args.path}
    elif command == "report-get":
        path = "/api/v1/reports/" + args.report_id
    elif command == "list":
        params = {
            k: values[k] for k in ("kind", "status", "limit", "offset") if values[k] is not None
        }
    else:
        path += "/" + args.task_id
        if command in {"events", "reports"}:
            path += "/" + command
            params = {"limit": args.limit, "offset": args.offset}
        elif command != "get":
            path += "/" + command
            method = "POST"
            models = {
                "approve": TaskApproval,
                "reject": TaskRejection,
                "simulate": TaskSimulation,
                "report": TaskPublish,
            }
            fields = models[command].model_fields
            body = {name: values[name] for name in fields if name in values}
            if command in {"approve", "reject"}:
                body["confirmed"] = args.confirm
            body = models[command].model_validate(body).model_dump(mode="json")
    if method == "POST":
        operation_identity(method, path.encode("ascii"), args.idempotency_key.encode("ascii"))
    if "limit" in params and (not 1 <= params["limit"] <= 100 or params["offset"] < 0):
        raise ValueError("Invalid pagination")
    if not 0 < args.timeout <= 3600:
        raise ValueError("Invalid timeout")
    return method, path, body, params


def exit_code(status, payload, command):
    if status in {400, 413, 415} or payload.get("code") == "invalid_request":
        return 2
    if status in {403, 404, 409}:
        return 4
    if status == 503:
        return 5
    if payload.get("code") in {"needs_clarification", "unsupported", "insufficient_evidence"}:
        return 3
    if status >= 400 or 300 <= status < 400:
        return 1
    if command in {"create", "approve", "reject", "simulate", "report"}:
        state = payload.get("data", {}).get("status")
        if state in {"needs_clarification", "unsupported", "insufficient_evidence"}:
            return 3
        if state not in {"completed", "awaiting_approval", "approved", "rejected", "simulated"}:
            return 1
    return 0


def main():
    args = parser().parse_args()
    try:
        method, path, body, params = request_options(args)
    except (ValueError, OSError, UnicodeError, ValidationError, KeyError):
        print(json.dumps(failure("invalid_request")))
        print("Invalid local input; inspect command help.", file=sys.stderr)
        return 2
    headers = {"Idempotency-Key": args.idempotency_key} if hasattr(args, "idempotency_key") else {}
    try:
        with httpx.Client(
            base_url=args.base_url, trust_env=False, follow_redirects=False, timeout=args.timeout
        ) as client:
            options = {"params": params, "headers": headers}
            if body is not None:
                options["json"] = body
            response = client.request(method, path, **options)
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Invalid response")
    except (httpx.HTTPError, ValueError):
        print(json.dumps(failure("service_unavailable")))
        print(
            "Service unavailable or outcome unknown; query the original operation before retrying.",
            file=sys.stderr,
        )
        return 5
    print(json.dumps(payload, ensure_ascii=True))
    result = exit_code(response.status_code, payload, args.command)
    if result:
        print(
            "Action did not complete; inspect JSON and query current state before retrying.",
            file=sys.stderr,
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
