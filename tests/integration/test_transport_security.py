"""Real TCP tests for raw values and launch enforcement; no HTTP mocks."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import h11
import httpx
import pytest

from app.http_transport import RawKeyConnection


@contextmanager
def running(tmp_path, mode="safe"):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GAMEADS_")}
    env.update(GAMEADS_LLM_API_KEY="", GAMEADS_LLM_MODEL="")
    if mode == "safe":
        command = [sys.executable, "-m", "app.server", "--port", str(port)]
    elif mode == "observe":
        code = """
import json, sys
from app.main import create_app
from app.server import serve
class Observer:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            row = {"raw": [v.hex() for v in scope.get("gameads.raw_keys", [])],
                   "fields": [v.hex() for v in scope.get("gameads.raw_key_fields", [])],
                   "asgi": [v.hex() for k, v in scope["headers"] if k == b"idempotency-key"]}
            with open(sys.argv[2], "a") as output:
                output.write(json.dumps(row) + "\\n")
        await self.app(scope, receive, send)
app = create_app()
app.add_middleware(Observer)
serve(app, port=int(sys.argv[1]))
"""
        command = [sys.executable, "-c", code, str(port), str(tmp_path / "layers.jsonl")]
    else:
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:create_app" if mode == "factory" else "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ]
        if mode == "factory":
            command += ["--factory"]
        if mode == "lifespan-off":
            command += ["--lifespan", "off"]
    with (tmp_path / (mode + ".log")).open("wb") as log:
        process = subprocess.Popen(command, cwd=tmp_path, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, (tmp_path / (mode + ".log")).read_text()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def wire(port, header, *, target=b"/api/v1/tasks", method=b"POST", close=True):
    return (
        method
        + b" "
        + target
        + b" HTTP/1.1\r\nHost: 127.0.0.1:"
        + str(port).encode()
        + b"\r\nContent-Length: 0\r\n"
        + (b"Connection: close\r\n" if close else b"")
        + header
        + b"\r\n"
    )


def exchange(port, payload, fragmented=False):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        if fragmented:
            for offset in range(0, len(payload), 7):
                connection.sendall(payload[offset : offset + 7])
        else:
            connection.sendall(payload)
        result = b""
        while chunk := connection.recv(65536):
            result += chunk
        return result


def test_real_tcp_raw_headers_and_observed_layers(tmp_path, record_property):
    vectors = [
        (b"Idempotency-Key: wire-key\r\n", 404),
        (b"Idempotency-Key:wire-key\r\n", 404),
        (b"iDeMpOtEnCy-KeY: MiXeD.Key:1\r\n", 404),
        (b"Idempotency-Key: " + b"a" * 128 + b"\r\n", 404),
        (b"Idempotency-Key: a\r\n", 404),
        (b"Idempotency-Key:  wire-key\r\n", 422),
        (b"Idempotency-Key:\twire-key\r\n", 422),
        (b"Idempotency-Key: wire-key \r\n", 422),
        (b"Idempotency-Key:  wire-key \t\r\n", 422),
        (b"Idempotency-Key: wire-key\t\r\n", 422),
        (b"Idempotency-Key: \r\n", 422),
        (b"Idempotency-Key: " + b"a" * 129 + b"\r\n", 422),
        (b"Idempotency-Key: x,y\r\n", 422),
        (b"Idempotency-Key: x%32\r\n", 422),
        (b"Idempotency-Key: x\ty\r\n", 422),
        (b"Idempotency-Key: \xff\r\n", 422),
        (b"Idempotency-Key: x\x00y\r\n", 400),
        (b"Idempotency-Key: x\r\nIDEMPOTENCY-KEY: x\r\n", 422),
        (b"Idempotency-Key: x\r\nIdempotency-Key: y\r\n", 422),
        (b"Idempotency-Key: x\r\n \t\r\n", 400),
    ]
    observations = []
    with running(tmp_path, "observe") as port:
        targets = [(b"/api/v1/tasks", b"POST")]
        targets += [
            (b"/api/v1/tasks/" + b"a" * 32 + b"/" + a, b"POST")
            for a in (b"approve", b"reject", b"simulate", b"report")
        ]
        targets += [(b"/api/v1/tasks/operations/lookup?method=POST&path=/api/v1/tasks", b"GET")]
        for target, method in targets:
            for header, expected in vectors:
                response = exchange(port, wire(port, header, target=target, method=method), True)
                status = int(response.split(b" ", 2)[1])
                assert status == expected, (header, target, response)
                if method == b"GET" and expected != 400:
                    assert b"cache-control: no-store" in response.lower()
                observations.append(
                    {"target": target.decode(), "header_hex": header.hex(), "status": status}
                )
        # Safety precedes key rejection even though the raw value was captured first.
        response = exchange(port, wire(port, b"Idempotency-Key: x \r\nOrigin: null\r\n"))
        assert b" 403 " in response.split(b"\r\n", 1)[0]
        # Keep-alive/pipelining, with valid and invalid headers in one TCP write.
        response = exchange(
            port, wire(port, vectors[0][0], close=False) + wire(port, vectors[7][0])
        )
        assert b"HTTP/1.1 404" in response and b"HTTP/1.1 422" in response
    layers = [json.loads(line) for line in (tmp_path / "layers.jsonl").read_text().splitlines()]
    assert any(
        row["fields"] == [b"  wire-key \t".hex()]
        and row["raw"] == [b" wire-key \t".hex()]
        and row["asgi"] == [b"wire-key".hex()]
        for row in layers
    )
    record_property("raw_tcp_vectors", json.dumps(observations))
    record_property("observed_layers", json.dumps(layers))


@pytest.mark.parametrize("mode", ["safe", "default", "factory", "lifespan-off"])
def test_real_launch_boundary_and_proxy_forgery(tmp_path, mode):
    with running(tmp_path, mode) as port:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            for path in ("/health", "/docs", "/openapi.json", "/missing", "/api/v1/reports"):
                if mode != "safe":
                    response = client.get(
                        path, headers={"x-forwarded-for": "127.0.0.1", "x-forwarded-proto": "https"}
                    )
                    assert response.status_code == 503
                    assert response.json()["code"] == "unsafe_http_transport"
                else:
                    response = client.get(
                        path,
                        headers={
                            "origin": f"https://127.0.0.1:{port}",
                            "x-forwarded-proto": "https",
                        },
                    )
                    assert response.status_code == 403
            if mode == "safe":
                assert (
                    client.get("/health", headers={"x-forwarded-for": "203.0.113.9"}).status_code
                    == 200
                )
    assert not (tmp_path / "data").exists()


def test_parser_keeps_key_bytes_without_global_h11_patch():
    payload = b"POST /api/v1/tasks HTTP/1.1\r\nHost: localhost\r\nIdempotency-Key:  x \t\r\n\r\n"
    original, captured = h11.Connection(h11.SERVER), RawKeyConnection(h11.SERVER)
    for connection in (original, captured):
        connection.receive_data(payload)
        assert list(connection.next_event().headers)[-1] == (b"idempotency-key", b"x")
    assert captured.raw_keys == [b" x \t"]
    assert captured.raw_fields == [b"  x \t"]
