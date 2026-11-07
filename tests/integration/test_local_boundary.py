"""Early security guard verified against an actual loopback server and raw ASGI headers."""

import hashlib
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.transport_support import create_app


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/health", "GET"),
        ("/api/v1/campaigns", "GET"),
        ("/api/v1/chat", "POST"),
        ("/api/v1/reports", "POST"),
        ("/openapi.json", "GET"),
        ("/docs", "HEAD"),
        ("/redoc", "OPTIONS"),
        ("/missing", "PATCH"),
        ("/api/v1/tasks", "POST"),
    ],
)
@pytest.mark.parametrize("bad", ["peer", "host", "origin"])
def test_guard_before_routing_and_storage(tmp_path, path, method, bad):
    db = tmp_path / "business.sqlite3"
    reports = tmp_path / "reports"
    app = create_app(business_db_path=db, reports_dir=reports)
    peer = ("203.0.113.9", 12345) if bad == "peer" else ("127.0.0.1", 12345)
    client = TestClient(app, base_url="http://localhost:8000", client=peer)
    headers = {"host": "attacker.invalid"} if bad == "host" else {}
    if bad == "origin":
        headers["origin"] = "https://localhost:8000"
    response = client.request(method, path, headers=headers, json={"query": "sensitive"})
    expected = {
        "peer": "local_access_only",
        "host": "local_host_required",
        "origin": "cross_origin_operation_denied",
    }[bad]
    assert response.status_code == 403
    if method != "HEAD":  # RFC HEAD never carries a response body.
        assert response.json()["code"] == expected
    if path.startswith("/api/v1/tasks"):
        assert response.json()["task_id"] is None
        assert response.json()["retryable"] is False
    assert not db.exists() and not reports.exists()


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([(b"host", b"localhost:8000"), (b"HOST", b"localhost:8000")], "local_host_required"),
        ([(b"host", b"testserver")], "local_host_required"),
        ([(b"host", b"localhost:")], "local_host_required"),
        ([(b"host", b"localhost:65536")], "local_host_required"),
        (
            [
                (b"host", b"localhost:8000"),
                (b"origin", b"http://localhost:8000"),
                (b"ORIGIN", b"http://localhost:8000"),
            ],
            "cross_origin_operation_denied",
        ),
        (
            [(b"host", b"localhost:8000"), (b"origin", b"http://localhost:")],
            "cross_origin_operation_denied",
        ),
        (
            [(b"host", b"localhost:8000"), (b"origin", b"http://127.0.0.1:8000")],
            "cross_origin_operation_denied",
        ),
        ([(b"host", b"localhost:8000"), (b"origin", b"null")], "cross_origin_operation_denied"),
    ],
)
def test_raw_duplicate_and_malformed_headers_rejected(tmp_path, headers, expected):
    db = tmp_path / "business.sqlite3"
    client = TestClient(create_app(business_db_path=db), base_url="http://localhost:8000")
    response = client.get("/api/v1/reports", headers=headers)
    assert response.status_code == 403 and response.json()["code"] == expected
    assert not db.exists()


def test_real_http_guard_ignores_proxy_headers_and_does_not_touch_db(tmp_path):
    db, reports = tmp_path / "state.sqlite3", tmp_path / "reports"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GAMEADS_")}
    env["GAMEADS_LLM_API_KEY"] = ""
    env["GAMEADS_LLM_MODEL"] = ""
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
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None
                try:
                    assert client.get("/health").status_code == 200
                    break
                except httpx.TransportError:
                    assert time.monotonic() < deadline
                    time.sleep(0.1)
            for path in ("/health", "/api/v1/reports", "/openapi.json", "/missing"):
                invalid = (
                    ({"host": "testserver"}, "local_host_required"),
                    ({"origin": "http://evil.invalid"}, "cross_origin_operation_denied"),
                    (
                        {
                            "origin": "https://evil.invalid",
                            "x-forwarded-proto": "https",
                            "x-forwarded-host": "evil.invalid",
                        },
                        "cross_origin_operation_denied",
                    ),
                )
                for headers, expected in invalid:
                    before = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
                    response = client.get(path, headers=headers)
                    assert response.status_code == 403 and response.json()["code"] == expected
                    assert (
                        hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
                    ) == before
                    assert not reports.exists()
                valid_proxy_spoof = {
                    "origin": f"http://127.0.0.1:{port}",
                    "x-forwarded-proto": "https",
                    "x-forwarded-host": "evil.invalid",
                }
                assert client.get(path, headers=valid_proxy_spoof).status_code != 403
            before = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
            for headers in ({"host": "testserver"}, {"origin": "null"}):
                assert (
                    client.post("/api/v1/budget-proposals", json={}, headers=headers).status_code
                    == 403
                )
            assert (hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None) == before
            assert not reports.exists()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
