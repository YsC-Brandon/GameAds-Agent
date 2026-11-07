"""Bound task JSON input before dependency evaluation or SQLite admission."""

from starlette.responses import JSONResponse

from app.phase4.service import failure
from app.task_wire import POST_PATH


class TaskBodyBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not POST_PATH.fullmatch(scope.get("raw_path", b""))
        ):
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        types = [v for k, v in headers if k.lower() == b"content-type"]
        if types != [b"application/json"]:
            await JSONResponse(failure("unsupported_media_type"), status_code=415)(
                scope, receive, send
            )
            return
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > 64 * 1024:
                await JSONResponse(failure("request_too_large"), status_code=413)(
                    scope, receive, send
                )
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay, send)
