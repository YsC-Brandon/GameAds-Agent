"""One supported HTTP/1 transport: capture before h11 normalizes field values.

Private h11 reader integration is deliberately version-gated. No global patches.
One optional SP after ':' is HTTP serialization, not part of the key. Any
additional SP, any HTAB, trailing whitespace or obs-fold remains invalid.
This cannot distinguish a client's intended leading SP from that delimiter.
"""

from importlib.metadata import version

import h11
from h11._readers import READERS
from starlette.responses import JSONResponse
from uvicorn.protocols.http.h11_impl import H11Protocol

TRANSPORT_TOKEN = object()


class _HeaderCapture:
    def __init__(self, buffer, owner):
        self.buffer, self.owner = buffer, owner

    def __getattr__(self, name):
        return getattr(self.buffer, name)

    def maybe_extract_lines(self):
        lines = self.buffer.maybe_extract_lines()
        if lines is not None:
            keys = []
            fields = []
            for line in lines[1:]:
                # Do not allow h11's obsolete folding to erase an empty
                # continuation. Reject at the HTTP parser layer, before ASGI.
                if line.startswith((b" ", b"\t")):
                    raise h11.LocalProtocolError("Obsolete header folding is unsupported")
                name, colon, value = line.partition(b":")
                if colon and name.lower() == b"idempotency-key":
                    fields.append(value)
                    keys.append(value[1:] if value.startswith(b" ") else value)
            self.owner.raw_keys = keys
            self.owner.raw_fields = fields
        return lines


class RawKeyConnection(h11.Connection):
    def __init__(self, *args, **kwargs):
        self.raw_keys = []
        self.raw_fields = []
        super().__init__(*args, **kwargs)

    def _get_io_object(self, role, event, io_dict):
        reader = super()._get_io_object(role, event, io_dict)
        if io_dict is READERS and role is h11.CLIENT and self.their_state is h11.IDLE:
            return lambda buffer: reader(_HeaderCapture(buffer, self))
        return reader


def check_transport_versions():
    if version("uvicorn") != "0.54.0" or version("h11") != "0.16.0":
        raise RuntimeError("HTTP transport dependency versions require security revalidation")


class LocalHTTPProtocol(H11Protocol):
    def __init__(self, config, server_state, app_state, _loop=None):
        check_transport_versions()
        super().__init__(config, server_state, app_state, _loop)
        self.conn = RawKeyConnection(h11.SERVER, config.h11_max_incomplete_event_size or 16384)
        application = self.app

        async def guarded(scope, receive, send):
            # Copy per request BEFORE awaiting: persistent/pipelined requests
            # must never use another request's key. The protocol captures the
            # snapshot synchronously when Uvicorn schedules each request.
            scope = dict(scope)
            scope["gameads.transport"] = TRANSPORT_TOKEN
            scope["client"] = self.client
            scope["scheme"] = self.scheme
            scope["gameads.peer"] = self.client
            scope["gameads.scheme"] = self.scheme
            scope["gameads.raw_keys"] = scope.pop("gameads.captured_keys")
            scope["gameads.raw_key_fields"] = scope.pop("gameads.captured_fields")
            await application(scope, receive, send)

        self.app = guarded

    def handle_events(self):
        # Uvicorn creates the scope and schedules the task synchronously inside
        # handle_events; its request cycle pauses further request parsing until
        # the response is complete. No await occurs before attaching the keys.
        super().handle_events()
        if self.scope is not None and "gameads.captured_keys" not in self.scope:
            self.scope["gameads.captured_keys"] = tuple(self.conn.raw_keys)
            self.scope["gameads.captured_fields"] = tuple(self.conn.raw_fields)


class TransportBoundary:
    """Fail closed for default Uvicorn, including --lifespan off and factories."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("gameads.transport") is not TRANSPORT_TOKEN:
            await JSONResponse(
                {
                    "status": "error",
                    "code": "unsafe_http_transport",
                    "message": "Use the supported local HTTP launcher.",
                },
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)
