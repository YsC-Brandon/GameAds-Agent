"""4A raw-input unit/ASGI and real TCP integration; task storage is not implemented."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from urllib.parse import quote

import httpx
import pytest

from app.task_wire import LOOKUP, TaskWireBoundary, parse_identity

PATHS = [b"/api/v1/tasks"] + [
    b"/api/v1/tasks/" + b"a" * 32 + b"/" + action
    for action in (b"approve", b"reject", b"simulate", b"report")
]


def scope(path, method="POST", query=b"", headers=None):
    return {
        "type": "http",
        "method": method,
        "raw_path": path,
        "query_string": query,
        "headers": [(b"idempotency-key", b"Synthetic.Key-1")] if headers is None else headers,
    }


@pytest.mark.parametrize("path", PATHS)
def test_post_and_lookup_share_exact_identity(path):
    identity = parse_identity(scope(path))
    assert identity == ("POST", path.decode(), "Synthetic.Key-1")
    for value in (path, quote(path.decode(), safe="").encode()):
        assert parse_identity(scope(LOOKUP, "GET", b"method=POST&path=" + value)) == identity


@pytest.mark.parametrize(
    "query",
    [
        b"method=POST&path=%252Fapi%252Fv1%252Ftasks",
        b"method=POST&path=/api/v1/tasks&path=/api/v1/tasks",
        b"method=POST&path=/api/v1/tasks&%70ath=/api/v1/tasks",
        b"method=POST&path=/api/v1/tasks&other=1",
        b"method=POST&Path=/api/v1/tasks",
        b"method=POST&path=%XX",
        b"method=POST&path=%FF",
        b"method=POST&path=/api/v1/tasks+",
        b"method=post&path=/api/v1/tasks",
        b"method=POST&path=/api/v1/tasks/",
        b"method=POST",
        b"method=POST&path=",
        b"method=POST&path=/api/v1/tasks&",
    ],
)
def test_lookup_invalid_queries(query):
    with pytest.raises((ValueError, UnicodeError)):
        parse_identity(scope(LOOKUP, "GET", query))


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"idempotency-key", b"x"), (b"IDEMPOTENCY-KEY", b"x")],
        [(b"idempotency-key", b"x"), (b"idempotency-key", b"y")],
        [(b"idempotency-key", b"x,y")],
        [(b"idempotency-key", b" x")],
        [(b"idempotency-key", b"x%32")],
        [(b"idempotency-key", b"x" * 129)],
    ],
)
@pytest.mark.parametrize("path", PATHS + [LOOKUP])
def test_key_rejections_before_dict_or_trim(headers, path):
    with pytest.raises(ValueError):
        parse_identity(scope(path, "GET" if path == LOOKUP else "POST", headers=headers))


def test_rejection_never_calls_downstream_or_receive():
    async def forbidden(*args):
        raise AssertionError("Rejected request reached body/router/storage.")

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(TaskWireBoundary(forbidden)(scope(PATHS[0] + b"/"), forbidden, send))
    assert sent[0]["status"] == 422
    assert json.loads(sent[1]["body"])["task_id"] is None


def test_real_tcp_paths_duplicates_and_queries_preserve_input(tmp_path):
    db = tmp_path / "state.sqlite3"
    reports = tmp_path / "reports"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GAMEADS_")}
    env.update(GAMEADS_LLM_API_KEY="", GAMEADS_LLM_MODEL="")
    command = (
        "from pathlib import Path; import sys; from app.main import create_app; "
        "from app.server import serve; serve(create_app(business_db_path=Path(sys.argv[1]), "
        "reports_dir=Path(sys.argv[2])), port=int(sys.argv[3]))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", command, str(db), str(reports), str(port)],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def request(target, method=b"POST", extra=b"", key=b"Idempotency-Key: wire-key\r\n"):
        wire = method + b" " + target + b" HTTP/1.1\r\nHost: 127.0.0.1:" + str(port).encode()
        wire += b"\r\nConnection: close\r\nContent-Length: 0\r\n" + key + extra + b"\r\n"
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            connection.sendall(wire)
            result = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                result += chunk
        head, body = result.split(b"\r\n\r\n", 1)
        payload = json.loads(body) if b"application/json" in head.lower() else body
        return int(head.split(b" ")[1]), head, payload

    try:
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False, timeout=2) as client:
            while True:
                assert process.poll() is None
                try:
                    assert client.get(f"http://127.0.0.1:{port}/health").status_code == 200
                    break
                except httpx.TransportError:
                    assert time.monotonic() < deadline
                    time.sleep(0.1)
        for path in PATHS:
            # 404 is deliberate: prevalidation is implemented, task business routes are not.
            assert request(path)[0] == 404
            aliases = [
                path + b"/",
                path.replace(b"/v1/", b"//v1/"),
                path.replace(b"/v1/", b"/./v1/"),
                path.replace(b"/v1/", b"/x/../v1/"),
                path.replace(b"api", b"%61pi"),
                path.replace(b"api", b"%2561pi"),
                path.replace(b"/v1/", b"%2Fv1%2F"),
                b"/" + path,
                path.replace(b"/v1/", b"\\v1\\"),
                path.upper(),
                path + b"?unknown=1",
            ]
            for alias in aliases:
                status, _, body = request(alias)
                assert status == 422, (alias, status)
                assert body["code"] == "invalid_request" and body["task_id"] is None
                assert not db.exists() and not reports.exists()
            assert request(path, extra=b"IDEMPOTENCY-KEY: wire-key\r\n")[0] == 422
            assert request(path, key=b"")[0] == 422
            assert request(path, extra=b"Origin: null\r\n")[0] == 403
        assert request(b"/api/v1/tasks/not-an-id/approve")[0] == 422
        for query, expected in (
            (b"method=POST&path=/api/v1/tasks", 404),
            (b"method=POST&path=%2Fapi%2Fv1%2Ftasks", 404),
            (b"method=POST&path=%252Fapi%252Fv1%252Ftasks", 422),
            (b"method=POST&path=/api/v1/tasks&%70ath=/api/v1/tasks", 422),
            (b"method=POST&path=%XX", 422),
        ):
            status, headers, _ = request(LOOKUP + b"?" + query, method=b"GET")
            assert status == expected
            assert b"cache-control: no-store" in headers.lower()
        for extra, expected in (
            (b"Origin: http://127.0.0.1:" + str(port).encode() + b"\r\nOrigin: null\r\n", 403),
            (b"Host: 127.0.0.1\r\n", 400),
        ):
            status, _, _ = request(PATHS[0], extra=extra)
            assert status == expected
        assert not db.exists() and not reports.exists()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
