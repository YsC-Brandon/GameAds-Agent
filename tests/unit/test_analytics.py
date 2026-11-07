"""Independently calculated expected values, not outputs copied from implementation."""

import json

import pytest
from pydantic import ValidationError

from app.core.exceptions import CampaignNotFoundError, DataValidationError
from app.tools.analytics_tools import (
    calculate_campaign_metrics,
    compare_campaigns,
    summarize_campaign_performance,
)
from app.tools.campaign_tools import get_campaign_detail, list_campaigns, query_campaigns


def test_all_five_metric_formulas(record) -> None:
    result = calculate_campaign_metrics([record])
    expected = {"ctr": 0.1, "cvr": 0.1, "cpc": 0.5, "cpa": 5, "roas": 2}
    for metric, value in expected.items():
        assert result["metrics"][metric]["status"] == "ok"
        assert result["metrics"][metric]["value"] == pytest.approx(value)
    assert result["totals"]["spend"] == "50.00"
    json.dumps(result, allow_nan=False)


def test_aggregation_uses_sums_not_daily_average(record) -> None:
    rows = [
        record,
        {
            **record,
            "date": "2026-10-02",
            "impressions": 9000,
            "clicks": 180,
            "conversions": 9,
            "spend": "450.00",
            "revenue": "100.00",
        },
    ]
    result = calculate_campaign_metrics(rows)
    expected = {
        "ctr": 280 / 10000,
        "cvr": 19 / 280,
        "cpc": 500 / 280,
        "cpa": 500 / 19,
        "roas": 200 / 500,
    }
    for metric, value in expected.items():
        assert result["metrics"][metric]["value"] == pytest.approx(value)
    assert result["metrics"]["ctr"]["value"] != pytest.approx((0.1 + 0.02) / 2)


def test_zero_denominators_are_null_not_zero(record) -> None:
    row = {**record, "impressions": 0, "clicks": 0, "conversions": 0, "spend": "0"}
    result = calculate_campaign_metrics([row])
    for metric in result["metrics"].values():
        assert metric["value"] is None
        assert metric["status"] == "zero_denominator"


def test_no_conversions_is_zero_cvr_but_null_cpa(record) -> None:
    result = calculate_campaign_metrics([{**record, "conversions": 0}])
    assert result["metrics"]["cvr"]["value"] == 0
    assert result["metrics"]["cpa"]["value"] is None


def test_decimal_money_sum_preserves_cents(record) -> None:
    rows = [{**record, "spend": "0.10"}, {**record, "date": "2026-10-02", "spend": "0.20"}]
    assert calculate_campaign_metrics(rows)["totals"]["spend"] == "0.30"


def test_invalid_clicks_block_affected_metrics(record) -> None:
    result = calculate_campaign_metrics([{**record, "impressions": 10}])
    assert result["status"] == "data_quality_warning"
    for name in ("ctr", "cvr", "cpc"):
        assert result["metrics"][name] == {
            **result["metrics"][name],
            "value": None,
            "status": "invalid_data",
        }
    assert result["metrics"]["roas"]["value"] == 2


def test_conversions_over_clicks_retained_with_attribution_warning(record) -> None:
    result = calculate_campaign_metrics([{**record, "conversions": 110}])
    assert result["metrics"]["cvr"]["value"] == 1.1
    assert result["data_quality_warnings"][0]["code"].startswith("conversions_exceed_clicks")


def test_empty_metric_data_is_explicit() -> None:
    result = calculate_campaign_metrics([])
    assert result["status"] == "no_data"
    assert all(value["status"] == "no_data" for value in result["metrics"].values())


def test_non_list_metric_input_rejected(record) -> None:
    with pytest.raises(DataValidationError):
        calculate_campaign_metrics(record)


def test_inclusive_filter_id_selection_and_pagination(csv_factory, record) -> None:
    rows = [{**record, "date": f"2026-10-{day:02}"} for day in range(1, 4)]
    rows.append({**record, "campaign_id": "B", "campaign_name": "Synthetic B"})
    path = csv_factory(list(reversed(rows)))
    result = query_campaigns("2026-10-01", "2026-10-02", ["A"], limit=1, data_path=path)
    assert result["total_records"] == 2
    assert result["records"][0]["date"] == "2026-10-01"
    assert result["has_more"]
    last = get_campaign_detail("A", "2026-10-01", "2026-10-02", offset=1, data_path=path)
    assert last["records"][0]["date"] == "2026-10-02"
    assert not last["has_more"]
    assert list_campaigns(data_path=path)["campaign_count"] == 2


