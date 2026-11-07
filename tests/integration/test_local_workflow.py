"""Run public tools and CLI against actual generated CSV data, without an LLM."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT, AnomalyThresholds
from app.services.campaign_service import DEFAULT_DATA_PATH, load_campaign_data
from app.tools.analytics_tools import compare_campaigns, summarize_campaign_performance
from app.tools.anomaly_tools import detect_campaign_anomalies
from scripts.generate_demo_data import generate_records, write_demo_data


def test_included_csv_matches_reproducible_generator(tmp_path: Path) -> None:
    generated = tmp_path / "generated.csv"
    write_demo_data(generated)
    assert DEFAULT_DATA_PATH.read_bytes() == generated.read_bytes()
    assert len(load_campaign_data()) == len(generate_records()) == 240


def test_local_workflow_repeatability_and_synthetic_patterns() -> None:
    first = compare_campaigns("2026-10-24", "2026-10-30", "roas", 3, "asc")
    assert first == compare_campaigns("2026-10-24", "2026-10-30", "roas", 3, "asc")
    assert len(first["rankings"]) == 3
    summary = summarize_campaign_performance("2026-10-01", "2026-10-30")
    assert summary["overall"]["row_count"] == 240
    assert summary["overall"]["campaign_count"] == 8
    result = detect_campaign_anomalies("2026-10-24", "2026-10-30", thresholds=AnomalyThresholds())
    assert result == detect_campaign_anomalies(
        "2026-10-24", "2026-10-30", thresholds=AnomalyThresholds()
    )
    campaigns = {row["campaign_id"]: row for row in result["campaigns"]}
    ctr = next(item for item in campaigns["C006"]["rules"] if item["rule"] == "ctr_drop")
    cpa = next(item for item in campaigns["C007"]["rules"] if item["rule"] == "cpa_increase")
    assert ctr["status"] == cpa["status"] == "triggered"
    assert campaigns["C008"]["status"] == "insufficient_evidence"
    for output in (first, summary, result):
        json.dumps(output, allow_nan=False)


@pytest.mark.parametrize(
    "command,extra",
    [
        ("list", []),
        ("query", ["--campaign-id", "C001", "--limit", "2"]),
        ("detail", ["--campaign-id", "C001"]),
        ("metrics", []),
        ("compare", ["--metric", "roas", "--top-n", "3", "--order", "asc"]),
        ("anomalies", []),
    ],
)
def test_cli_commands_outside_project(tmp_path: Path, command, extra) -> None:
    args = [sys.executable, "-m", "app", command]
    if command != "list":
        args += ["--start-date", "2026-10-24", "--end-date", "2026-10-30"]
    result = subprocess.run(args + extra, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads(result.stdout)
    assert output["status"] != "error"
    assert not result.stderr


@pytest.mark.parametrize(
    "extra,code",
    [
        (["query", "--start-date", "bad-date", "--end-date", "2026-10-01"], "invalid_arguments"),
        (
            [
                "detail",
                "--campaign-id",
                "unknown",
                "--start-date",
                "2026-10-01",
                "--end-date",
                "2026-10-01",
            ],
            "campaign_not_found",
        ),
        (["--data-path", "missing.csv", "list"], "data_source_error"),
    ],
)
def test_cli_errors_are_structured(extra, code) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app", *extra], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["code"] == code
    assert "Traceback" not in result.stderr


def test_generator_cli_wont_overwrite_existing_file(tmp_path: Path) -> None:
    output = tmp_path / "test.csv"
    args = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "generate_demo_data.py"),
        "--output",
        str(output),
    ]
    first = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    original = output.read_bytes()
    second = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert second.returncode == 1
    assert "--force" in second.stderr
    assert output.read_bytes() == original
