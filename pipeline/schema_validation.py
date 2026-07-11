"""Versioned JSON Schema validation for Rardar data artifacts.

Schemas validate the shape of one file. Cross-file facts such as counts,
freshness, and exact Star deltas remain the responsibility of ``audit_data``.
Validation is read-only and never fills defaults or rewrites payloads.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from pipeline.data_lock import data_dir_lock


CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"


class ArtifactKind(str, Enum):
    GITHUB_SNAPSHOT = "github-snapshot"
    TECHNICAL_SIGNALS = "technical-signals"
    STATIC_EVIDENCE = "static-evidence"
    PROJECT_ENRICHMENT = "project-enrichment"
    SIGNAL_ENRICHMENT = "signal-enrichment"
    CATALOG = "catalog"
    CODEX_QUEUE = "codex-queue"


SCHEMA_FILES = {
    ArtifactKind.GITHUB_SNAPSHOT: "github-snapshot.schema.json",
    ArtifactKind.TECHNICAL_SIGNALS: "technical-signals.schema.json",
    ArtifactKind.STATIC_EVIDENCE: "static-evidence.schema.json",
    ArtifactKind.PROJECT_ENRICHMENT: "project-enrichment.schema.json",
    ArtifactKind.SIGNAL_ENRICHMENT: "signal-enrichment.schema.json",
    ArtifactKind.CATALOG: "catalog.schema.json",
    ArtifactKind.CODEX_QUEUE: "codex-queue.schema.json",
}


FORMAT_CHECKER = FormatChecker()
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


@FORMAT_CHECKER.checks("date-time")
def _is_rfc3339(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if not RFC3339_PATTERN.fullmatch(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


@FORMAT_CHECKER.checks("http-url")
def _is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return True
    if (
        not value
        or len(value) > 2048
        or any(character == "\\" or character.isspace() or ord(character) < 32 for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing ``port`` is what makes urllib reject malformed or
        # out-of-range ports instead of silently carrying them forward.
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(hostname and hostname.strip("."))
        and parsed.username is None
        and parsed.password is None
    )


@FORMAT_CHECKER.checks("repository")
def _is_repository(value: object) -> bool:
    return not isinstance(value, str) or bool(REPOSITORY_PATTERN.fullmatch(value))


@dataclass(frozen=True)
class ValidationIssue:
    message: str
    instance_path: str
    schema_path: str
    source_path: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    kind: ArtifactKind
    version: int | None
    issues: tuple[ValidationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues


class ArtifactValidationError(ValueError):
    def __init__(self, result: ValidationResult):
        self.result = result
        details = "; ".join(
            f"{issue.instance_path}: {issue.message}" for issue in result.issues[:5]
        )
        if len(result.issues) > 5:
            details += f"; and {len(result.issues) - 5} more"
        super().__init__(f"{result.kind.value} schema validation failed: {details}")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON object key: {key}")
        payload[key] = value
    return payload


def strict_json_loads(value: str) -> Any:
    """Parse JSON while rejecting duplicate keys and non-finite numbers."""
    return json.loads(
        value,
        parse_constant=_reject_constant,
        object_pairs_hook=_object_without_duplicates,
    )


def strict_json_dumps(payload: object, *, indent: int = 2) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=indent, allow_nan=False)


def _json_pointer(parts: Iterable[object]) -> str:
    encoded = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(encoded) if encoded else "/"


def _bounded_message(value: object, limit: int = 1000) -> str:
    message = str(value)
    return message if len(message) <= limit else message[:limit] + "…"


def _safe_repository_filename(repository: str) -> str:
    return re.sub(
        r"[^a-z0-9-]+",
        "-",
        repository.lower().replace("/", "--"),
    ).strip("-")


@lru_cache(maxsize=None)
def _validator(kind: ArtifactKind) -> Draft202012Validator:
    schema_path = CONTRACTS_DIR / SCHEMA_FILES[kind]
    schema = strict_json_loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FORMAT_CHECKER)


def _non_finite_issues(payload: object, path: tuple[object, ...] = ()) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if isinstance(payload, float) and not math.isfinite(payload):
        issues.append(
            ValidationIssue(
                message="number must be finite",
                instance_path=_json_pointer(path),
                schema_path="/finite-number",
            )
        )
    elif isinstance(payload, dict):
        for key, value in payload.items():
            issues.extend(_non_finite_issues(value, (*path, key)))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            issues.extend(_non_finite_issues(value, (*path, index)))
    return issues


def validate_payload(
    kind: ArtifactKind | str,
    payload: object,
    *,
    source_path: Path | None = None,
    expected_repository: str | None = None,
) -> ValidationResult:
    artifact_kind = ArtifactKind(kind)
    issues = _non_finite_issues(payload)
    schema_errors = sorted(
        _validator(artifact_kind).iter_errors(payload),
        key=lambda error: (_json_pointer(error.absolute_path), error.message),
    )
    issues.extend(
        ValidationIssue(
            message=_bounded_message(error.message),
            instance_path=_json_pointer(error.absolute_path),
            schema_path=_json_pointer(error.absolute_schema_path),
            source_path=str(source_path) if source_path else None,
        )
        for error in schema_errors
    )

    repository = payload.get("repository") if isinstance(payload, dict) else None
    if expected_repository is not None and repository != expected_repository:
        issues.append(
            ValidationIssue(
                message=(
                    f"repository {repository!r} does not match expected "
                    f"{expected_repository!r}"
                ),
                instance_path="/repository",
                schema_path="/identity/repository",
                source_path=str(source_path) if source_path else None,
            )
        )
    if (
        source_path is not None
        and artifact_kind in {ArtifactKind.STATIC_EVIDENCE, ArtifactKind.PROJECT_ENRICHMENT}
        and source_path.parent.name.lower() in {"analysis", "enrichment"}
        and isinstance(repository, str)
        and repository != "local"
        and source_path.stem != _safe_repository_filename(repository)
    ):
        issues.append(
            ValidationIssue(
                message=(
                    f"repository {repository!r} maps to "
                    f"{_safe_repository_filename(repository)!r}, not file {source_path.stem!r}"
                ),
                instance_path="/repository",
                schema_path="/identity/file-name",
                source_path=str(source_path),
            )
        )
    if (
        source_path is not None
        and artifact_kind is ArtifactKind.STATIC_EVIDENCE
        and source_path.parent.name.lower() == "analysis"
        and repository == "local"
    ):
        issues.append(
            ValidationIssue(
                message="committed static evidence must identify a GitHub owner/name repository",
                instance_path="/repository",
                schema_path="/identity/committed-repository",
                source_path=str(source_path),
            )
        )
    if (
        source_path is not None
        and artifact_kind is ArtifactKind.GITHUB_SNAPSHOT
        and source_path.name.lower() == "latest.json"
        and source_path.parent.name.lower() == "snapshots"
        and isinstance(payload, dict)
    ):
        for field in ("query_status", "successful_query_count", "failed_query_count"):
            if field not in payload:
                issues.append(
                    ValidationIssue(
                        message=f"latest GitHub snapshot requires {field!r}",
                        instance_path=f"/{field}",
                        schema_path="/identity/latest-snapshot-required",
                        source_path=str(source_path),
                    )
                )

    normalized_issues = tuple(
        ValidationIssue(
            message=issue.message,
            instance_path=issue.instance_path,
            schema_path=issue.schema_path,
            source_path=issue.source_path or (str(source_path) if source_path else None),
        )
        for issue in issues
    )
    version: int | None = None
    if isinstance(payload, dict):
        raw_version = payload.get("schemaVersion", payload.get("schema_version"))
        if isinstance(raw_version, int) and not isinstance(raw_version, bool):
            version = raw_version
    return ValidationResult(artifact_kind, version, normalized_issues)


def require_valid(
    kind: ArtifactKind | str,
    payload: object,
    *,
    source_path: Path | None = None,
    expected_repository: str | None = None,
) -> dict[str, Any]:
    result = validate_payload(
        kind,
        payload,
        source_path=source_path,
        expected_repository=expected_repository,
    )
    if not result.valid:
        raise ArtifactValidationError(result)
    if not isinstance(payload, dict):
        raise TypeError("validated Rardar artifacts must be JSON objects")
    return payload


def infer_artifact_kind(path: Path) -> ArtifactKind | None:
    path = Path(path)
    name = path.name.lower()
    parent = path.parent.name.lower()
    grandparent = path.parent.parent.name.lower()
    if parent == "snapshots" and name == "latest.json":
        return ArtifactKind.GITHUB_SNAPSHOT
    if parent == "history" and grandparent == "snapshots" and name.endswith(".json"):
        return ArtifactKind.GITHUB_SNAPSHOT
    if parent == "signals" and name == "latest.json":
        return ArtifactKind.TECHNICAL_SIGNALS
    if parent == "signals" and name == "enrichment.json":
        return ArtifactKind.SIGNAL_ENRICHMENT
    if parent == "analysis" and name.endswith(".json"):
        return ArtifactKind.STATIC_EVIDENCE
    if parent == "enrichment" and name.endswith(".json"):
        return ArtifactKind.PROJECT_ENRICHMENT
    if parent == "catalog" and name == "latest.json":
        return ArtifactKind.CATALOG
    if parent == "queues" and name == "codex.json":
        return ArtifactKind.CODEX_QUEUE
    return None


def artifact_data_root(path: Path) -> Path | None:
    """Return the canonical data directory owning a recognized artifact path."""
    resolved = Path(path).expanduser().resolve()
    kind = infer_artifact_kind(resolved)
    if kind is None:
        return None
    if (
        kind is ArtifactKind.GITHUB_SNAPSHOT
        and resolved.parent.name.lower() == "history"
    ):
        return resolved.parent.parent.parent
    return resolved.parent.parent


@contextmanager
def artifact_write_lock(path: Path) -> Iterator[None]:
    """Serialize a standalone writer when its target belongs to a data tree."""
    data_root = artifact_data_root(path)
    if data_root is None:
        yield
        return
    with data_dir_lock(data_root):
        yield


def require_valid_for_path(path: Path, payload: object) -> dict[str, Any]:
    kind = infer_artifact_kind(path)
    if kind is None:
        if not isinstance(payload, dict):
            raise TypeError("JSON batch payloads must be objects")
        return payload
    return require_valid(kind, payload, source_path=path)


def atomic_write_validated_json(
    path: Path,
    kind: ArtifactKind | str,
    payload: object,
    *,
    expected_repository: str | None = None,
) -> dict[str, Any]:
    """Validate and atomically replace one JSON artifact.

    A recognized canonical output path is reserved for its inferred artifact
    kind. Serialization and validation finish before the destination directory
    or temporary file is touched, so failures leave an existing artifact
    unchanged.
    """
    path = Path(path)
    artifact_kind = ArtifactKind(kind)
    inferred_kind = infer_artifact_kind(path)
    if inferred_kind is not None and inferred_kind != artifact_kind:
        raise ValueError(
            f"output path {path} is reserved for {inferred_kind.value}, "
            f"not {artifact_kind.value}"
        )

    validated = require_valid(
        artifact_kind,
        payload,
        source_path=path,
        expected_repository=expected_repository,
    )
    serialized = strict_json_dumps(validated) + "\n"

    # Canonical writers hold the owning data-directory lock while calling this
    # helper.  Reject an older completed payload inside that short critical
    # section so slow collection or analysis cannot overwrite newer facts.
    _reject_stale_or_conflicting_replacement(path, artifact_kind, validated)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return validated


def _artifact_timestamp(kind: ArtifactKind, payload: dict[str, Any]) -> datetime | None:
    field = {
        ArtifactKind.GITHUB_SNAPSHOT: "captured_at",
        ArtifactKind.TECHNICAL_SIGNALS: "capturedAt",
        ArtifactKind.STATIC_EVIDENCE: "analyzed_at",
        ArtifactKind.PROJECT_ENRICHMENT: "analyzedAt",
        ArtifactKind.SIGNAL_ENRICHMENT: "generatedAt",
        ArtifactKind.CATALOG: "capturedAt",
        ArtifactKind.CODEX_QUEUE: "generatedAt",
    }[kind]
    value = payload.get(field)
    if not isinstance(value, str) or not _is_rfc3339(value):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _reject_stale_or_conflicting_replacement(
    path: Path,
    kind: ArtifactKind,
    candidate: dict[str, Any],
) -> None:
    if not path.exists():
        return
    try:
        existing_raw = strict_json_loads(path.read_text(encoding="utf-8"))
        existing = require_valid(kind, existing_raw, source_path=path)
    except (OSError, ValueError):
        # A valid candidate may repair a corrupt current file. Semantic audit
        # remains responsible for deciding whether that repair is publishable.
        return

    if kind in {ArtifactKind.STATIC_EVIDENCE, ArtifactKind.PROJECT_ENRICHMENT}:
        existing_repository = existing.get("repository")
        candidate_repository = candidate.get("repository")
        if existing_repository != candidate_repository:
            raise ValueError(
                f"output path {path} already belongs to repository "
                f"{existing_repository!r}, not {candidate_repository!r}"
            )

    existing_time = _artifact_timestamp(kind, existing)
    candidate_time = _artifact_timestamp(kind, candidate)
    if existing_time is not None and (
        candidate_time is None or candidate_time < existing_time
    ):
        raise ValueError(
            f"refusing to replace newer {kind.value} at {path}: "
            f"candidate timestamp {candidate_time!s}, existing {existing_time!s}"
        )


def load_validated_json(
    path: Path,
    kind: ArtifactKind | str | None = None,
    *,
    expected_repository: str | None = None,
) -> dict[str, Any]:
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"invalid JSON artifact {path}: {error}") from None
    artifact_kind = ArtifactKind(kind) if kind is not None else infer_artifact_kind(path)
    if artifact_kind is None:
        raise ValueError(f"cannot infer Rardar artifact kind from {path}")
    return require_valid(
        artifact_kind,
        payload,
        source_path=path,
        expected_repository=expected_repository,
    )


def validate_data_tree(data_dir: Path) -> list[ValidationResult]:
    """Validate every supported artifact without running semantic audits."""
    data_dir = data_dir.resolve()
    required_paths = [
        data_dir / "snapshots" / "latest.json",
        data_dir / "signals" / "latest.json",
        data_dir / "catalog" / "latest.json",
        data_dir / "queues" / "codex.json",
    ]
    optional_paths = [
        *sorted((data_dir / "snapshots" / "history").glob("*.json")),
        *sorted((data_dir / "analysis").glob("*.json")),
        *sorted((data_dir / "enrichment").glob("*.json")),
    ]
    signal_enrichment = data_dir / "signals" / "enrichment.json"
    if signal_enrichment.exists():
        optional_paths.append(signal_enrichment)

    results: list[ValidationResult] = []
    for path in required_paths:
        kind = infer_artifact_kind(path)
        if not path.exists() and kind is not None:
            results.append(
                ValidationResult(
                    kind=kind,
                    version=None,
                    issues=(
                        ValidationIssue(
                            message="required artifact is missing",
                            instance_path="/",
                            schema_path="/required-artifact",
                            source_path=str(path),
                        ),
                    ),
                )
            )
    for path in [*required_paths, *optional_paths]:
        if not path.exists():
            continue
        kind = infer_artifact_kind(path)
        if kind is None:
            continue
        try:
            payload = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as error:
            results.append(
                ValidationResult(
                    kind=kind,
                    version=None,
                    issues=(
                        ValidationIssue(
                            message=_bounded_message(f"invalid JSON: {error}"),
                            instance_path="/",
                            schema_path="/json",
                            source_path=str(path),
                        ),
                    ),
                )
            )
            continue
        results.append(validate_payload(kind, payload, source_path=path))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Rardar JSON artifacts against contracts")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    arguments = parser.parse_args()
    results = validate_data_tree(arguments.data_dir)
    issues = [
        {
            "kind": result.kind.value,
            "version": result.version,
            "sourcePath": issue.source_path,
            "instancePath": issue.instance_path,
            "schemaPath": issue.schema_path,
            "message": issue.message,
        }
        for result in results
        for issue in result.issues
    ]
    report = {
        "schemaVersion": 1,
        "status": "failed" if issues else "healthy",
        "validatedCount": len(results),
        "errorCount": len(issues),
        "issues": issues,
    }
    print(strict_json_dumps(report))
    raise SystemExit(1 if issues else 0)


if __name__ == "__main__":
    main()
