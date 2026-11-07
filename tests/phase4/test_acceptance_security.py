"""Live supported transport matrices; no public listener or real model.

The innermost ASGI probe proves security/prevalidation rejects before routing,
body delivery and dependencies. Real SQLite snapshots independently prove no
operations, task/audit rows or reports change. Nonloopback peers use raw ASGI;
they are not simulated by proxy headers on a loopback TCP connection.
"""

import json
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from starlette.middleware import Middleware

from app.core.config import Settings
from app.http_transport import LocalHTTPProtocol
from app.main import create_app, get_settings
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
from tests.integration.test_boundary_raw import exchange as asgi_exchange
from tests.integration.test_transport_security import exchange

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows local candidate")


@pytest.fixture
def live(tmp_path, csv_factory, record):
    with CandidateOwner(tmp_path / "candidate.sqlite3") as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        app = create_app(
            data_path=csv_factory([record]),
            business_db_path=candidate.path,
            reports_dir=tmp_path / "reports",
            storage_owner=owner,
        )
        app.dependency_overrides[get_settings] = lambda: Settings(
            _env_file=None, llm_api_key="", llm_model=""
        )
        counters = {"router": 0, "body": 0}

        class Probe:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] == "http":
                    counters["router"] += 1

                async def observed():
                    counters["body"] += 1
                    return await receive()

                await self.app(scope, observed, send)

        app.user_middleware.append(Middleware(Probe))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        configuration = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            http=LocalHTTPProtocol,
            proxy_headers=False,
            access_log=False,
            lifespan="off",
            log_level="error",
        )
        service = uvicorn.Server(configuration)
        thread = threading.Thread(target=service.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not service.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.01)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                assert client.get("/health").status_code == 200
                spec = client.get("/openapi.json").json()
                yield client, candidate, counters, port, spec
        finally:
            service.should_exit = True
            thread.join(10)
            sock.close()
            assert not thread.is_alive()


def snapshot(candidate, root):
    with candidate.snapshot() as connection:
        tables = ("tasks", "task_events", "operations", "documents", "events", "task_documents")
        rows = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
    return rows, {p.name: p.read_bytes() for p in (root / "reports").glob("*")}


def raw(port, method, path, headers, body=b""):
    wire = (
        method.encode()
        + b" "
        + path
        + b" HTTP/1.1\r\nConnection: close\r\n"
        + b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\n"
        + b"".join(k + b": " + v + b"\r\n" for k, v in headers)
        + b"\r\n"
        + body
    )
    result = exchange(port, wire)
    return int(result.split(b" ", 2)[1]), result


def test_all_routes_methods_host_origin_proxy_matrix(live, tmp_path, record_property):
    client, candidate, counters, port, spec = live
    paths = [
        p.replace("{task_id}", "a" * 32)
        .replace("{proposal_id}", "a" * 32)
        .replace("{report_id}", "a" * 32)
        .encode()
        for p in spec["paths"]
    ]
    paths += [
        b"/docs",
        b"/docs/",
        b"/docs/oauth2-redirect",
        b"/redoc",
        b"/openapi.json",
        b"/unknown",
        b"/health/",
    ]
    host = b"127.0.0.1:" + str(port).encode()
    origin = b"http://" + host
    vectors = [
        [(b"Host", bad)]
        for bad in (
            b"evil.invalid",
            b"testserver",
            b"localhost.",
            b"user@localhost",
            b"localhost/path",
            b"localhost,127.0.0.1",
            b"localhost:0",
            b"localhost:65536",
            b"localhost:-1",
        )
    ]
    vectors += [
        [(b"Host", host), (b"Origin", bad)]
        for bad in (
            b"null",
            b"",
            b"https://" + host,
            b"http://localhost:" + str(port).encode(),
            b"http://127.0.0.1:1",
            origin + b"/",
            origin + b"?",
            origin + b"#",
            b"http://user@" + host,
            origin + b"," + origin,
            origin + b" " + origin,
        )
    ]
    vectors += [
        [(b"Host", host), (b"Origin", first), (b"origin", last)]
        for first, last in ((origin, b"null"), (b"null", origin), (origin, origin))
    ]
    vectors += [
        [
            (b"Host", host),
            (b"Origin", b"https://" + host),
            (b"X-Forwarded-Proto", b"https"),
            (b"X-Forwarded-For", b"127.0.0.1"),
        ],
        [(b"Host", host), (b"Host", host)],
        [],
    ]
    before = snapshot(candidate, tmp_path)
    total = 0
    for path in paths:
        for method in ("GET", "POST", "HEAD", "OPTIONS", "PATCH"):
            for headers in vectors:
                counters.update(router=0, body=0)
                status, response = raw(port, method, path, headers)
                assert status in {400, 403}, (method, path, headers, response)
                assert counters == {"router": 0, "body": 0}
                total += 1
            # Real peer cannot be spoofed on TCP: exercise absent/nonlocal peer
            # in the same byte-preserving ASGI boundary with legitimate Host.
            for peer in (None, ("203.0.113.9", 123)):
                calls, sent = asgi_exchange(
                    [(b"host", host), (b"x-forwarded-for", b"127.0.0.1")], peer, path, method
                )
                assert calls == {"route": 0, "body": 0} and sent[0]["status"] == 403
    assert snapshot(candidate, tmp_path) == before
    for name in ("localhost", "127.0.0.1", "[::1]"):
        header = f"{name}:{port}"
        assert (
            client.get(
                "/health",
                headers={
                    "Host": header,
                    "Origin": "http://" + header,
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-Host": "evil.invalid",
                },
            ).status_code
            == 200
        )
    record_property("live_security_vectors", str(total))


