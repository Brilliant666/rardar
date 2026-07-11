"""Validate a Codex enrichment draft before atomically publishing it."""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pipeline.data_lock import locked_data_dir
from pipeline.schema_validation import (
    ArtifactKind,
    atomic_write_validated_json,
    require_valid,
    strict_json_loads,
)


EnrichmentKind = Literal["project", "signal"]


def _safe_name(repository: str) -> str:
    return re.sub(
        r"[^a-z0-9-]+",
        "-",
        repository.lower().replace("/", "--"),
    ).strip("-")


def _read_draft(path: Path) -> dict[str, Any]:
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"cannot read enrichment draft {path}: {error}") from None
    if not isinstance(payload, dict):
        raise ValueError(f"enrichment draft must be a JSON object: {path}")
    return payload


@locked_data_dir
def ingest_enrichment(
    data_dir: Path,
    kind: EnrichmentKind,
    draft_path: Path,
) -> Path:
    """Publish one validated draft without exposing a partial official file."""
    draft_path = draft_path.expanduser().resolve()
    if draft_path == data_dir or data_dir in draft_path.parents:
        raise ValueError(
            "the input must be a draft outside the official data directory; "
            "drafts inside data cannot preserve the publication boundary"
        )

    payload = _read_draft(draft_path)

    if kind == "project":
        require_valid(ArtifactKind.PROJECT_ENRICHMENT, payload)
        if payload.get("schemaVersion") != 1:
            raise ValueError(
                "only project enrichment v1 drafts may be published; "
                "legacy v0 is read-only compatibility data"
            )
        analyzed_at = datetime.fromisoformat(
            str(payload["analyzedAt"]).replace("Z", "+00:00")
        )
        source_analysis_at = datetime.fromisoformat(
            str(payload["sourceAnalysisAt"]).replace("Z", "+00:00")
        )
        if analyzed_at < source_analysis_at:
            raise ValueError(
                "project enrichment analyzedAt cannot precede sourceAnalysisAt"
            )
        repository = payload.get("repository")
        if not isinstance(repository, str):  # Kept explicit for type checkers.
            raise ValueError("project enrichment repository must be a string")
        target = data_dir / "enrichment" / f"{_safe_name(repository)}.json"
        artifact_kind = ArtifactKind.PROJECT_ENRICHMENT
        expected_repository: str | None = repository
    elif kind == "signal":
        require_valid(ArtifactKind.SIGNAL_ENRICHMENT, payload)
        target = data_dir / "signals" / "enrichment.json"
        artifact_kind = ArtifactKind.SIGNAL_ENRICHMENT
        expected_repository = None
    else:
        raise ValueError(f"unsupported enrichment kind: {kind}")

    target = target.resolve()

    atomic_write_validated_json(
        target,
        artifact_kind,
        payload,
        expected_repository=expected_repository,
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and atomically publish a Codex enrichment draft"
    )
    parser.add_argument("--kind", choices=("project", "signal"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args()

    target = ingest_enrichment(arguments.data_dir, arguments.kind, arguments.input)
    print(target)


if __name__ == "__main__":
    main()
