"""Actual loopback HTTP budget lifecycle; isolated files and no external model calls."""

import hashlib
import os
import socket
import subprocess
import sys
import time

import httpx


def test_phase3_live_http_no_key_end_to_end(business, budget_request, tmp_path):
    before = hashlib.sha256(business.data_path.read_bytes()).hexdigest()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("GAMEADS_")}
    env["GAMEADS_LLM_API_KEY"], env["GAMEADS_LLM_MODEL"] = "", ""
    command = (
        "from pathlib import Path; import sys; from app.main import create_app; "
        "from app.server import serve; "
        "serve(create_app(data_path=Path(sys.argv[1]), business_db_path=Path(sys.argv[2]), "
        "reports_dir=Path(sys.argv[3])), port=int(sys.argv[4]))"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            command,
            str(business.data_path),
            str(business.store.path),
            str(tmp_path / "reports"),
            str(port),
        ],
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
                assert process.poll() is None, "Uvicorn exited before startup."
                try:
                    response = client.get("/health")
                    break
                except httpx.TransportError:
                    if time.monotonic() > deadline:
                        raise AssertionError("Uvicorn startup timed out.") from None
                    time.sleep(0.1)
            assert response.json() == {"status": "ok", "model_configured": False}
            response = client.post(
                "/api/v1/budget-proposals", json=budget_request.model_dump(mode="json")
            )
            assert response.status_code == 201
            proposal = response.json()["data"]
            base = "/api/v1/budget-proposals/" + proposal["id"]
            review = {"proposal_sha256": proposal["proposal_sha256"], "expected_version": 1}
            assert client.post(base + "/simulate", json=review).status_code == 409
            assert (
                client.post(
                    base + "/approve",
                    json=review | {"confirmed": True, "approver": "loopback-test-reviewer"},
                ).status_code
                == 200
            )
            review["expected_version"] = 2
            simulation = client.post(base + "/simulate", json=review)
            assert simulation.json()["data"]["simulation"]["real_execution"] is False
            assert client.post(base + "/simulate", json=review).status_code == 409
            dates = {"start_date": "2026-10-01", "end_date": "2026-10-01"}
            report = client.post("/api/v1/reports", json=dates | {"proposal_id": proposal["id"]})
            assert report.status_code == 201
            report_id = report.json()["data"]["id"]
            assert client.get("/api/v1/reports/" + report_id).json()["data"]["content"]
            failed_agent = client.post("/api/v1/report-workflows", json=dates)
            assert failed_agent.status_code == 503 and failed_agent.json()["data"]["report"] is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    assert hashlib.sha256(business.data_path.read_bytes()).hexdigest() == before
