"""Real application/SQLite/TCP coverage of cooperative Windows ownership."""

import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from app.phase3.contracts import ApprovalRequest, BudgetRequest, ReportRequest
from app.phase3.reports import ReportService
from app.phase3.service import BusinessService
from app.phase3.store import BusinessError, Store, digest
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows owner integration")
DATES = {"start_date": "2026-10-01", "end_date": "2026-10-01"}


@pytest.fixture
def application_data(csv_factory, record):
    return csv_factory([record])


def budget():
    return DATES | {
        "budget_period": {"start_date": "2026-11-01", "end_date": "2026-11-07"},
        "campaigns": [
            {"campaign_id": "A", "current_budget": "100.00", "min_budget": "0", "max_budget": "200"}
        ],
    }


@contextmanager
def server(tmp_path, database, data, *, bound=False, offline_analysis=False):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    code = """
import sys
from pathlib import Path
from app.main import create_app
from app.server import serve
from app.phase4.ownership import CandidateOwner
from app.phase4.store import CandidateStore
database, data, reports = map(Path, sys.argv[1:4])
owner = None
try:
    if sys.argv[5] == 'bound':
        owner = CandidateOwner(database).acquire()
        CandidateStore(owner).recover_before_serving()
    app = create_app(data_path=data, business_db_path=database, reports_dir=reports,
                     storage_owner=owner)
    if sys.argv[6] == 'offline':
        from app.main import get_agent_service
        from app.agent.service import AgentService
        from app.core.config import Settings
        from app.phase4.offline_demo import OfflineProvider
        app.dependency_overrides[get_agent_service] = lambda: AgentService(
            Settings(_env_file=None, llm_api_key='', llm_model=''),
            provider=OfflineProvider(), data_path=data)
    serve(app, port=int(sys.argv[4]))
finally:
    if owner is not None:
        owner.close()
"""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GAMEADS_")}
    env.update(GAMEADS_LLM_API_KEY="", GAMEADS_LLM_MODEL="")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[2]), str(Path(sys.prefix) / "Lib/site-packages")]
    )
    with (tmp_path / "server.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys._base_executable,
                "-c",
                code,
                str(database),
                str(data),
                str(tmp_path / "reports"),
                str(port),
                "bound" if bound else "unbound",
                "offline" if offline_analysis else "no-model",
            ],
            cwd=tmp_path,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5
            ) as client:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, (tmp_path / "server.log").read_text()
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
                yield client
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_tcp_all_legacy_writes_reject_owner_conflict_before_effects(tmp_path, application_data):
    database = tmp_path / "candidate.sqlite3"
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        with server(tmp_path, database, application_data) as client:
            review = {"expected_version": 1, "proposal_sha256": "a" * 64}
            prefix = "/api/v1/budget-proposals/" + "a" * 32
            requests = [
                ("/api/v1/budget-proposals", budget()),
                (prefix + "/approve", review | {"confirmed": True, "approver": "synthetic"}),
                (
                    prefix + "/reject",
                    review | {"confirmed": True, "approver": "synthetic", "reason": "no"},
                ),
                (prefix + "/simulate", review),
                ("/api/v1/reports", DATES),
                ("/api/v1/report-workflows", DATES),
            ]
            for path, body in requests:
                response = client.post(path, json=body)
                assert response.status_code == 503, response.text
                assert response.json()["code"] == "database_busy"
        with candidate.snapshot() as connection:
            for table in ("documents", "events", "tasks", "operations"):
                assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert not (tmp_path / "reports").exists()


