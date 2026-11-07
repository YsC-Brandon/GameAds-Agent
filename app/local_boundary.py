"""Early, route-independent guard for the trusted single-user loopback service.

The server must be launched with proxy_headers=False: ASGI middleware cannot
reconstruct the real peer after an outer proxy-headers middleware rewrites it.
"""

import ipaddress
import re
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from app.task_wire import is_task_path

_HOST = re.compile(r"(localhost|127\.0\.0\.1|\[::1\])(?::([0-9]+))?", re.IGNORECASE)
_ORIGIN = re.compile(r"https?://[^\s/?#@,]+", re.IGNORECASE | re.ASCII)


def _host(value: bytes) -> tuple[str, int | None] | None:
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    match = _HOST.fullmatch(text)
    if match is None:
        return None
    port = match.group(2)
    if port is not None:
        port = port.lstrip("0")
        if not port or len(port) > 5 or int(port) > 65535:
            return None
    return match.group(1).strip("[]").lower(), int(port) if port else None


def _same_origin(origin: bytes, host: tuple[str, int | None], scheme: str) -> bool:
    try:
        value = origin.decode("ascii")
        if not _ORIGIN.fullmatch(value):
            return False
        parsed = urlsplit(value)
        authority = _host(parsed.netloc.encode("ascii"))
        if authority is None:
            return False
        origin_host, origin_port = authority
    except (UnicodeDecodeError, ValueError):
        return False
    if parsed.scheme.lower() != scheme or origin_host is None:
        return False
    if origin_host.lower() != host[0]:
        return False
    default_port = 443 if scheme == "https" else 80
    return (origin_port or default_port) == (host[1] or default_port)


class LocalBoundary:
    """Check raw ASGI headers before routing, dependencies or redirects."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("gameads.peer", scope.get("client"))
        try:
            peer = ipaddress.ip_address(client[0]) if client else None
        except (ValueError, TypeError):
            peer = None
        code = None
        if peer is None or not peer.is_loopback:
            code = "local_access_only"
        else:
            headers = scope.get("headers", [])
            hosts = [value for name, value in headers if name.lower() == b"host"]
            host = _host(hosts[0]) if len(hosts) == 1 else None
            if host is None:
                code = "local_host_required"
            else:
                origins = [value for name, value in headers if name.lower() == b"origin"]
                scheme = scope.get("gameads.scheme", scope.get("scheme", "http")).lower()
                if (
                    scheme not in {"http", "https"}
                    or len(origins) > 1
                    or (origins and not _same_origin(origins[0], host, scheme))
                ):
                    code = "cross_origin_operation_denied"
        if code is None:
            await self.app(scope, receive, send)
            return
        raw_path = scope.get("raw_path", b"")
        is_task = is_task_path(raw_path)
        content = {
            "status": "error",
            "code": code,
            "message": "Request could not be completed. Check inputs/configuration.",
        }
        if is_task:
            content.update(task_id=None, retryable=False)
        headers = {"Cache-Control": "no-store"} if is_task else None
        await JSONResponse(status_code=403, content=content, headers=headers)(scope, receive, send)
