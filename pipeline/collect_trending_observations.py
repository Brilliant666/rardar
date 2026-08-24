"""CLI for one deterministic two-hour GitHub trending observation phase."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from pipeline.trending_observations import (
    DEFAULT_LIMIT,
    SCHEDULE_TIMEZONE,
    TrendingObservationError,
    nearest_scheduled_phase,
    parse_scheduled_at,
    run_observer,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record one append-only GitHub trending observation capture"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--scheduled-at",
        help="timezone-aware RFC3339 timestamp for an exact two-hour phase",
    )
    parser.add_argument("--timezone", default=SCHEDULE_TIMEZONE)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        scheduled_at = (
            parse_scheduled_at(arguments.scheduled_at)
            if arguments.scheduled_at
            else nearest_scheduled_phase(datetime.now(timezone.utc))
        )
        result = run_observer(
            data_dir=arguments.data_dir,
            scheduled_at=scheduled_at,
            timezone_name=arguments.timezone,
            limit=arguments.limit,
            dry_run=arguments.dry_run,
            token=os.environ.get("GITHUB_TOKEN"),
        )
    except TrendingObservationError as error:
        print(
            json.dumps(
                {"state": "failed", "error": error.as_dict()},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
