"""Keep the packaged synthetic fixture identical to the accepted source dataset."""

from pathlib import Path

import app
from app.core.config import PROJECT_ROOT
from app.services.campaign_service import DEFAULT_DATA_PATH


def test_packaged_synthetic_data_matches_source_bytes():
    packaged = Path(app.__file__).parent / "resources" / "sample_campaigns.csv"
    assert packaged.read_bytes() == (PROJECT_ROOT / "data" / "sample_campaigns.csv").read_bytes()
    assert DEFAULT_DATA_PATH == PROJECT_ROOT / "data" / "sample_campaigns.csv"