def test_bound_owner_real_tcp_budget_report_and_sqlite_readback(tmp_path, application_data):
    database = tmp_path / "candidate.sqlite3"
    with CandidateOwner(database) as owner:
        CandidateStore(owner).initialize_empty_candidate()
    with server(tmp_path, database, application_data, bound=True) as client:
        response = client.post("/api/v1/budget-proposals", json=budget())
        assert response.status_code == 201, response.text
        proposal = response.json()["data"]
        base = "/api/v1/budget-proposals/" + proposal["id"]
        review = {"expected_version": 1, "proposal_sha256": proposal["proposal_sha256"]}
        assert (
            client.post(
                base + "/approve", json=review | {"confirmed": True, "approver": "synthetic"}
            ).status_code
            == 200
        )
        assert (
            client.post(base + "/simulate", json=review | {"expected_version": 2}).status_code
            == 200
        )
        report = client.post("/api/v1/reports", json=DATES | {"proposal_id": proposal["id"]})
        assert report.status_code == 201, report.text
        report = report.json()["data"]
        assert client.get("/api/v1/reports/" + report["id"]).json()["data"]["content"]
        with pytest.raises(BusinessError, match="database_busy"):
            with Store(database).transaction():
                pytest.fail("second writer admitted")
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.recover_before_serving()
        with candidate.snapshot() as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
            assert Store.load(connection, proposal["id"], "proposal")["version"] == 3
            assert (
                Store.load(connection, report["id"], "report")["content_sha256"]
                == report["content_sha256"]
            )
            assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 4
    assert not list((tmp_path / "reports").glob("*.tmp"))


def test_close_drains_business_action_and_rejects_stale_epoch(tmp_path):
    database = tmp_path / "candidate.sqlite3"
    owner = CandidateOwner(database).acquire()
    candidate = CandidateStore(owner)
    candidate.initialize_empty_candidate()
    legacy = Store(database, owner=owner)
    admitted, release, closing = threading.Event(), threading.Event(), threading.Event()

    def write():
        with legacy.admission():
            admitted.set()
            assert release.wait(5)
            with legacy.transaction() as connection:
                Store.save(connection, "a" * 32, "report", {"synthetic": True}, new=True)

    def close():
        closing.set()
        owner.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write)
        assert admitted.wait(5)
        closer = pool.submit(close)
        assert closing.wait(5)
        assert not closer.done()
        with pytest.raises(BusinessError, match="database_busy"):
            with Store(database).transaction():
                pytest.fail("owner released before drain")
        release.set()
        writer.result(timeout=5)
        closer.result(timeout=5)
    owner.acquire()
    try:
        with pytest.raises(BusinessError, match="database_busy"):
            with legacy.transaction():
                pytest.fail("stale generation admitted")
    finally:
        owner.close()


def test_direct_services_reject_owner_conflict_without_tools_or_reports(tmp_path):
    database = tmp_path / "candidate.sqlite3"
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        business = BusinessService(Store(database), data_path=tmp_path / "does-not-exist.csv")
        reports = ReportService(business, tmp_path / "reports")
        for action in (
            lambda: business.propose_budget_reallocation(BudgetRequest.model_validate(budget())),
            lambda: reports.generate_campaign_report(ReportRequest.model_validate(DATES)),
        ):
            with pytest.raises(BusinessError, match="database_busy"):
                action()
        assert not (tmp_path / "reports").exists()
        with candidate.snapshot() as connection:
            assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_shared_service_guards_task_managed_proposal(tmp_path, application_data):
    database = tmp_path / "candidate.sqlite3"
    with CandidateOwner(database) as owner:
        candidate = CandidateStore(owner)
        candidate.initialize_empty_candidate()
        store = Store(database, owner=owner)
        business = BusinessService(store, data_path=application_data)
        proposal = business.propose_budget_reallocation(BudgetRequest.model_validate(budget()))
        op, _ = candidate.reserve("POST", b"/api/v1/tasks", b"synthetic", digest(budget()))
        task = candidate.create_submitted(
            op["operation_id"], "budget_review", validated_request=budget()
        )
        with candidate.unit_of_work(op["operation_id"]) as connection:
            connection.execute(
                "INSERT INTO task_documents VALUES(?,?,'proposal')", (task, proposal["id"])
            )
        review = ApprovalRequest(
            approver="synthetic",
            confirmed=True,
            expected_version=1,
            proposal_sha256=proposal["proposal_sha256"],
        )
        with pytest.raises(BusinessError, match="task_managed_resource"):
            business.approve_budget_proposal(proposal["id"], review)
        with pytest.raises(BusinessError, match="task_managed_resource"):
            ReportService(business, tmp_path / "reports").generate_campaign_report(
                ReportRequest.model_validate(DATES | {"proposal_id": proposal["id"]})
            )
        with candidate.snapshot() as connection:
            assert Store.load(connection, proposal["id"], "proposal") == proposal
            assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert not (tmp_path / "reports").exists()
