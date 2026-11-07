"""Supported local-only HTTP launch. Never trust forwarded proxy headers."""

import argparse
import os
from pathlib import Path

import uvicorn

from app.http_transport import LocalHTTPProtocol, check_transport_versions


def serve(application="app.main:app", *, port: int = 8000) -> None:
    """Only network launch boundary, also used by isolated integration fixtures."""
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    check_transport_versions()
    uvicorn.run(
        application,
        host="127.0.0.1",
        port=port,
        http=LocalHTTPProtocol,
        proxy_headers=False,
        access_log=False,
        ws="none",
        workers=1,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--instance-root", type=Path, help="Existing installed instance root")
    parser.add_argument(
        "--database", type=Path, help="Explicit preinitialized v1 candidate database"
    )
    parser.add_argument(
        "--reports-dir", type=Path, help="Fixed absolute candidate report directory"
    )
    args = parser.parse_args()
    if args.instance_root is not None:
        if not args.instance_root.is_absolute():
            parser.error("--instance-root must be absolute")
        os.environ["GAMEADS_INSTANCE_ROOT"] = str(args.instance_root)
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535.")
    if args.database is None:
        if args.reports_dir is not None:
            parser.error("--reports-dir requires --database")
        if os.environ.get("GAMEADS_INSTANCE_ROOT"):
            from app.main import create_app

            serve(create_app(), port=args.port)
        else:
            serve(port=args.port)
        return
    if args.reports_dir is None or not args.reports_dir.is_absolute():
        parser.error("Candidate launch requires an absolute --reports-dir")
    from app.main import create_app
    from app.phase4.ownership import CandidateOwner
    from app.phase4.store import CandidateStore

    with CandidateOwner(args.database) as owner:
        candidate = CandidateStore(owner)
        candidate.recover_before_serving()
        serve(
            create_app(
                business_db_path=args.database,
                reports_dir=args.reports_dir,
                data_path=owner.generation_directory / "dataset.csv"
                if owner.generation_directory is not None
                else None,
                storage_owner=owner,
            ),
            port=args.port,
        )


if __name__ == "__main__":
    main()
