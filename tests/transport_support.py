"""Explicit in-process scope adapter; never installed by a production launcher."""

from app.http_transport import TRANSPORT_TOKEN
from app.main import create_app as production_app


class InProcessTransport:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        scope["gameads.transport"] = TRANSPORT_TOKEN
        await self.app(scope, receive, send)


def create_app(**kwargs):
    application = production_app(**kwargs)
    application.add_middleware(InProcessTransport)
    return application
