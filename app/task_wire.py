"""Storage-free 4A-r4 wire prevalidation; does not implement task operations.

Alias recognition is ONLY for rejecting noncanonical request targets. Accepted
POST paths are compared to the untouched raw bytes, never to decoded aliases.
"""

import posixpath
import re
from urllib.parse import unquote, unquote_to_bytes

from starlette.responses import JSONResponse

POST_PATH = re.compile(rb"/api/v1/tasks(?:/[0-9a-f]{32}/(?:approve|reject|simulate|report))?")
LOOKUP = b"/api/v1/tasks/operations/lookup"
KEY = re.compile(rb"[A-Za-z0-9._:-]{1,128}")
BAD_ESCAPE = re.compile(rb"%(?![0-9a-fA-F]{2})")
ENTRY = re.compile(r"/api/v1/tasks(?:/[^/]+/(?:approve|reject|simulate|report))?")


def _recognize(raw: bytes) -> str:
    """Recognize aliases for rejection, NOT for routing or identity acceptance."""
    value = raw.decode("ascii", errors="replace")
    while "%" in value:
        decoded = unquote(value, errors="replace")
        if decoded == value:
            break
        value = decoded
    value = "/" + value.replace("\\", "/").lstrip("/")
    return posixpath.normpath(value).lower().rstrip("/")


def is_task_path(raw: bytes) -> bool:
    path = _recognize(raw)
    return path == "/api/v1/tasks" or path.startswith("/api/v1/tasks/")


def operation_identity(method: str, path: bytes, key: bytes) -> tuple[str, str, str]:
    """Shared exact validator; no trim, case-fold, decoding or path repair."""
    if method != "POST" or POST_PATH.fullmatch(path) is None or KEY.fullmatch(key) is None:
        raise ValueError("invalid_request")
    return method, path.decode("ascii"), key.decode("ascii")


def _decode(value: bytes) -> str:
    if BAD_ESCAPE.search(value):
        raise ValueError("invalid_request")
    return unquote_to_bytes(value.replace(b"+", b" ")).decode("utf-8", errors="strict")


def parse_identity(scope: dict) -> tuple[str, str, str]:
    """Prevalidate either a canonical POST or a canonical GET lookup."""
    keys = [v for k, v in scope.get("headers", []) if k.lower() == b"idempotency-key"]
    # The supported transport preserves duplicates and value bytes before h11
    # trims them. Direct in-process validator callers retain the ASGI contract.
    if "gameads.raw_keys" in scope:
        keys = scope["gameads.raw_keys"]
    if len(keys) != 1:
        raise ValueError("invalid_request")
    raw_path, query = scope.get("raw_path", b""), scope.get("query_string", b"")
    if scope["method"] == "POST":
        if query:
            raise ValueError("invalid_request")
        return operation_identity("POST", raw_path, keys[0])
    if scope["method"] != "GET" or raw_path != LOOKUP:
        raise ValueError("invalid_request")
    pairs = []
    for field in query.split(b"&"):
        name, separator, value = field.partition(b"=")
        if not separator:
            raise ValueError("invalid_request")
        pairs.append((_decode(name), _decode(value)))
    if len(pairs) != 2 or sorted(name for name, _ in pairs) != ["method", "path"]:
        raise ValueError("invalid_request")
    values = dict(pairs)  # Only after decoded duplicate/name checks.
    return operation_identity(values["method"], values["path"].encode("ascii"), keys[0])


class TaskWireBoundary:
    """Run AFTER LocalBoundary and BEFORE routing/storage. No task API is added."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = scope.get("raw_path", b"")
        recognized = _recognize(raw)
        lookup = recognized == LOOKUP.decode("ascii")
        post = scope["method"] == "POST" and ENTRY.fullmatch(recognized) is not None
        if not post and not (scope["method"] == "GET" and lookup):
            await self.app(scope, receive, send)
            return

        async def no_store(message):
            if message["type"] == "http.response.start" and lookup:
                message = dict(message)
                message["headers"] = [
                    (k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        try:
            identity = parse_identity(scope)
        except (ValueError, UnicodeError):
            await JSONResponse(
                status_code=422,
                content={
                    "status": "error",
                    "code": "invalid_request",
                    "message": "Request could not be completed. Check inputs/configuration.",
                    "task_id": None,
                    "retryable": False,
                },
            )(scope, receive, no_store)
            return
        scope = dict(scope)
        scope["gameads.operation_identity"] = identity
        await self.app(scope, receive, no_store)
