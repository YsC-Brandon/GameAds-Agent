"""Actual loopback Uvicorn process and HTTP requests; no external model calls."""

import os
import socket
import subprocess
import sys
import time

import httpx


def test_live_server_and_cli_without_model_key(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GAMEADS_")}
    env["GAMEADS_LLM_API_KEY"] = ""
    env["GAMEADS_LLM_MODEL"] = ""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2
        ) as client:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, "Uvicorn exited before startup."
                try:
                    response = client.get("/health")
                    break
                except httpx.TransportError:
                    if time.monotonic() > deadline:
                        raise AssertionError("Uvicorn startup timed out.") from None
                    time.sleep(0.1)
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "model_configured": False}
            data = client.get("/api/v1/campaigns")
            assert data.json()["data"]["campaign_count"] == 8
            chat = client.post("/api/v1/chat", json={"query": "list campaigns"})
            assert chat.status_code == 503
            assert chat.json()["errors"] == ["model_not_configured"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    cli = subprocess.run(
        [sys.executable, "-m", "app.cli", "list campaigns"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert cli.returncode == 1
    assert "model_not_configured" in cli.stdout
    assert "Traceback" not in cli.stdout + cli.stderr
