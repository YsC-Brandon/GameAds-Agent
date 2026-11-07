"""Subprocess CLI exit/status, proxy and redirect boundaries over real HTTP."""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (200, {"items": [], "limit": 50, "offset": 0, "has_more": False}, 0),
        (500, {"status": "error", "code": "internal_error"}, 1),
        (422, {"status": "error", "code": "invalid_request"}, 2),
        (422, {"status": "error", "code": "needs_clarification"}, 3),
        (409, {"status": "error", "code": "operation_interrupted"}, 4),
        (503, {"status": "error", "code": "database_busy"}, 5),
        (302, {"status": "error", "code": "redirect"}, 1),
        (200, {"data": {"status": "failed"}}, 0),
    ],
)
def test_cli_real_wire_exit_codes_no_proxy_redirect(tmp_path, status, payload, expected):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(
        os.environ,
        HTTP_PROXY="http://127.0.0.1:1",
        HTTPS_PROXY="http://127.0.0.1:1",
        ALL_PROXY="http://127.0.0.1:1",
        NO_PROXY="",
    )
    try:
        command = ["get", "a" * 32] if payload == {"data": {"status": "failed"}} else ["list"]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.task_cli",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                *command,
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            input="",
            timeout=15,
        )
        assert result.returncode == expected, result.stderr
        assert json.loads(result.stdout) == payload
        assert bool(result.stderr) == bool(expected)
        assert len(requests) == 1 and "/must-not-follow" not in requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_cli_timeout_does_not_send_cancel_or_retry(tmp_path):
    requests, completed = [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            time.sleep(0.3)
            completed.set()
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.task_cli",
                "--timeout",
                "0.05",
                "--base-url",
                f"http://127.0.0.1:{server.server_port}",
                "list",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            input="",
            timeout=15,
        )
        assert result.returncode == 5
        assert json.loads(result.stdout)["code"] == "service_unavailable"
        assert completed.wait(2) and len(requests) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize(
    "command",
    [
        "create",
        "list",
        "get",
        "events",
        "reports",
        "approve",
        "reject",
        "simulate",
        "report",
        "report-get",
        "operation-get",
    ],
)
def test_all_cli_help_is_noninteractive(tmp_path, command):
    result = subprocess.run(
        [sys.executable, "-m", "app.task_cli", command, "--help"],
        cwd=tmp_path,
        input="",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0 and not result.stderr
    assert "usage:" in result.stdout.lower()
    assert not list(tmp_path.iterdir())
