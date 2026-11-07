"""Run with an isolated installed Python, outside the source tree, without keys."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def main():
    evidence = Path(sys.argv[1]).resolve()
    evidence.mkdir(exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="gameads-installed-runtime-"))
    database = work / "synthetic.sqlite3"
    reports = work / "reports"
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GAMEADS_")}
    env.update(GAMEADS_LLM_API_KEY="", GAMEADS_LLM_MODEL="")
    results = []

    def run(name, arguments, *, stdin=None):
        result = subprocess.run(
            [sys.executable, *arguments],
            cwd=work,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
        )
        (evidence / (name + ".stdout.log")).write_text(result.stdout, encoding="utf-8")
        (evidence / (name + ".stderr.log")).write_text(result.stderr, encoding="utf-8")
        results.append({"name": name, "exit_code": result.returncode, "arguments": arguments})
        (evidence / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        assert result.returncode == 0, name + " failed"
        return json.loads(result.stdout)

    run("init", ["-m", "app.phase4.maintenance", "init", "--database", str(database)])
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))
    command = [
        sys.executable,
        "-m",
        "app.server",
        "--database",
        str(database),
        "--reports-dir",
        str(reports),
        "--port",
        str(port),
    ]

    def start(log):
        process = subprocess.Popen(command, cwd=work, env=env, stdout=log, stderr=log)
        deadline = time.monotonic() + 20
        while True:
            assert process.poll() is None, "installed server exited"
            try:
                with opener.open(base + "/health", timeout=1) as response:
                    assert json.load(response) == {"status": "ok", "model_configured": False}
                return process
            except OSError:
                assert time.monotonic() < deadline, "installed server timed out"
                time.sleep(0.05)

    cli = ["-m", "app.task_cli", "--base-url", base]
    with (evidence / "server.log").open("xb") as log:
        process = start(log)
        try:
            request = {
                "kind": "budget_review",
                "request": {
                    "start_date": "2026-10-24",
                    "end_date": "2026-10-30",
                    "budget_period": {"start_date": "2026-11-01", "end_date": "2026-11-07"},
                    "campaigns": [
                        {
                            "campaign_id": "C001",
                            "current_budget": "100.00",
                            "min_budget": "0.00",
                            "max_budget": "200.00",
                        }
                    ],
                },
            }
            created = run(
                "create",
                cli + ["create", "--request-json", "-", "--idempotency-key", "install-create"],
                stdin=json.dumps(request),
            )
            task = created["data"]
            with opener.open(
                base + "/api/v1/budget-proposals/" + task["proposal_id"], timeout=5
            ) as response:
                proposal = json.load(response)["data"]
            run(
                "approve",
                cli
                + [
                    "approve",
                    task["id"],
                    "--expected-task-version",
                    "3",
                    "--expected-version",
                    "1",
                    "--proposal-sha256",
                    proposal["proposal_sha256"],
                    "--approver",
                    "synthetic-installer",
                    "--confirm",
                    "--idempotency-key",
                    "install-approve",
                ],
            )
            run(
                "simulate",
                cli
                + [
                    "simulate",
                    task["id"],
                    "--expected-task-version",
                    "4",
                    "--expected-version",
                    "2",
                    "--proposal-sha256",
                    proposal["proposal_sha256"],
                    "--idempotency-key",
                    "install-simulate",
                ],
            )
            completed = run(
                "report",
                cli
                + [
                    "report",
                    task["id"],
                    "--expected-task-version",
                    "5",
                    "--idempotency-key",
                    "install-report",
                ],
            )["data"]
            assert completed["status"] == "completed"
            assert run("get", cli + ["get", task["id"]])["data"] == completed
            report = run("report-get", cli + ["report-get", completed["report_id"]])["data"]
            assert report["proposal"]["version"] == 3
            assert report["content"]
        finally:
            process.terminate()
            process.wait(timeout=10)
    with (evidence / "restart.log").open("xb") as log:
        process = start(log)
        try:
            assert run("restart-get", cli + ["get", task["id"]])["data"] == completed
        finally:
            process.terminate()
            process.wait(timeout=10)
    run("check", ["-m", "app.phase4.maintenance", "check", "--database", str(database)])
    (evidence / "environment.json").write_text(
        json.dumps(
            {
                "python": sys.executable,
                "cwd": str(work),
                "database": "synthetic only",
                "model_calls": 0,
                "restart": "process termination; not power-loss proof",
                "all_commands_zero": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
