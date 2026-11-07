"""Safe launcher is bound to loopback without forwarded-header trust."""

import os
import socket
import subprocess
import sys
import time

import httpx


def test_launcher_real_loopback_without_key(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GAMEADS_")}
    env["GAMEADS_LLM_API_KEY"] = ""
    env["GAMEADS_LLM_MODEL"] = ""
    process = subprocess.Popen(
        [sys.executable, "-m", "app.server", "--port", str(port)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=3
        ) as client:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, "Local server exited before readiness."
                try:
                    assert client.get("/health").json() == {
                        "status": "ok",
                        "model_configured": False,
                    }
                    break
                except httpx.TransportError:
                    assert time.monotonic() < deadline
                    time.sleep(0.1)
            for path in ("/health", "/api/v1/campaigns", "/openapi.json"):
                response = client.get(
                    path, headers={"host": "testserver", "x-forwarded-host": "127.0.0.1"}
                )
                assert response.status_code == 403
                assert response.json()["code"] == "local_host_required"
                response = client.get(
                    path, headers={"origin": "https://127.0.0.1", "x-forwarded-proto": "https"}
                )
                assert response.status_code == 403
                assert response.json()["code"] == "cross_origin_operation_denied"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
