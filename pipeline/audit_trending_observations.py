"""Read-only audit CLI for the append-only trending observation store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from pipeline.trending_observations import (
    TrendingObservationError,
    audit_observation_store,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit append-only GitHub trending observation captures"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args(argv)
    try:
        report = audit_observation_store(arguments.data_dir)
    except TrendingObservationError as error:
        report = {
            "schemaVersion": 1,
            "status": "failed",
            "captureCount": 0,
            "observationCount": 0,
            "earliestCapturedAt": None,
            "latestCapturedAt": None,
            "eligibleCaptureCount": 0,
            "degradedCaptureCount": 0,
            "issueCount": 1,
            "issues": [error.as_dict()],
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] in {"healthy", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