@pytest.mark.parametrize("existing", [False, True])
def test_live_five_posts_lookup_aliases_and_duplicate_keys(
    live, tmp_path, existing, record_property
):
    client, candidate, counters, port, _ = live
    if existing:
        response = client.post(
            "/api/v1/tasks",
            json={"kind": "analysis", "request": {"query": "offline"}},
            headers={"Idempotency-Key": "known"},
        )
        assert response.status_code == 503
        task_id = response.json()["task_id"]
    else:
        task_id = "a" * 32
    paths = [b"/api/v1/tasks"] + [
        b"/api/v1/tasks/" + task_id.encode() + b"/" + action
        for action in (b"approve", b"reject", b"simulate", b"report")
    ]
    host = [(b"Host", b"127.0.0.1:" + str(port).encode())]
    key = [(b"Idempotency-Key", b"known")]
    before = snapshot(candidate, tmp_path)
    total = 0
    for path in paths:
        aliases = [
            path + b"/",
            path.replace(b"/v1/", b"//v1/"),
            path.replace(b"/v1/", b"/./v1/"),
            path.replace(b"/v1/", b"/x/../v1/"),
            path.replace(b"api", b"%61pi"),
            path.replace(b"api", b"%2561pi"),
            path.replace(b"/v1/", b"%2Fv1%2F"),
            path.upper(),
            path.replace(b"/v1/", b"\\v1\\"),
            path + b"?unknown=1",
        ]
        for alias in aliases:
            counters.update(router=0, body=0)
            status, _ = raw(port, "POST", alias, host + key)
            assert status == 422 and counters == {"router": 0, "body": 0}
            total += 1
    lookup = b"/api/v1/tasks/operations/lookup"
    paths += [lookup + b"?method=POST&path=/api/v1/tasks"]
    for path in paths:
        method = "GET" if path.startswith(lookup) else "POST"
        for keys in (
            [],
            key + key,
            key + [(b"IDEMPOTENCY-KEY", b"different")],
            [(b"Idempotency-Key", b"known,other")],
            [(b"Idempotency-Key", b"known ")],
            [(b"Idempotency-Key", b"x" * 129)],
        ):
            counters.update(router=0, body=0)
            status, response = raw(port, method, path, host + keys)
            assert status == 422 and counters == {"router": 0, "body": 0}
            if method == "GET":
                assert b"cache-control: no-store" in response.lower()
            total += 1
    for query in (
        b"method=POST&path=%252Fapi%252Fv1%252Ftasks",
        b"method=POST&path=/api/v1/tasks&%70ath=/api/v1/tasks",
        b"method=POST&path=%XX",
        b"method=POST&path=%FF",
        b"method=POST&method=POST&path=/api/v1/tasks",
    ):
        counters.update(router=0, body=0)
        status, _ = raw(port, "GET", lookup + b"?" + query, host + key)
        assert status == 422 and counters == {"router": 0, "body": 0}
        total += 1
    assert snapshot(candidate, tmp_path) == before
    record_property("live_wire_vectors", json.dumps({"existing_key": existing, "count": total}))
