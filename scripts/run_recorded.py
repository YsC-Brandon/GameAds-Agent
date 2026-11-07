"""Run a local check with immutable logs and an explicit completion record."""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("name")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    base = args.directory / args.name
    start = datetime.now(UTC).isoformat()
    with base.with_suffix(".stdout.log").open("xb") as stdout:
        with base.with_suffix(".stderr.log").open("xb") as stderr:
            result = subprocess.run(args.command, stdout=stdout, stderr=stderr, check=False)
    with base.with_suffix(".result.json").open("x", encoding="utf-8") as output:
        json.dump(
            {
                "command": args.command,
                "started_utc": start,
                "finished_utc": datetime.now(UTC).isoformat(),
                "exit_code": result.returncode,
            },
            output,
            indent=2,
        )


if __name__ == "__main__":
    main()
