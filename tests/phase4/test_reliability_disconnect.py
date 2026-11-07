"""A real TCP disconnect after acceptance cannot cancel or duplicate execution."""

import json
import socket
import threading
import time

from app.agent.contracts import AgentResponse
from tests.phase4.test_acceptance_security import live

__all__ = ["live"]


def test_disconnect_after_running_preserves_one_sealed_operation(
    live, monkeypatch, record_property
):
    client, candidate, _, port, _ = live
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def offline(self, request):
        calls.append(threading.get_ident())
        entered.set()
        assert finish.wait(10)
        return AgentResponse(request_id="synthetic", status="unsupported", final_answer="Offline")

    monkeypatch.setattr("app.agent.service.AgentService.run", offline)
    body = {"kind": "analysis", "request": {"query": "private disconnect sentinel"}}
    payload = json.dumps(body).encode()
    wire = (
        f"POST /api/v1/tasks HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Content-Type: application/json\r\nIdempotency-Key: disconnected\r\n"
        f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
    ).encode() + payload
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(wire)
            assert entered.wait(5)
            lookup = client.get(
                "/api/v1/tasks/operations/lookup",
                params={"method": "POST", "path": "/api/v1/tasks"},
                headers={"Idempotency-Key": "disconnected"},
            ).json()["data"]
            assert lookup["phase"] == "in_progress"
            task = client.get("/api/v1/tasks/" + lookup["task_id"]).json()["data"]
            assert task["status"] == "running"
            connection.shutdown(socket.SHUT_RDWR)
        finish.set()
        deadline = time.monotonic() + 5
        while True:
            operation = candidate.lookup("POST", b"/api/v1/tasks", b"disconnected")
            if operation["phase"] == "completed":
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        replay = client.post(
            "/api/v1/tasks", json=body, headers={"Idempotency-Key": "disconnected"}
        )
        assert replay.status_code == operation["sealed_http_status"] == 422
        assert replay.content == operation["response"]
        assert len(calls) == 1 and "private disconnect sentinel" not in replay.text
        with candidate.snapshot() as connection:
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
            assert tuple(connection.execute("SELECT status,version FROM tasks").fetchone()) == (
                "unsupported",
                3,
            )
        record_property(
            "disconnect_evidence",
            json.dumps(
                {
                    "calls": calls,
                    "accepted": lookup,
                    "sealed_status": operation["sealed_http_status"],
                    "response": operation["response"].decode(),
                }
            ),
        )
    finally:
        finish.set()
