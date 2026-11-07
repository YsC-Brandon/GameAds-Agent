"""Real subprocess CLI input protection and task writes over real TCP."""

import json
import os
import subprocess
import sys

import pytest

from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
from tests.phase4.test_owner_application_integration import budget, server

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows task service")


def invoke(tmp_path, *args, stdin=None):
    return subprocess.run(
        [sys.executable, "-m", "app.task_cli", *args],
        input=stdin,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_cli_task_lifecycle_and_explicit_human_confirmation(tmp_path, csv_factory, record):
    data = csv_factory([record])
    database = tmp_path / "candidate.sqlite3"
    with CandidateOwner(database) as owner:
        CandidateStore(owner).initialize_empty_candidate()
    with server(tmp_path, database, data, bound=True) as client:
        base = ["--base-url", str(client.base_url)]
        request = json.dumps({"kind": "budget_review", "request": budget()})
        created = invoke(
            tmp_path,
            *base,
            "create",
            "--request-json",
            "-",
            "--idempotency-key",
            "cli-create",
            stdin=request,
        )
        assert created.returncode == 0, created.stdout + created.stderr
        task = json.loads(created.stdout)["data"]
        assert not created.stderr
        proposal = client.get("/api/v1/budget-proposals/" + task["proposal_id"]).json()["data"]
        review = [
            task["id"],
            "--approver",
            "synthetic",
            "--expected-task-version",
            "3",
            "--expected-version",
            "1",
            "--proposal-sha256",
            proposal["proposal_sha256"],
            "--idempotency-key",
            "cli-approve",
        ]
        missing = invoke(tmp_path, *base, "approve", *review)
        assert missing.returncode == 2 and not missing.stdout
        approved = invoke(tmp_path, *base, "approve", *review, "--confirm")
        assert approved.returncode == 0, approved.stdout + approved.stderr
        simulated = invoke(
            tmp_path,
            *base,
            "simulate",
            task["id"],
            "--expected-task-version",
            "4",
            "--expected-version",
            "2",
            "--proposal-sha256",
            proposal["proposal_sha256"],
            "--idempotency-key",
            "cli-simulate",
        )
        assert simulated.returncode == 0, simulated.stdout + simulated.stderr
        published = invoke(
            tmp_path,
            *base,
            "report",
            task["id"],
            "--expected-task-version",
            "5",
            "--idempotency-key",
            "cli-report",
        )
        assert published.returncode == 0, published.stdout + published.stderr
        assert json.loads(published.stdout)["data"]["status"] == "completed"


@pytest.mark.parametrize(
    "url", ["http://example.com", "http://user@127.0.0.1", "http://127.0.0.1/#fragment"]
)
def test_cli_rejects_nonlocal_url_before_request(tmp_path, url):
    result = invoke(tmp_path, "--base-url", url, "list")
    assert result.returncode == 2 and not result.stdout


def test_cli_requires_explicit_model_intent_without_echoing_query(tmp_path):
    result = invoke(
        tmp_path,
        "create",
        "--request-json",
        "-",
        "--idempotency-key",
        "synthetic",
        stdin=json.dumps({"kind": "analysis", "request": {"query": "private-synthetic"}}),
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "invalid_request"
    assert "private-synthetic" not in result.stdout + result.stderr
