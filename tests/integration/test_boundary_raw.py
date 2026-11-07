"""Byte-preserving security vectors. No business mocks count as HTTP integration."""

import asyncio
import json

import pytest

from app.local_boundary import LocalBoundary

PATHS = [
    b"/health",
    b"/docs",
    b"/docs/",
    b"/docs/oauth2-redirect",
    b"/redoc",
    b"/openapi.json",
    b"/api/v1/campaigns",
    b"/api/v1/chat",
    b"/api/v1/budget-proposals",
    b"/api/v1/reports",
    b"/api/v1/report-workflows",
    b"/api/v1/tasks",
    b"/api/v1/tasks/operations/lookup",
    b"/unknown",
]


def exchange(headers, peer=("127.0.0.1", 12345), path=b"/health", method="GET"):
    calls = {"route": 0, "body": 0}
    sent = []

    async def downstream(scope, receive, send):
        calls["route"] += 1

    async def receive():
        calls["body"] += 1
        return {"type": "http.request", "body": b"sensitive-never-read"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "client": peer,
        "scheme": "http",
        "headers": headers,
        "path": path.decode("ascii"),
        "raw_path": path,
        "method": method,
    }
    asyncio.run(LocalBoundary(downstream)(scope, receive, send))
    return calls, sent


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "OPTIONS", "PATCH"])
@pytest.mark.parametrize(
    ("peer", "headers", "code"),
    [
        (None, [(b"host", b"evil.invalid"), (b"origin", b"null")], "local_access_only"),
        (
            ("203.0.113.9", 2),
            [(b"host", b"localhost"), (b"x-forwarded-for", b"127.0.0.1")],
            "local_access_only",
        ),
        (
            ("127.0.0.1", 2),
            [(b"host", b"evil.invalid"), (b"origin", b"null")],
            "local_host_required",
        ),
        (
            ("127.0.0.1", 2),
            [(b"host", b"localhost"), (b"origin", b"null")],
            "cross_origin_operation_denied",
        ),
    ],
)
def test_rejection_order_never_dispatches_or_reads_body(path, method, peer, headers, code):
    calls, sent = exchange(headers, peer, path, method)
    assert calls == {"route": 0, "body": 0}
    assert sent[0]["status"] == 403
    assert json.loads(sent[1]["body"])["code"] == code


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"host", b"localhost"), (b"Host", b"localhost")],
    ],
)
def test_missing_or_duplicate_host(headers):
    calls, sent = exchange(headers)
    assert calls == {"route": 0, "body": 0}
    assert json.loads(sent[1]["body"])["code"] == "local_host_required"


@pytest.mark.parametrize(
    "host",
    [
        b"testserver",
        b"localhost.",
        b"user@localhost",
        b"localhost/",
        b"localhost,127.0.0.1",
        b"localhost:0",
        b"localhost:65536",
        b"localhost:-1",
        b"::1",
        b"localhost:",
        b"localhost:" + b"9" * 5000,
        b"localhost\xff",
        b"localhost:+80",
    ],
)
def test_bad_host_does_not_raise_or_call_router(host):
    calls, sent = exchange([(b"host", host)])
    assert calls == {"route": 0, "body": 0}
    assert sent[0]["status"] == 403


@pytest.mark.parametrize(
    "origin",
    [
        b"",
        b"null",
        b"http://localhost/",
        b"http://localhost?",
        b"http://localhost#",
        b"http://user@localhost",
        b"http://localhost,http://localhost",
        b"http://localhost http://localhost",
        b"http://localhost:",
        b"https://localhost",
        b"http://127.0.0.1",
        b"http://localhost:81",
    ],
)
def test_bad_origin_does_not_dispatch(origin):
    calls, sent = exchange([(b"host", b"localhost"), (b"origin", origin)])
    assert calls == {"route": 0, "body": 0}
    assert json.loads(sent[1]["body"])["code"] == "cross_origin_operation_denied"


@pytest.mark.parametrize(
    "host,origin",
    [
        (b"localhost", b"http://LOCALHOST:80"),
        (b"localhost:000080", b"http://localhost"),
        (b"localhost", b"http://localhost:000080"),
        (b"127.0.0.1:8000", b"http://127.0.0.1:8000"),
        (b"[::1]", b"http://[::1]:80"),
    ],
)
def test_valid_baseline_enters_router(host, origin):
    calls, sent = exchange([(b"host", host), (b"origin", origin)])
    assert calls == {"route": 1, "body": 0}
    assert sent == []