def test_unknown_id_is_not_silently_skipped(csv_factory, record) -> None:
    path = csv_factory([record])
    with pytest.raises(CampaignNotFoundError) as exc:
        query_campaigns("2026-10-01", "2026-10-02", ["A", "missing"], data_path=path)
    assert exc.value.campaign_ids == ["missing"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 1001},
        {"offset": -1},
        {"campaign_ids": []},
        {"campaign_ids": ["A", "A"]},
        {"limit": True},
        {"offset": 0.5},
    ],
)
def test_query_input_bounds(params) -> None:
    with pytest.raises(ValidationError):
        query_campaigns("2026-10-01", "2026-10-02", **params)


def test_empty_period_and_dataset(csv_factory, record) -> None:
    path = csv_factory([record])
    assert query_campaigns("2020-01-01", "2020-01-01", data_path=path)["status"] == "no_data"
    empty = csv_factory([], "empty.csv")
    assert list_campaigns(data_path=empty)["status"] == "no_data"
    assert query_campaigns("2026-10-01", "2026-10-02", data_path=empty)["status"] == "no_data"
    assert (
        summarize_campaign_performance("2026-10-01", "2026-10-02", data_path=empty)["status"]
        == "no_data"
    )


def test_comparison_excludes_null_values_and_breaks_ties_by_id(csv_factory, record) -> None:
    rows = [
        {**record, "campaign_id": "B"},
        {**record, "campaign_id": "A"},
        {**record, "campaign_id": "C", "spend": "0"},
        {**record, "campaign_id": "D", "revenue": "200"},
    ]
    path = csv_factory(rows)
    result = compare_campaigns("2026-10-01", "2026-10-01", "roas", 3, "asc", data_path=path)
    assert [row["campaign_id"] for row in result["rankings"]] == ["A", "B", "D"]
    assert result["excluded_campaigns"] == [{"campaign_id": "C", "reason": "zero_denominator"}]
    descending = compare_campaigns("2026-10-01", "2026-10-01", "roas", 1, data_path=path)
    assert descending["rankings"][0]["campaign_id"] == "D"


@pytest.mark.parametrize(
    "params", [{"metric": "profit"}, {"top_n": 0}, {"top_n": 101}, {"order": "up"}, {"top_n": True}]
)
def test_comparison_input_bounds(params) -> None:
    with pytest.raises(ValidationError):
        compare_campaigns("2026-10-01", "2026-10-02", **{"metric": "roas", **params})


def test_partial_coverage_is_visible(csv_factory, record) -> None:
    result = summarize_campaign_performance(
        "2026-10-01", "2026-10-03", data_path=csv_factory([record])
    )
    assert result["status"] == "data_quality_warning"
    assert result["campaigns"][0]["coverage"] == {
        "expected_days": 3,
        "observed_days": 1,
        "complete": False,
    }


def test_one_campaign_gap_cannot_be_hidden_by_another_campaign(csv_factory, record) -> None:
    rows = [record, {**record, "campaign_id": "B", "date": "2026-10-02"}]
    result = summarize_campaign_performance("2026-10-01", "2026-10-02", data_path=csv_factory(rows))
    assert result["overall"]["coverage"]["complete"]
    assert result["status"] == "data_quality_warning"
    assert result["overall"]["data_quality_warnings"][0] == {
        "code": "incomplete_campaign_coverage",
        "campaign_ids": ["A", "B"],
    }


def test_comparison_uses_exact_ratios_even_when_float_values_tie(csv_factory, record) -> None:
    rows = [
        {
            **record,
            "campaign_id": "A",
            "spend": "1000000000000000.00",
            "revenue": "1000000000000000.00",
        },
        {
            **record,
            "campaign_id": "B",
            "spend": "1000000000000000.00",
            "revenue": "1000000000000000.01",
        },
    ]
    result = compare_campaigns("2026-10-01", "2026-10-01", "roas", data_path=csv_factory(rows))
    assert [row["campaign_id"] for row in result["rankings"]] == ["B", "A"]


def test_no_computable_ranking_is_explicit(csv_factory, record) -> None:
    result = compare_campaigns(
        "2026-10-01", "2026-10-01", "roas", data_path=csv_factory([{**record, "spend": "0"}])
    )
    assert result["status"] == "no_computable_values"
    assert result["rankings"] == []


def test_empty_ranking_is_explicit(csv_factory) -> None:
    result = compare_campaigns("2026-10-01", "2026-10-01", "roas", data_path=csv_factory([]))
    assert result["status"] == "no_data"
