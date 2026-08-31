"""CLI for audited near-real-time Discover publication."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from pipeline.trending_discover import (
    TrendingDiscoverError,
    derive_trending_discover,
    resolve_current_discover,
    rollback_discover,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Derive or inspect versioned TrendingDiscoverArtifact generations"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    subparsers = parser.add_subparsers(dest="command")
    derive = subparsers.add_parser("derive")
    derive.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("status")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("generation_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    command = arguments.command or "derive"
    try:
        if command == "derive":
            result = derive_trending_discover(
                arguments.data_dir, dry_run=bool(getattr(arguments, "dry_run", False))
            )
        elif command == "rollback":
            result = rollback_discover(arguments.data_dir, arguments.generation_id)
        else:
            current = resolve_current_discover(arguments.data_dir)
            result = {
                "state": "healthy",
                "generationId": current.generation_id,
                "latestCaptureId": current.artifact["latestCaptureId"],
                "coverage": current.artifact["coverage"],
            }
    except (OSError, TrendingDiscoverError, ValueError) as error:
        result = {
            "state": "failed",
            "errorCode": getattr(error, "code", "discover_command_failed"),
            "stage": getattr(error, "stage", "command"),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
