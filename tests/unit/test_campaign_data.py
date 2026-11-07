"""Data integrity and repeatability checks; no network or LLM credentials needed."""

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.exceptions import DataSourceError, DataValidationError
from app.schemas import DateRange
from app.services.campaign_service import load_campaign_data, quality_warnings, validate_records
from scripts.generate_demo_data import generate_records, write_demo_data


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("impressions", -1),
        ("clicks", -1),
        ("conversions", -1),
        ("spend", "-0.01"),
        ("revenue", "-1"),
        ("spend", "NaN"),
        ("revenue", "Infinity"),
        ("clicks", 1.5),
        ("impressions", True),
        ("spend", False),
        ("spend", "1.001"),
        ("currency", "EUR"),
        ("date", "2026-02-30"),
        ("date", "2026-1-01"),
        ("date", "2026-10-01T00:00:00"),
        ("date", 1790812800),
        ("date", datetime(2026, 10, 1)),
        ("campaign_id", "  "),
        ("revenue", None),
        ("clicks", ""),
        ("clicks", "NA"),
    ],
)
def test_invalid_records_fail_explicitly(record, field, value) -> None:
    record[field] = value
    with pytest.raises(DataValidationError) as exc:
        validate_records([record])
    assert exc.value.issues[0]["record"] == 1
    assert exc.value.issues[0]["field"] == [field]


def test_missing_field_does_not_silently_become_zero(record) -> None:
    del record["clicks"]
    with pytest.raises(DataValidationError):
        validate_records([record])


def test_duplicate_campaign_day_is_rejected(record) -> None:
    with pytest.raises(DataValidationError, match="Dataset rejected") as exc:
        validate_records([record, record])
    assert exc.value.issues == [{"record": 2, "code": "duplicate_campaign_date"}]


def test_campaign_metadata_must_be_consistent(record) -> None:
    changed = {**record, "date": "2026-10-02", "platform": "Different"}
    with pytest.raises(DataValidationError) as exc:
        validate_records([record, changed])
    assert exc.value.issues[0]["code"] == "inconsistent_campaign_metadata"


def test_questionable_counts_are_kept_and_flagged(record) -> None:
    records = validate_records([{**record, "impressions": 10, "clicks": 20, "conversions": 30}])
    assert records[0].conversions == 30
    assert {warning["code"] for warning in quality_warnings(records)} == {
        "clicks_exceed_impressions",
        "conversions_exceed_clicks_attribution_review",
    }


@pytest.mark.parametrize("start,end", [("2026-10-02", "2026-10-01"), ("not-a-date", "2026-10-01")])
def test_invalid_period_is_rejected(start, end) -> None:
    with pytest.raises(ValidationError):
        DateRange(start_date=start, end_date=end)


@pytest.mark.parametrize("content", ["", "date,campaign_id\n", "date,date\n", "not a CSV"])
def test_bad_headers_are_rejected(tmp_path, content) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(DataValidationError):
        load_campaign_data(path)


def test_csv_missing_cell_is_rejected(csv_factory, record) -> None:
    path = csv_factory([{**record, "clicks": ""}])
    with pytest.raises(DataValidationError):
        load_campaign_data(path)


def test_csv_ragged_row_is_rejected(csv_factory, record) -> None:
    path = csv_factory([record])
    with path.open("a", encoding="utf-8") as stream:
        stream.write("broken,row\n")
    with pytest.raises(DataValidationError) as exc:
        load_campaign_data(path)
    assert exc.value.issues[0]["code"] == "invalid_column_count"


def test_header_only_csv_has_explicit_empty_result(csv_factory) -> None:
    assert load_campaign_data(csv_factory([])) == []


def test_missing_file_is_readable_error(tmp_path) -> None:
    with pytest.raises(DataSourceError, match="Cannot read"):
        load_campaign_data(tmp_path / "missing.csv")


def test_bad_encoding_is_readable_error(tmp_path) -> None:
    path = tmp_path / "bad.csv"
    path.write_bytes(b"\xff\xff\xff")
    with pytest.raises(DataSourceError):
        load_campaign_data(path)


def test_oversized_file_rejected_before_reading(tmp_path) -> None:
    path = tmp_path / "big.csv"
    with path.open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(DataSourceError, match="10 MiB"):
        load_campaign_data(path)


def test_generator_is_repeatable_and_complete() -> None:
    rows = generate_records()
    assert rows == generate_records()
    assert rows != generate_records(seed=43)
    assert len(rows) == 240
    assert len({row["campaign_id"] for row in rows}) == 8
    assert len({row["date"] for row in rows}) == 30
    assert len({row["game_genre"] for row in rows}) == 8
    assert {row["currency"] for row in rows} == {"USD"}
    assert not quality_warnings(validate_records(rows))


def test_generator_bytes_and_overwrite_guard(tmp_path: Path) -> None:
    first, second = tmp_path / "one.csv", tmp_path / "two.csv"
    assert write_demo_data(first) == 240
    write_demo_data(second)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(FileExistsError):
        write_demo_data(first)
    assert write_demo_data(first, force=True) == 240
    assert len(load_campaign_data(first)) == 240


def test_csv_with_bom_loads(csv_factory, record) -> None:
    path = csv_factory([record])
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert len(load_campaign_data(path)) == 1


def test_wrong_extension_rejected(tmp_path) -> None:
    path = tmp_path / "input.txt"
    path.write_text("data", encoding="utf-8")
    with pytest.raises(DataSourceError):
        load_campaign_data(path)


def test_row_limit_enforced(csv_factory, record, monkeypatch) -> None:
    monkeypatch.setattr("app.services.campaign_service.MAX_CSV_ROWS", 1)
    path = csv_factory([record, {**record, "date": "2026-10-02"}])
    with pytest.raises(DataSourceError, match="record"):
        load_campaign_data(path)


def test_invalid_record_instance_cannot_bypass_validation(record) -> None:
    from app.schemas import CampaignRecord

    invalid = CampaignRecord.model_construct(**{**record, "spend": "-1"})
    with pytest.raises(DataValidationError):
        validate_records([invalid])
