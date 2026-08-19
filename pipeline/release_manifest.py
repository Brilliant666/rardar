"""Create a release manifest inside an isolated CI staging directory."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pipeline.release_artifact import (
    BUILDER_OS,
    MANIFEST_NAME,
    NODE_VERSION,
    PYTHON_WHEEL_VERSION,
    REPOSITORY,
    SCHEMA_VERSION,
    TARGET_ARCHITECTURE,
    TARGET_PLATFORM,
    ReleaseArtifactError,
    sha256_file,
    validate_manifest_payload,
)


def _canonical_built_at(value: str) -> str:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ReleaseArtifactError("FAIL_RELEASE_ARTIFACT_MANIFEST", "builtAt must be RFC3339") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseArtifactError("FAIL_RELEASE_ARTIFACT_MANIFEST", "builtAt must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def create_release_manifest(
    release_root: Path,
    *,
    commit_sha: str,
    verify_workflow_run_id: str,
    verify_workflow_head_sha: str,
    npm_version: str,
    built_at: str,
) -> dict[str, object]:
    root = Path(release_root).resolve(strict=True)
    payload: dict[str, object] = {
        "schemaVersion": SCHEMA_VERSION,
        "repository": REPOSITORY,
        "commitSha": commit_sha,
        "platform": TARGET_PLATFORM,
        "architecture": TARGET_ARCHITECTURE,
        "builderOs": BUILDER_OS,
        "nodeVersion": NODE_VERSION,
        "npmVersion": npm_version,
        "pythonWheelVersion": PYTHON_WHEEL_VERSION,
        "packageLockSha256": sha256_file(root / "package-lock.json"),
        "requirementsLockSha256": sha256_file(root / "requirements.lock"),
        "verifyWorkflowRunId": verify_workflow_run_id,
        "verifyWorkflowHeadSha": verify_workflow_head_sha,
        "builtAt": _canonical_built_at(built_at),
    }
    validate_manifest_payload(payload, expected_sha=commit_sha)
    destination = root / MANIFEST_NAME
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(destination, flags, 0o644)
    except FileExistsError as error:
        raise ReleaseArtifactError(
            "FAIL_RELEASE_ARTIFACT_MANIFEST",
            f"release manifest already exists: {destination}",
        ) from error
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an exact Rardar release manifest")
    parser.add_argument("--release-root", required=True, type=Path)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--verify-workflow-run-id", required=True)
    parser.add_argument("--verify-workflow-head-sha", required=True)
    parser.add_argument("--npm-version", required=True)
    parser.add_argument("--built-at", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        payload = create_release_manifest(
            arguments.release_root,
            commit_sha=arguments.commit_sha,
            verify_workflow_run_id=arguments.verify_workflow_run_id,
            verify_workflow_head_sha=arguments.verify_workflow_head_sha,
            npm_version=arguments.npm_version,
            built_at=arguments.built_at,
        )
    except (OSError, ReleaseArtifactError) as error:
        detail = error.as_dict() if isinstance(error, ReleaseArtifactError) else {
            "code": "FAIL_RELEASE_ARTIFACT_MANIFEST",
            "detail": str(error),
        }
        print(json.dumps({"schemaVersion": SCHEMA_VERSION, "status": "failed", "error": detail}, indent=2))
        return 1
    print(json.dumps({"schemaVersion": SCHEMA_VERSION, "status": "created", "manifest": payload}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
