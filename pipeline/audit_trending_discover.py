"""Read-only CLI audit for versioned TrendingDiscoverArtifact generations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from pipeline.trending_discover import audit_discover_store


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit the current Discover generation")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args(argv)
    report = audit_discover_store(arguments.data_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") in {"healthy", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
