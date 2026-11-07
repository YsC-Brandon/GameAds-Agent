"""Generate reproducible synthetic campaign-days; never query an advertising platform."""

import argparse
import csv
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.services.campaign_service import CSV_COLUMNS, DEFAULT_DATA_PATH, validate_records

DEFAULT_SEED = 42
START_DATE = date(2026, 10, 1)
DAYS = 30
# CTR, CVR, CPC (USD), revenue per conversion (USD). These are synthetic profiles.
PROFILES = (
    ("RPG", 0.035, 0.12, 0.45, 9.0),
    ("Strategy", 0.028, 0.10, 0.50, 8.0),
    ("Puzzle", 0.025, 0.08, 0.55, 7.0),
    ("Casual", 0.020, 0.06, 0.60, 6.0),
    ("Racing", 0.015, 0.04, 0.70, 5.0),
    ("Simulation", 0.030, 0.09, 0.50, 7.0),
    ("Action", 0.025, 0.10, 0.55, 7.0),
    ("Sports", 0.020, 0.08, 0.60, 6.0),
)


def generate_records(seed: int = DEFAULT_SEED) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for day in range(DAYS):
        for index, (genre, ctr, cvr, cpc, value) in enumerate(PROFILES, start=1):
            impressions = rng.randint(8000, 16000)
            # The last week contains deliberate, documented test patterns, not findings.
            if day >= 23 and index == 6:
                ctr *= 0.45
            if day >= 23 and index == 7:
                cvr *= 0.40
            if day >= 23 and index == 8:
                impressions = rng.randint(20, 60)
            clicks = int(impressions * ctr * rng.uniform(0.9, 1.1))
            conversions = int(clicks * cvr * rng.uniform(0.9, 1.1))
            spend = Decimal(str(clicks * cpc * rng.uniform(0.9, 1.1))).quantize(Decimal("0.01"))
            revenue = Decimal(str(conversions * value * rng.uniform(0.9, 1.1))).quantize(
                Decimal("0.01")
            )
            rows.append(
                {
                    "date": (START_DATE + timedelta(days=day)).isoformat(),
                    "campaign_id": f"C{index:03}",
                    "campaign_name": f"Synthetic {genre} {index:02}",
                    "platform": ("DemoNetwork-A", "DemoNetwork-B")[index % 2],
                    "game_genre": genre,
                    "impressions": impressions,
                    "clicks": clicks,
                    "conversions": conversions,
                    "spend": str(spend),
                    "revenue": str(revenue),
                    "currency": "USD",
                }
            )
    validate_records(rows)
    return rows


def write_demo_data(
    path: Path = DEFAULT_DATA_PATH, *, seed: int = DEFAULT_SEED, force=False
) -> int:
    rows = generate_records(seed)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w" if force else "x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_DATA_PATH)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--force", action="store_true", help="Explicitly overwrite an existing file."
    )
    args = parser.parse_args()
    try:
        count = write_demo_data(args.output, seed=args.seed, force=args.force)
    except FileExistsError:
        parser.exit(1, "Output exists; choose another path or explicitly use --force.\n")
    except OSError:
        parser.exit(1, "Cannot write the requested local CSV path.\n")
    print(f"Generated {count} synthetic campaign-days (USD, seed={args.seed}).")
    print("Synthetic demonstration data only; not real advertising performance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
