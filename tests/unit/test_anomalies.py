"""Independently constructed periods exercise each rule and evidence gate."""

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.core.config import AnomalyThresholds, Settings
from app.tools.anomaly_tools import detect_campaign_anomalies


def periods(record: dict, current_changes: dict | None = None, baseline_changes=None) -> list[dict]:
    baseline = {
        **record,
        "impressions": 10000,
        "clicks": 1000,
        "conversions": 100,
        "spend": "100.00",
        "revenue": "200.00",
        **(baseline_changes or {}),
    }
    current = {**baseline, **(current_changes or {})}
    return [
        {
            **(baseline if day < 2 else current),
            "date": (date(2026, 10, 1) + timedelta(days=day)).isoformat(),
        }
        for day in range(4)
    ]


def screen(csv_factory, rows: list[dict], thresholds=None) -> dict:
    return detect_campaign_anomalies(
        "2026-10-03",
        "2026-10-04",
        baseline_days=2,
        data_path=csv_factory(rows),
        thresholds=thresholds or AnomalyThresholds(),
    )


def rule(result: dict, name: str) -> dict:
    return next(item for item in result["campaigns"][0]["rules"] if item["rule"] == name)


def test_periods_and_ctr_drop_are_calculated_from_data(csv_factory, record) -> None:
    result = screen(csv_factory, periods(record, {"clicks": 600}))
    ctr = rule(result, "ctr_drop")
    assert result["baseline_period"] == {
        "start_date": "2026-10-01",
        "end_date": "2026-10-02",
        "inclusive": True,
    }
    assert ctr["status"] == "triggered"
    assert ctr["current_value"] == pytest.approx(0.06)
    assert ctr["baseline_value"] == pytest.approx(0.10)
    assert ctr["relative_change"] == pytest.approx(-0.40)
    assert result["campaigns"][0]["current"]["row_count"] == 2


def test_cpa_rise_rule(csv_factory, record) -> None:
    result = screen(csv_factory, periods(record, {"conversions": 50}))
    cpa = rule(result, "cpa_increase")
    assert cpa["status"] == "triggered"
    assert cpa["baseline_value"] == 1
    assert cpa["current_value"] == 2
    assert cpa["relative_change"] == 1


def test_roas_floor_rule(csv_factory, record) -> None:
    result = screen(csv_factory, periods(record, {"revenue": "50.00"}))
    roas = rule(result, "roas_below_floor")
    assert roas["status"] == "triggered"
    assert roas["current_value"] == 0.5
    assert not roas["baseline_required"]


@pytest.mark.parametrize(
    "changes,name",
    [
        ({"clicks": 700}, "ctr_drop"),
        ({"spend": "130.00"}, "cpa_increase"),
        ({"revenue": "100.00"}, "roas_below_floor"),
    ],
)
def test_exact_threshold_does_not_trigger_strict_rule(csv_factory, record, changes, name) -> None:
    assert rule(screen(csv_factory, periods(record, changes)), name)["status"] == "not_triggered"


def test_low_sample_is_not_reported_as_clear(csv_factory, record) -> None:
    changes = {"impressions": 20, "clicks": 2, "conversions": 0, "spend": "1", "revenue": "0"}
    result = screen(csv_factory, periods(record, changes))
    assert result["campaigns"][0]["status"] == "insufficient_evidence"
    assert all(
        item["status"] == "insufficient_evidence" for item in result["campaigns"][0]["rules"]
    )
    assert "current:insufficient_impressions" in rule(result, "ctr_drop")["evidence_reasons"]


def test_no_baseline_blocks_relative_rules_only(csv_factory, record) -> None:
    result = screen(csv_factory, periods(record, {"revenue": "50.00"})[2:])
    assert rule(result, "ctr_drop")["status"] == "insufficient_evidence"
    assert rule(result, "cpa_increase")["status"] == "insufficient_evidence"
    assert rule(result, "roas_below_floor")["status"] == "triggered"
    assert rule(result, "ctr_drop")["relative_change"] is None


def test_partial_baseline_is_visible(csv_factory, record) -> None:
    rows = periods(record)
    result = screen(csv_factory, rows[1:])
    assert "baseline:incomplete_date_coverage" in rule(result, "ctr_drop")["evidence_reasons"]


def test_partial_current_period_is_not_screened(csv_factory, record) -> None:
    result = screen(csv_factory, periods(record)[:-1])
    assert result["campaigns"][0]["status"] == "insufficient_evidence"
    assert (
        "current:incomplete_date_coverage" in rule(result, "roas_below_floor")["evidence_reasons"]
    )


def test_baseline_only_campaign_is_visible(csv_factory, record) -> None:
    result = screen(csv_factory, periods(record)[:2])
    assert result["status"] == "no_data"
    assert len(result["campaigns"]) == 1
    assert result["campaigns"][0]["status"] == "insufficient_evidence"


def test_zero_baseline_does_not_produce_infinity(csv_factory, record) -> None:
    result = screen(csv_factory, periods(record, {"spend": "100"}, {"spend": "0"}))
    cpa = rule(result, "cpa_increase")
    assert cpa["relative_change"] is None
    assert cpa["status"] == "insufficient_evidence"
    assert "baseline:zero_baseline_value" in cpa["evidence_reasons"]


def test_data_quality_warning_requires_review(csv_factory, record) -> None:
    result = screen(csv_factory, periods(record, {"clicks": 11000}))
    assert result["campaigns"][0]["current"]["data_quality_warnings"]
    assert all(
        item["status"] == "insufficient_evidence" for item in result["campaigns"][0]["rules"]
    )


def test_empty_periods_return_no_data(csv_factory) -> None:
    result = screen(csv_factory, [])
    assert result["status"] == "no_data"
    assert result["campaigns"] == []


def test_configurable_threshold_changes_result(csv_factory, record) -> None:
    thresholds = AnomalyThresholds(ctr_drop_fraction=0.50)
    result = screen(csv_factory, periods(record, {"clicks": 600}), thresholds)
    assert rule(result, "ctr_drop")["status"] == "not_triggered"
    assert result["thresholds"]["ctr_drop_fraction"] == 0.50


def test_nested_environment_threshold_override(monkeypatch) -> None:
    monkeypatch.setenv("GAMEADS_ANOMALY__CTR_DROP_FRACTION", "0.45")
    assert Settings(_env_file=None).anomaly.ctr_drop_fraction == 0.45


@pytest.mark.parametrize(
    "values",
    [
        {"ctr_drop_fraction": -0.1},
        {"ctr_drop_fraction": 1.1},
        {"cpa_increase_fraction": float("nan")},
        {"roas_floor": float("inf")},
        {"min_clicks": 0},
        {"min_conversions": -1},
        {"min_impressions": 0},
    ],
)
def test_invalid_thresholds(values) -> None:
    with pytest.raises(ValidationError):
        AnomalyThresholds(**values)


@pytest.mark.parametrize("days", [0, -1, 366, True])
def test_baseline_days_bounds(days) -> None:
    with pytest.raises(ValidationError):
        detect_campaign_anomalies("2026-10-24", "2026-10-30", days)


def test_baseline_date_cannot_underflow() -> None:
    with pytest.raises(ValidationError):
        detect_campaign_anomalies("0001-01-01", "0001-01-02")
