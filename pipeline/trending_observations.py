"""Append-only GitHub trending observations for Rardar v2.

This module records raw repository metadata observations.  It intentionally
does not derive 24-hour deltas, rank projects, run AI, publish generations, or
write D1.  Every capture is an immutable, self-digested fact bundle addressed
by one fixed two-hour schedule phase.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import urllib.error
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from zoneinfo import ZoneInfo

from pipeline.collect_github import GitHubClient, candidate_queries
from pipeline.data_lock import _try_lock, _unlock, data_dir_lock_path
from pipeline.schema_validation import (
    ArtifactKind,
    ArtifactValidationError,
    require_valid,
    strict_json_loads,
)
from pipeline.stable_read import StableReadError, stable_read


POLICY_VERSION = "trending-observation-v1"
SCHEMA_VERSION = 1
SCHEDULE_TIMEZONE = "Asia/Shanghai"
CADENCE_MINUTES = 120
WINDOW_TOLERANCE_SECONDS = 600
TRACKING_WINDOW_HOURS = 26
DEFAULT_LIMIT = 500
RETENTION_DAYS = 45
LEGACY_RETENTION_DAYS = 90
SUPPORTED_RETENTION_DAYS = frozenset({RETENTION_DAYS, LEGACY_RETENTION_DAYS})
CAPTURE_ID_PATTERN = re.compile(r"^trending-v1-(\d{8})T(\d{6})Z$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CAPTURE_FILE_PATTERN = re.compile(r"^trending-v1-\d{8}T\d{6}Z\.json$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TEMPORARY_FILE_PATTERN = re.compile(r"^\..+\.tmp$")
_CREATE_SETTLEMENT_MAX_ATTEMPTS = 4
_CREATE_SETTLEMENT_BACKOFF_SECONDS = (0.005, 0.01, 0.02)
_RETRYABLE_GITHUB_HTTP_CODES = frozenset({408, 429, *range(500, 600)})


class TrendingObservationError(RuntimeError):
    """A stable, redacted error from collection, persistence, or audit."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            **({"details": self.details} if self.details else {}),
        }


class ObserverAlreadyRunningError(TrendingObservationError):
    def __init__(
        self,
        lock_path: Path,
        existing_owner: dict[str, Any] | None,
    ) -> None:
        self.lock_path = lock_path
        self.existing_owner = existing_owner
        super().__init__(
            "observer_already_running",
            "another trending observer already owns this data directory",
        )


class TrendingGitHubClient(Protocol):
    def search_response(
        self,
        query: str,
        *,
        per_page: int = 100,
        page: int = 1,
    ) -> dict[str, Any]: ...

    def repository(self, github_repository_id: int) -> dict[str, Any]: ...


@dataclass
class _Candidate:
    github_repository_id: int
    repository: str
    recalled_by: list[dict[str, Any]]
    carry_captured_at: datetime | None = None
    search_order: tuple[int, int] | None = None
    search_names: set[str] | None = None

    def __post_init__(self) -> None:
        if self.search_names is None:
            self.search_names = set()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TrendingObservationError(
            "timezone_required",
            "timestamp must include an explicit timezone",
        )
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    canonical = _utc(value).isoformat(timespec="microseconds")
    return canonical.replace("+00:00", "Z")


def parse_timestamp(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise TrendingObservationError(
            "invalid_timestamp",
            f"{field} must be a timezone-aware RFC3339 timestamp",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TrendingObservationError(
            "invalid_timestamp",
            f"{field} must be a timezone-aware RFC3339 timestamp",
        ) from None
    return _utc(parsed)


def parse_scheduled_at(value: str) -> datetime:
    scheduled = parse_timestamp(value, field="scheduledAt")
    _assert_fixed_phase(scheduled)
    return scheduled


def _assert_fixed_phase(scheduled_at: datetime) -> None:
    local = _utc(scheduled_at).astimezone(ZoneInfo(SCHEDULE_TIMEZONE))
    if (
        local.hour % 2 != 0
        or local.minute != 0
        or local.second != 0
        or local.microsecond != 0
    ):
        raise TrendingObservationError(
            "invalid_schedule_phase",
            "scheduledAt must be an exact two-hour Asia/Shanghai phase",
        )


def nearest_scheduled_phase(now: datetime) -> datetime:
    local = _utc(now).astimezone(ZoneInfo(SCHEDULE_TIMEZONE))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (local - midnight).total_seconds()
    cadence = CADENCE_MINUTES * 60
    lower_index = int(elapsed // cadence)
    lower = midnight + timedelta(seconds=lower_index * cadence)
    upper = lower + timedelta(seconds=cadence)
    selected = lower if (local - lower) <= (upper - local) else upper
    return selected.astimezone(timezone.utc)


def capture_id_for_scheduled_at(scheduled_at: datetime) -> str:
    scheduled = _utc(scheduled_at)
    _assert_fixed_phase(scheduled)
    return f"trending-v1-{scheduled.strftime('%Y%m%dT%H%M%SZ')}"


def _absolute_without_escape(path: Path, *, label: str) -> Path:
    expanded = Path(path).expanduser()
    if ".." in expanded.parts:
        raise TrendingObservationError(
            "unsafe_observation_path",
            f"{label} contains a parent traversal component",
        )
    absolute = Path(os.path.abspath(expanded))
    # Compare filesystem object types component-by-component instead of comparing
    # ``resolve`` text.  Windows may legitimately expand an 8.3 short path to
    # its long spelling even when no link exists.
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise TrendingObservationError(
                "unsafe_observation_path",
                f"{label} cannot be inspected safely: {error}",
            ) from None
        if _is_reparse(metadata):
            raise TrendingObservationError(
                "unsafe_observation_path",
                f"{label} traverses a symbolic link or reparse point",
            )
    return absolute


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        int(getattr(metadata, "st_file_attributes", 0)) & flag
    )


def _assert_safe_existing(path: Path, *, expect_directory: bool | None = None) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise TrendingObservationError(
            "unsafe_observation_path",
            f"observation path cannot be inspected: {path}: {error}",
        ) from None
    if _is_reparse(metadata):
        raise TrendingObservationError(
            "unsafe_observation_path",
            f"observation path is a symbolic link or reparse point: {path}",
        )
    if expect_directory is True and not stat.S_ISDIR(metadata.st_mode):
        raise TrendingObservationError(
            "unsafe_observation_path",
            f"observation directory is not a regular directory: {path}",
        )
    if expect_directory is False and not stat.S_ISREG(metadata.st_mode):
        raise TrendingObservationError(
            "unsafe_observation_path",
            f"observation file is not a regular file: {path}",
        )


def observation_root(data_dir: Path) -> Path:
    canonical = _absolute_without_escape(data_dir, label="data directory")
    _assert_safe_existing(canonical, expect_directory=True)
    return canonical / "observations" / "trending" / "v1" / "captures"


def capture_path_for_scheduled_at(data_dir: Path, scheduled_at: datetime) -> Path:
    scheduled = _utc(scheduled_at)
    capture_id = capture_id_for_scheduled_at(scheduled)
    return (
        observation_root(data_dir)
        / scheduled.strftime("%Y")
        / scheduled.strftime("%m")
        / scheduled.strftime("%d")
        / f"{capture_id}.json"
    )


def _ensure_capture_parent(data_dir: Path, target: Path) -> None:
    data_root = _absolute_without_escape(data_dir, label="data directory")
    target = _absolute_without_escape(target, label="capture target")
    try:
        target.relative_to(data_root)
    except ValueError:
        raise TrendingObservationError(
            "unsafe_observation_path",
            "capture target escapes the configured data directory",
        ) from None

    chain: list[Path] = []
    current = target.parent
    while current != data_root.parent:
        chain.append(current)
        if current == data_root:
            break
        current = current.parent
    if not chain or chain[-1] != data_root:
        raise TrendingObservationError(
            "unsafe_observation_path",
            "capture target is not owned by the configured data directory",
        )
    for directory in reversed(chain):
        _assert_safe_existing(directory, expect_directory=True)
        try:
            directory.mkdir(exist_ok=True)
        except OSError as error:
            raise TrendingObservationError(
                "observation_directory_create_failed",
                f"capture directory could not be created: {directory}: {error}",
            ) from None
        _assert_safe_existing(directory, expect_directory=True)
    _assert_safe_existing(target, expect_directory=False)


def canonical_json_bytes(payload: object) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise TrendingObservationError(
            "non_canonical_json",
            f"payload cannot be canonically serialized: {error}",
        ) from None


def compute_bundle_digest(payload: dict[str, Any]) -> str:
    digestless = copy.deepcopy(payload)
    digestless.pop("digest", None)
    return hashlib.sha256(canonical_json_bytes(digestless)).hexdigest()


def attach_bundle_digest(payload: dict[str, Any]) -> dict[str, Any]:
    prepared = copy.deepcopy(payload)
    prepared["digest"] = {
        "algorithm": "sha256",
        "value": compute_bundle_digest(prepared),
    }
    return prepared


def _validate_repository_semantics(observation: dict[str, Any]) -> None:
    repository = observation["repository"]
    expected_url = f"https://github.com/{repository}"
    if observation["htmlUrl"] != expected_url:
        raise TrendingObservationError(
            "repository_url_mismatch",
            "observation htmlUrl does not exactly match its repository",
        )
    recalled = observation["recalledBy"]
    keys = [canonical_json_bytes(item) for item in recalled]
    if len(keys) != len(set(keys)):
        raise TrendingObservationError(
            "duplicate_recall_provenance",
            "observation contains duplicate recall provenance",
        )


def validate_observation(observation: object) -> dict[str, Any]:
    try:
        validated = require_valid(ArtifactKind.TRENDING_OBSERVATION, observation)
    except (ArtifactValidationError, TypeError, ValueError) as error:
        raise TrendingObservationError(
            "invalid_trending_observation",
            f"TrendingObservation failed Schema validation: {error}",
        ) from None
    _validate_repository_semantics(validated)
    return validated


def _expected_queries(scheduled_at: datetime) -> list[str]:
    return candidate_queries(_utc(scheduled_at))


def validate_capture_bundle(
    payload: object,
    *,
    expected_capture_id: str | None = None,
    expected_path: Path | None = None,
) -> dict[str, Any]:
    try:
        bundle = require_valid(ArtifactKind.TRENDING_CAPTURE_BUNDLE, payload)
    except (ArtifactValidationError, TypeError, ValueError) as error:
        raise TrendingObservationError(
            "invalid_capture_schema",
            f"TrendingCaptureBundle failed Schema validation: {error}",
        ) from None

    scheduled = parse_timestamp(bundle["scheduledAt"], field="scheduledAt")
    captured = parse_timestamp(bundle["capturedAt"], field="capturedAt")
    _assert_fixed_phase(scheduled)
    expected_id = capture_id_for_scheduled_at(scheduled)
    if bundle["captureId"] != expected_id or (
        expected_capture_id is not None and bundle["captureId"] != expected_capture_id
    ):
        raise TrendingObservationError(
            "capture_identity_mismatch",
            "captureId does not match policyVersion and scheduledAt",
        )
    if expected_path is not None:
        path = _absolute_without_escape(expected_path, label="capture path")
        expected_suffix = Path(
            scheduled.strftime("%Y/%m/%d")
        ) / f"{expected_id}.json"
        if Path(*path.parts[-4:]) != expected_suffix:
            raise TrendingObservationError(
                "capture_path_mismatch",
                "capture directory or filename does not match scheduledAt and captureId",
            )

    delay = (captured - scheduled).total_seconds()
    if abs(float(bundle["captureDelaySeconds"]) - delay) > 0.000001:
        raise TrendingObservationError(
            "capture_delay_mismatch",
            "captureDelaySeconds does not match capturedAt minus scheduledAt",
        )
    if bundle["windowEligible"] is not (abs(delay) <= WINDOW_TOLERANCE_SECONDS):
        raise TrendingObservationError(
            "window_eligibility_mismatch",
            "windowEligible does not match the ten-minute schedule window",
        )

    query_status = bundle["queryStatus"]
    expected_queries = _expected_queries(scheduled)
    expected_ids = [f"query-{index:02d}" for index in range(1, 10)]
    if [item["queryId"] for item in query_status] != expected_ids or [
        item["query"] for item in query_status
    ] != expected_queries:
        raise TrendingObservationError(
            "query_policy_mismatch",
            "queryStatus does not contain the ordered nine-query recall policy",
        )
    successful = sum(item["state"] == "healthy" for item in query_status)
    failed = len(query_status) - successful
    if (
        bundle["successfulQueryCount"] != successful
        or bundle["failedQueryCount"] != failed
    ):
        raise TrendingObservationError(
            "query_count_mismatch",
            "query success and failure counts do not match queryStatus",
        )

    observations = bundle["observations"]
    failures = bundle["metadataFailures"]
    if bundle["observationCount"] != len(observations):
        raise TrendingObservationError(
            "observation_count_mismatch",
            "observationCount does not match observations",
        )
    if bundle["metadataFailureCount"] != len(failures):
        raise TrendingObservationError(
            "metadata_failure_count_mismatch",
            "metadataFailureCount does not match metadataFailures",
        )
    if bundle["candidateCount"] != len(observations) + len(failures):
        raise TrendingObservationError(
            "candidate_count_mismatch",
            "candidateCount must equal observations plus metadata failures",
        )

    observation_ids: set[int] = set()
    repositories: dict[str, int] = {}
    query_by_id = {item["queryId"]: item for item in query_status}
    for observation in observations:
        validate_observation(observation)
        repository_id = observation["githubRepositoryId"]
        repository_key = observation["repository"].casefold()
        if repository_id in observation_ids:
            raise TrendingObservationError(
                "duplicate_repository_identity",
                "a capture contains a duplicate GitHub repository ID",
            )
        if repository_key in repositories and repositories[repository_key] != repository_id:
            raise TrendingObservationError(
                "repository_name_identity_collision",
                "one repository name maps to multiple GitHub repository IDs",
            )
        observation_ids.add(repository_id)
        repositories[repository_key] = repository_id
        if parse_timestamp(observation["capturedAt"], field="observation.capturedAt") != captured:
            raise TrendingObservationError(
                "observation_capture_time_mismatch",
                "observation capturedAt must equal its bundle capturedAt",
            )
        for source in observation["recalledBy"]:
            source_time = parse_timestamp(source["capturedAt"], field="recalledBy.capturedAt")
            if source_time > captured:
                raise TrendingObservationError(
                    "future_recall_provenance",
                    "recall provenance cannot be newer than its observation",
                )
            if source["source"] == "github_search":
                query = query_by_id.get(source["queryId"])
                if (
                    source["sourceKey"] != source["queryId"]
                    or query is None
                    or query["state"] != "healthy"
                    or source["query"] != query["query"]
                    or source["sourceRank"] > query["resultCount"]
                ):
                    raise TrendingObservationError(
                        "query_provenance_mismatch",
                        "GitHub Search provenance does not match a healthy query result",
                    )
            else:
                match = CAPTURE_ID_PATTERN.fullmatch(source["sourceKey"])
                if match is None:
                    raise TrendingObservationError(
                        "carry_forward_provenance_mismatch",
                        "carry-forward sourceKey must be a prior capture ID",
                    )
                source_schedule = datetime.strptime(
                    "".join(match.groups()), "%Y%m%d%H%M%S"
                ).replace(tzinfo=timezone.utc)
                try:
                    _assert_fixed_phase(source_schedule)
                except TrendingObservationError:
                    raise TrendingObservationError(
                        "carry_forward_provenance_mismatch",
                        "carry-forward sourceKey must identify a fixed two-hour phase",
                    ) from None
                if source_schedule >= scheduled:
                    raise TrendingObservationError(
                        "carry_forward_provenance_mismatch",
                        "carry-forward provenance must identify an earlier phase",
                    )
    failure_ids = [item["githubRepositoryId"] for item in failures]
    if len(failure_ids) != len(set(failure_ids)) or observation_ids.intersection(failure_ids):
        raise TrendingObservationError(
            "duplicate_candidate_outcome",
            "each selected candidate must have exactly one metadata outcome",
        )

    degraded = (
        failed > 0
        or any(item["incompleteResults"] for item in query_status)
        or bool(failures)
    )
    expected_coverage = "degraded" if degraded else "healthy"
    if bundle["coverageState"] != expected_coverage:
        raise TrendingObservationError(
            "coverage_state_mismatch",
            "coverageState does not match query and metadata outcomes",
        )

    retention_days = bundle["retention"].get("retentionDays")
    if type(retention_days) is not int or retention_days not in SUPPORTED_RETENTION_DAYS:
        raise TrendingObservationError(
            "retention_mismatch",
            "retentionDays must use the current 45-day policy or the historical 90-day policy",
        )
    retain_until = parse_timestamp(
        bundle["retention"]["retainUntil"], field="retention.retainUntil"
    )
    if retain_until != captured + timedelta(days=retention_days):
        raise TrendingObservationError(
            "retention_mismatch",
            "retainUntil must exactly match capturedAt plus retentionDays",
        )
    digest = bundle["digest"]
    if digest["algorithm"] != "sha256" or not SHA256_PATTERN.fullmatch(digest["value"]):
        raise TrendingObservationError("invalid_capture_digest", "capture digest is invalid")
    if compute_bundle_digest(bundle) != digest["value"]:
        raise TrendingObservationError(
            "capture_digest_mismatch",
            "capture digest does not match canonical payload bytes",
        )
    return bundle


def _decode_capture(content: bytes, path: Path) -> dict[str, Any]:
    try:
        text = content.decode("utf-8", errors="strict")
        payload = strict_json_loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise TrendingObservationError(
            "invalid_capture_json",
            f"capture is not strict UTF-8 JSON: {path}: {error}",
        ) from None
    return validate_capture_bundle(
        payload,
        expected_capture_id=path.stem,
        expected_path=path,
    )


def load_capture(path: Path) -> dict[str, Any]:
    path = _absolute_without_escape(path, label="capture path")
    _assert_safe_existing(path.parent, expect_directory=True)
    _assert_safe_existing(path, expect_directory=False)
    try:
        snapshot = stable_read(path)
    except StableReadError as error:
        raise TrendingObservationError(
            "unsafe_or_unstable_capture",
            f"capture could not be read as stable regular bytes: {path}: {error.reason}",
        ) from None
    return _decode_capture(snapshot.content, path)


def _walk_capture_paths(data_dir: Path) -> list[Path]:
    root = observation_root(data_dir)
    if not os.path.lexists(root):
        return []
    _assert_safe_existing(root, expect_directory=True)
    paths: list[Path] = []

    def visit(directory: Path, depth: int) -> None:
        _assert_safe_existing(directory, expect_directory=True)
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise TrendingObservationError(
                "observation_store_unreadable",
                f"observation directory cannot be listed: {directory}: {error}",
            ) from None
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise TrendingObservationError(
                    "observation_store_unreadable",
                    f"observation entry cannot be inspected: {path}: {error}",
                ) from None
            if _is_reparse(metadata):
                raise TrendingObservationError(
                    "unsafe_observation_path",
                    f"observation store contains a symbolic link or reparse point: {path}",
                )
            if TEMPORARY_FILE_PATTERN.fullmatch(entry.name):
                raise TrendingObservationError(
                    "temporary_file_residual",
                    f"observation store contains a residual temporary file: {path}",
                )
            if depth < 3:
                expected = (r"^\d{4}$", r"^(0[1-9]|1[0-2])$", r"^(0[1-9]|[12]\d|3[01])$")[depth]
                if not stat.S_ISDIR(metadata.st_mode) or not re.fullmatch(expected, entry.name):
                    raise TrendingObservationError(
                        "unexpected_observation_entry",
                        f"unexpected observation directory entry: {path}",
                    )
                visit(path, depth + 1)
            else:
                if not stat.S_ISREG(metadata.st_mode) or not CAPTURE_FILE_PATTERN.fullmatch(entry.name):
                    raise TrendingObservationError(
                        "unexpected_observation_entry",
                        f"unexpected observation capture entry: {path}",
                    )
                paths.append(path)

    visit(root, 0)
    return paths


def _load_recent_candidates(
    data_dir: Path,
    scheduled_at: datetime,
    *,
    limit: int,
) -> list[_Candidate]:
    cutoff = _utc(scheduled_at) - timedelta(hours=TRACKING_WINDOW_HOURS)
    recent: list[tuple[datetime, dict[str, Any]]] = []
    for path in _walk_capture_paths(data_dir):
        match = CAPTURE_ID_PATTERN.fullmatch(path.stem)
        if match is None:
            # The directory walker already enforces the filename pattern; keep
            # this defensive branch fail-closed if the two policies drift.
            raise TrendingObservationError(
                "capture_identity_mismatch",
                f"capture filename cannot be parsed: {path.name}",
            )
        file_slot = datetime.strptime(
            "".join(match.groups()), "%Y%m%d%H%M%S"
        ).replace(tzinfo=timezone.utc)
        if file_slot < cutoff or file_slot >= _utc(scheduled_at):
            continue
        bundle = load_capture(path)
        bundle_scheduled = parse_timestamp(bundle["scheduledAt"], field="scheduledAt")
        bundle_captured = parse_timestamp(bundle["capturedAt"], field="capturedAt")
        if bundle_scheduled != file_slot:
            raise TrendingObservationError(
                "capture_identity_mismatch",
                "capture scheduledAt does not match its filename phase",
            )
        recent.append((bundle_captured, bundle))
    recent.sort(key=lambda item: (item[0], item[1]["captureId"]))

    candidates: dict[int, _Candidate] = {}
    for bundle_captured, bundle in recent:
        for rank, observation in enumerate(bundle["observations"], start=1):
            repository_id = observation["githubRepositoryId"]
            provenance = {
                "source": "recent_observation_carry_forward",
                "sourceKey": bundle["captureId"],
                "sourceRank": rank,
                "capturedAt": bundle["capturedAt"],
            }
            current = candidates.get(repository_id)
            if current is None:
                current = _Candidate(
                    github_repository_id=repository_id,
                    repository=observation["repository"],
                    recalled_by=[],
                )
                candidates[repository_id] = current
            current.repository = observation["repository"]
            current.carry_captured_at = bundle_captured
            current.recalled_by.append(provenance)
    if len(candidates) > limit:
        raise TrendingObservationError(
            "tracking_capacity_exceeded",
            f"{len(candidates)} repositories remain in the 26-hour tracking window, exceeding limit {limit}",
            details={"carryForwardCount": len(candidates), "limit": limit},
        )
    return sorted(
        candidates.values(),
        key=lambda item: (
            -(item.carry_captured_at or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
            item.github_repository_id,
        ),
    )


def _sanitize_error(error: BaseException, *, token: str | None) -> tuple[str, str]:
    if isinstance(error, urllib.error.HTTPError):
        code = f"github_http_{error.code}"
        message = f"GitHub API returned HTTP {error.code}"
    elif isinstance(error, (TimeoutError, urllib.error.URLError)):
        code = "github_network_error"
        message = "GitHub API request failed or timed out"
    elif isinstance(error, TrendingObservationError):
        code = error.code
        message = str(error)
    else:
        code = re.sub(r"[^a-z0-9]+", "_", type(error).__name__.lower()).strip("_")
        code = f"github_{code or 'request_error'}"
        message = str(error) or "GitHub API request failed"
    if token:
        message = message.replace(token, "[REDACTED]")
    message = re.sub(r"(?i)(bearer|token)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]", message)
    return code[:100], message[:300]


def _all_github_failures_are_retryable(error_codes: list[str]) -> bool:
    if not error_codes:
        return False
    for code in error_codes:
        if code == "github_network_error":
            continue
        match = re.fullmatch(r"github_http_(\d{3})", code)
        if match is None or int(match.group(1)) not in _RETRYABLE_GITHUB_HTTP_CODES:
            return False
    return True


def observation_error_retryable(error: TrendingObservationError) -> bool:
    """Return whether one failed phase may use its single bounded retry."""

    return error.code in {
        "all_candidate_queries_failed",
        "all_repository_metadata_failed",
    } and error.details.get("retryable") is True


def _search_response(
    client: TrendingGitHubClient,
    query: str,
) -> tuple[list[dict[str, Any]], bool]:
    payload = client.search_response(query, per_page=100, page=1)
    if not isinstance(payload, dict):
        raise TrendingObservationError(
            "invalid_search_response",
            "GitHub Search response must be an object",
        )
    items = payload.get("items")
    incomplete = payload.get("incomplete_results", False)
    if not isinstance(items, list) or not isinstance(incomplete, bool):
        raise TrendingObservationError(
            "invalid_search_response",
            "GitHub Search response has invalid items or incomplete_results",
        )
    normalized: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise TrendingObservationError(
                "invalid_search_result",
                "GitHub Search returned a non-object repository item",
            )
        repository_id = item.get("id")
        repository = item.get("full_name")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
            or not isinstance(repository, str)
            or not REPOSITORY_PATTERN.fullmatch(repository)
        ):
            raise TrendingObservationError(
                "invalid_search_result",
                "GitHub Search returned an invalid repository identity",
            )
        normalized.append({"id": repository_id, "repository": repository})
    return normalized, incomplete


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TrendingObservationError(
            "invalid_repository_metadata",
            f"GitHub repository metadata field {key} must be a non-negative integer",
        )
    return value


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise TrendingObservationError(
            "invalid_repository_metadata",
            f"GitHub repository metadata field {key} must be a non-empty string",
        )
    return value


def _required_bool(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise TrendingObservationError(
            "invalid_repository_metadata",
            f"GitHub repository metadata field {key} must be a boolean",
        )
    return value


def _normalize_repository_metadata(
    payload: object,
    candidate: _Candidate,
    captured_at: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TrendingObservationError(
            "invalid_repository_metadata",
            "GitHub repository metadata response must be an object",
        )
    repository_id = payload.get("id")
    if (
        isinstance(repository_id, bool)
        or not isinstance(repository_id, int)
        or repository_id != candidate.github_repository_id
    ):
        raise TrendingObservationError(
            "repository_metadata_identity_mismatch",
            "GitHub metadata endpoint returned a different repository ID",
        )
    repository = _required_string(payload, "full_name")
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise TrendingObservationError(
            "invalid_repository_metadata",
            "GitHub metadata returned an invalid owner/name",
        )
    if candidate.search_names and repository not in candidate.search_names:
        raise TrendingObservationError(
            "repository_identity_changed_during_capture",
            "GitHub Search and repository metadata returned different owner/name values for one ID",
        )
    html_url = _required_string(payload, "html_url")
    created_at = _required_string(payload, "created_at")
    updated_at = _required_string(payload, "updated_at")
    pushed_at = _required_string(payload, "pushed_at")
    for field, value in (
        ("created_at", created_at),
        ("updated_at", updated_at),
        ("pushed_at", pushed_at),
    ):
        parse_timestamp(value, field=field)
    default_branch = _required_string(payload, "default_branch")
    topics = payload.get("topics", [])
    if not isinstance(topics, list) or any(
        not isinstance(topic, str) or not topic for topic in topics
    ):
        raise TrendingObservationError(
            "invalid_repository_metadata",
            "GitHub repository topics must be strings",
        )
    language = payload.get("language")
    if language is not None and (not isinstance(language, str) or not language):
        raise TrendingObservationError(
            "invalid_repository_metadata",
            "GitHub repository language must be null or a non-empty string",
        )
    license_value = payload.get("license")
    if license_value is not None and not isinstance(license_value, dict):
        raise TrendingObservationError(
            "invalid_repository_metadata",
            "GitHub repository license must be null or an object",
        )
    license_id = license_value.get("spdx_id") if isinstance(license_value, dict) else None
    if license_id is not None and (not isinstance(license_id, str) or not license_id):
        raise TrendingObservationError(
            "invalid_repository_metadata",
            "GitHub repository SPDX ID must be null or a non-empty string",
        )
    mirror_url = payload.get("mirror_url")
    if mirror_url is not None and (not isinstance(mirror_url, str) or not mirror_url):
        raise TrendingObservationError(
            "invalid_repository_metadata",
            "GitHub repository mirror URL must be null or a non-empty string",
        )
    description = payload.get("description")
    if description is not None and not isinstance(description, str):
        raise TrendingObservationError(
            "invalid_repository_metadata",
            "GitHub repository description must be null or a string",
        )
    recalled_by = sorted(
        candidate.recalled_by,
        key=lambda item: (
            item["source"],
            item["sourceKey"],
            item["sourceRank"],
            item["capturedAt"],
        ),
    )
    observation = {
        "schemaVersion": SCHEMA_VERSION,
        "githubRepositoryId": repository_id,
        "repository": repository,
        "htmlUrl": html_url,
        "description": description,
        "capturedAt": captured_at,
        "totalStars": _required_int(payload, "stargazers_count"),
        "forks": _required_int(payload, "forks_count"),
        "openIssues": _required_int(payload, "open_issues_count"),
        "createdAt": created_at,
        "updatedAt": updated_at,
        "pushedAt": pushed_at,
        "defaultBranch": default_branch,
        "primaryLanguage": language,
        "topics": sorted(set(topics)),
        "licenseSpdxId": license_id,
        "archived": _required_bool(payload, "archived"),
        "disabled": _required_bool(payload, "disabled"),
        "fork": _required_bool(payload, "fork"),
        "mirrorUrl": mirror_url,
        "recalledBy": recalled_by,
    }
    return validate_observation(observation)


def collect_capture_bundle(
    *,
    data_dir: Path,
    scheduled_at: datetime,
    client: TrendingGitHubClient,
    token: str,
    limit: int = DEFAULT_LIMIT,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip():
        raise TrendingObservationError(
            "github_token_required",
            "GITHUB_TOKEN is required; anonymous fallback is disabled",
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= DEFAULT_LIMIT:
        raise TrendingObservationError(
            "invalid_candidate_limit",
            f"limit must be between 1 and {DEFAULT_LIMIT}",
        )
    scheduled = _utc(scheduled_at)
    _assert_fixed_phase(scheduled)
    now = clock or (lambda: datetime.now(timezone.utc))

    carry = _load_recent_candidates(data_dir, scheduled, limit=limit)
    candidates: dict[int, _Candidate] = {
        item.github_repository_id: item for item in carry
    }
    query_status: list[dict[str, Any]] = []
    queries = _expected_queries(scheduled)
    current_name_ids: dict[str, int] = {}

    for query_index, query in enumerate(queries, start=1):
        query_id = f"query-{query_index:02d}"
        try:
            items, incomplete = _search_response(client, query)
            queried_at = _timestamp(now())
            seen_in_query: set[int] = set()
            for rank, item in enumerate(items, start=1):
                repository_id = item["id"]
                repository = item["repository"]
                if repository_id in seen_in_query:
                    existing = candidates.get(repository_id)
                    if (
                        existing is not None
                        and existing.search_names
                        and repository not in existing.search_names
                    ):
                        raise TrendingObservationError(
                            "repository_identity_changed_during_capture",
                            "GitHub Search returned multiple owner/name values for one repository ID",
                        )
                    continue
                seen_in_query.add(repository_id)
                name_key = repository.casefold()
                known_id = current_name_ids.get(name_key)
                if known_id is not None and known_id != repository_id:
                    raise TrendingObservationError(
                        "repository_name_identity_collision",
                        "GitHub Search mapped one owner/name to multiple repository IDs",
                    )
                current_name_ids[name_key] = repository_id
                candidate = candidates.get(repository_id)
                if candidate is None:
                    candidate = _Candidate(repository_id, repository, [])
                    candidates[repository_id] = candidate
                if candidate.search_names is None:
                    raise TrendingObservationError(
                        "repository_identity_changed_during_capture",
                        "candidate identity state is unavailable",
                    )
                candidate.search_names.add(repository)
                if len(candidate.search_names) > 1:
                    raise TrendingObservationError(
                        "repository_identity_changed_during_capture",
                        "GitHub Search returned multiple owner/name values for one repository ID",
                    )
                candidate.repository = repository
                order = (query_index, rank)
                if candidate.search_order is None or order < candidate.search_order:
                    candidate.search_order = order
                candidate.recalled_by.append(
                    {
                        "source": "github_search",
                        "sourceKey": query_id,
                        "sourceRank": rank,
                        "capturedAt": queried_at,
                        "queryId": query_id,
                        "query": query,
                        "page": 1,
                    }
                )
        except TrendingObservationError as error:
            if error.code in {
                "repository_identity_changed_during_capture",
                "repository_name_identity_collision",
            }:
                raise
            code, message = _sanitize_error(error, token=token)
            query_status.append(
                {
                    "queryId": query_id,
                    "query": query,
                    "state": "failed",
                    "resultCount": 0,
                    "incompleteResults": False,
                    "errorCode": code,
                    "errorMessage": message,
                }
            )
            continue
        except Exception as error:
            code, message = _sanitize_error(error, token=token)
            query_status.append(
                {
                    "queryId": query_id,
                    "query": query,
                    "state": "failed",
                    "resultCount": 0,
                    "incompleteResults": False,
                    "errorCode": code,
                    "errorMessage": message,
                }
            )
            continue
        query_status.append(
            {
                "queryId": query_id,
                "query": query,
                "state": "healthy",
                "resultCount": len(items),
                "incompleteResults": incomplete,
                "errorCode": None,
                "errorMessage": None,
            }
        )

    successful = sum(item["state"] == "healthy" for item in query_status)
    if successful == 0:
        error_codes = sorted(
            {
                str(item["errorCode"])
                for item in query_status
                if item.get("errorCode")
            }
        )
        raise TrendingObservationError(
            "all_candidate_queries_failed",
            "all nine GitHub candidate queries failed; no capture was created",
            details={
                "errorCodes": error_codes,
                "retryable": _all_github_failures_are_retryable(error_codes),
            },
        )

    carry_ids = {item.github_repository_id for item in carry}
    carry_ordered = [candidates[item.github_repository_id] for item in carry]
    search_new = sorted(
        (item for repository_id, item in candidates.items() if repository_id not in carry_ids),
        key=lambda item: (
            item.search_order or (999, 999),
            item.github_repository_id,
        ),
    )
    selected = (carry_ordered + search_new)[:limit]
    if not selected:
        raise TrendingObservationError(
            "no_candidates_recalled",
            "successful GitHub queries returned no repositories and no recent observations exist",
        )

    raw_metadata: list[tuple[_Candidate, dict[str, Any]]] = []
    metadata_failures: list[dict[str, Any]] = []
    for candidate in selected:
        try:
            metadata = client.repository(candidate.github_repository_id)
            if not isinstance(metadata, dict):
                raise TrendingObservationError(
                    "invalid_repository_metadata",
                    "GitHub repository metadata response must be an object",
                )
            returned_id = metadata.get("id")
            returned_name = metadata.get("full_name")
            if (
                isinstance(returned_id, bool)
                or not isinstance(returned_id, int)
                or returned_id != candidate.github_repository_id
            ):
                raise TrendingObservationError(
                    "repository_metadata_identity_mismatch",
                    "GitHub metadata endpoint returned a different repository ID",
                )
            if candidate.search_names and returned_name not in candidate.search_names:
                raise TrendingObservationError(
                    "repository_identity_changed_during_capture",
                    "GitHub Search and metadata changed owner/name during one capture",
                )
            raw_metadata.append((candidate, metadata))
        except TrendingObservationError as error:
            if error.code in {
                "repository_metadata_identity_mismatch",
                "repository_identity_changed_during_capture",
                "repository_name_identity_collision",
            }:
                raise
            code, message = _sanitize_error(error, token=token)
            metadata_failures.append(
                {
                    "githubRepositoryId": candidate.github_repository_id,
                    "repository": candidate.repository,
                    "errorCode": code,
                    "errorMessage": message,
                }
            )
        except Exception as error:
            code, message = _sanitize_error(error, token=token)
            metadata_failures.append(
                {
                    "githubRepositoryId": candidate.github_repository_id,
                    "repository": candidate.repository,
                    "errorCode": code,
                    "errorMessage": message,
                }
            )
    captured = _utc(now())
    captured_text = _timestamp(captured)
    observations: list[dict[str, Any]] = []
    names: dict[str, int] = {}
    for candidate, metadata in raw_metadata:
        try:
            observation = _normalize_repository_metadata(metadata, candidate, captured_text)
        except TrendingObservationError as error:
            if error.code in {
                "repository_metadata_identity_mismatch",
                "repository_identity_changed_during_capture",
                "repository_name_identity_collision",
            }:
                raise
            code, message = _sanitize_error(error, token=token)
            metadata_failures.append(
                {
                    "githubRepositoryId": candidate.github_repository_id,
                    "repository": candidate.repository,
                    "errorCode": code,
                    "errorMessage": message,
                }
            )
            continue
        key = observation["repository"].casefold()
        previous_id = names.get(key)
        if previous_id is not None and previous_id != observation["githubRepositoryId"]:
            raise TrendingObservationError(
                "repository_name_identity_collision",
                "repository metadata mapped one owner/name to multiple repository IDs",
            )
        names[key] = observation["githubRepositoryId"]
        observations.append(observation)
    if not observations:
        error_codes = sorted(
            {
                str(item["errorCode"])
                for item in metadata_failures
                if item.get("errorCode")
            }
        )
        raise TrendingObservationError(
            "all_repository_metadata_failed",
            "metadata lookup failed for every selected candidate; no capture was created",
            details={
                "errorCodes": error_codes,
                "retryable": _all_github_failures_are_retryable(error_codes),
            },
        )

    delay = (captured - scheduled).total_seconds()
    degraded = (
        successful != len(query_status)
        or any(item["incompleteResults"] for item in query_status)
        or bool(metadata_failures)
    )
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "policyVersion": POLICY_VERSION,
        "captureId": capture_id_for_scheduled_at(scheduled),
        "scheduleTimezone": SCHEDULE_TIMEZONE,
        "cadenceMinutes": CADENCE_MINUTES,
        "scheduledAt": _timestamp(scheduled),
        "capturedAt": captured_text,
        "captureDelaySeconds": delay,
        "windowEligible": abs(delay) <= WINDOW_TOLERANCE_SECONDS,
        "coverageState": "degraded" if degraded else "healthy",
        "successfulQueryCount": successful,
        "failedQueryCount": len(query_status) - successful,
        "candidateCount": len(selected),
        "observationCount": len(observations),
        "metadataFailureCount": len(metadata_failures),
        "queryStatus": query_status,
        "metadataFailures": metadata_failures,
        "observations": observations,
        "retention": {
            "retentionClass": "raw_2h_observation",
            "retentionDays": RETENTION_DAYS,
            "retainUntil": _timestamp(captured + timedelta(days=RETENTION_DAYS)),
        },
    }
    return validate_capture_bundle(attach_bundle_digest(payload))


def _serialized_bundle(bundle: dict[str, Any]) -> bytes:
    validate_capture_bundle(bundle)
    return json.dumps(
        bundle,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"


def _capture_settlement_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise TrendingObservationError(
            "unsafe_or_unstable_capture",
            f"capture target is unavailable during create settlement: {error.__class__.__name__}",
        ) from None
    if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise TrendingObservationError(
            "unsafe_or_unstable_capture",
            "capture target is not a no-follow regular file during create settlement",
        )
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        stat.S_IFMT(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_nlink),
    )


def _load_capture_for_concurrent_create_settlement(path: Path) -> dict[str, Any]:
    """Read a same-slot create winner after its hard-link source is released.

    The only retryable transition is the winning publisher unlinking another
    name for the same immutable inode.  Object replacement, byte mutation,
    disappearance, unsafe types, and semantic validation failures remain
    permanent failures.
    """

    path = _absolute_without_escape(path, label="capture path")
    _assert_safe_existing(path.parent, expect_directory=True)
    _assert_safe_existing(path, expect_directory=False)
    for attempt in range(_CREATE_SETTLEMENT_MAX_ATTEMPTS):
        before = _capture_settlement_identity(path)
        try:
            snapshot = stable_read(path)
        except StableReadError as error:
            if error.reason != "concurrent_change" or not error.retryable:
                raise TrendingObservationError(
                    "unsafe_or_unstable_capture",
                    "capture target failed a permanent stable-read check during create settlement",
                ) from None
            after = _capture_settlement_identity(path)
            same_immutable_object = before[:5] == after[:5]
            hardlink_source_released = after[5] < before[5] and after[5] >= 1
            if not same_immutable_object or not hardlink_source_released:
                raise TrendingObservationError(
                    "unsafe_or_unstable_capture",
                    "capture target changed beyond a hard-link source release during create settlement",
                ) from None
            if attempt + 1 >= _CREATE_SETTLEMENT_MAX_ATTEMPTS:
                raise TrendingObservationError(
                    "capture_create_settlement_failed",
                    "capture target remained unstable after bounded create settlement",
                    details={"attempts": _CREATE_SETTLEMENT_MAX_ATTEMPTS},
                ) from None
            time.sleep(_CREATE_SETTLEMENT_BACKOFF_SECONDS[attempt])
            continue

        snapshot_identity = (
            snapshot.identity[0],
            snapshot.identity[1],
            stat.S_IFMT(snapshot.identity[2]),
            snapshot.identity[3],
            snapshot.identity[4],
        )
        if snapshot_identity != before[:5]:
            raise TrendingObservationError(
                "unsafe_or_unstable_capture",
                "capture target was replaced before a stable create-settlement read",
            )
        return _decode_capture(snapshot.content, path)

    raise AssertionError("bounded capture create settlement did not terminate")


def _existing_capture(
    path: Path,
    capture_id: str,
    *,
    settle_concurrent_create: bool = False,
) -> dict[str, Any] | None:
    if not os.path.lexists(path):
        return None
    try:
        loaded = (
            _load_capture_for_concurrent_create_settlement(path)
            if settle_concurrent_create
            else load_capture(path)
        )
        return validate_capture_bundle(
            loaded,
            expected_capture_id=capture_id,
            expected_path=path,
        )
    except TrendingObservationError as error:
        if error.code == "capture_create_settlement_failed":
            raise
        raise TrendingObservationError(
            "existing_capture_invalid",
            f"existing capture is unsafe, corrupt, or has a mismatched identity: {error.code}",
        ) from None


def write_capture_create_only(
    data_dir: Path,
    bundle: dict[str, Any],
) -> tuple[str, Path]:
    validated = validate_capture_bundle(bundle)
    scheduled = parse_timestamp(validated["scheduledAt"], field="scheduledAt")
    target = capture_path_for_scheduled_at(data_dir, scheduled)
    existing = _existing_capture(
        target,
        validated["captureId"],
        settle_concurrent_create=True,
    )
    if existing is not None:
        return "already_captured", target
    _ensure_capture_parent(data_dir, target)
    serialized = _serialized_bundle(validated)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        staged = stable_read(temporary)
        _decode_capture(staged.content, target)
        _absolute_without_escape(target, label="capture target")
        _assert_safe_existing(target.parent, expect_directory=True)
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing = _existing_capture(
                target,
                validated["captureId"],
                settle_concurrent_create=True,
            )
            if existing is not None:
                return "already_captured", target
            raise TrendingObservationError(
                "capture_create_conflict",
                "capture target appeared concurrently with an invalid identity",
            ) from None
        except OSError as error:
            raise TrendingObservationError(
                "atomic_create_failed",
                f"capture could not be atomically created without replacement: {error}",
            ) from None
        target_snapshot = stable_read(target)
        _absolute_without_escape(target, label="published capture")
        if target_snapshot.content != staged.content:
            raise TrendingObservationError(
                "capture_publish_identity_mismatch",
                "published capture bytes differ from the validated temporary file",
            )
        _decode_capture(target_snapshot.content, target)
        temporary.unlink()
        temporary = None
        if os.name != "nt":
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        return "captured", target
    except TrendingObservationError:
        raise
    except OSError as error:
        raise TrendingObservationError(
            "capture_write_failed",
            f"capture staging or durability operation failed: {error}",
        ) from None
    finally:
        if temporary is not None and os.path.lexists(temporary):
            try:
                temporary.unlink()
            except OSError:
                pass


def observer_lock_path(data_dir: Path, lock_root: Path | None = None) -> Path:
    writer = data_dir_lock_path(data_dir, lock_root=lock_root)
    return (
        writer.parent
        / "trending-observer-instances"
        / writer.name.replace("data-", "trending-observer-", 1)
    )


def _safe_lock_owner(path: Path) -> dict[str, Any] | None:
    try:
        snapshot = stable_read(path, max_attempts=2)
        payload = strict_json_loads(snapshot.content.decode("utf-8"))
    except (StableReadError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    acquired_at = payload.get("acquiredAt")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(acquired_at, str)
    ):
        return None
    try:
        parse_timestamp(acquired_at, field="acquiredAt")
    except TrendingObservationError:
        return None
    return {"pid": pid, "acquiredAt": acquired_at}


@contextmanager
def observer_instance_lock(
    data_dir: Path,
    *,
    lock_root: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Iterator[Path]:
    path = _absolute_without_escape(
        observer_lock_path(data_dir, lock_root), label="observer lock"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TrendingObservationError(
            "observer_lock_unavailable",
            f"observer lock directory could not be created: {path.parent}: {error}",
        ) from None
    _absolute_without_escape(path, label="observer lock")
    _assert_safe_existing(path.parent, expect_directory=True)
    _assert_safe_existing(path, expect_directory=False)
    try:
        handle = path.open("a+b")
    except OSError as error:
        raise TrendingObservationError(
            "observer_lock_unavailable",
            f"observer lock could not be opened: {path}: {error}",
        ) from None
    acquired = False
    try:
        try:
            opened = os.fstat(handle.fileno())
            linked = os.lstat(path)
        except OSError as error:
            raise TrendingObservationError(
                "observer_lock_unavailable",
                f"observer lock identity could not be verified: {path}: {error}",
            ) from None
        if (
            _is_reparse(opened)
            or _is_reparse(linked)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or not os.path.samestat(opened, linked)
        ):
            raise TrendingObservationError(
                "unsafe_observer_lock",
                "observer lock changed identity or is not a no-follow regular file",
            )
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
        except OSError as error:
            raise TrendingObservationError(
                "observer_lock_unavailable",
                f"observer lock could not be initialized: {path}: {error}",
            ) from None
        try:
            _try_lock(handle)
            acquired = True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ObserverAlreadyRunningError(path, _safe_lock_owner(path)) from error
            raise TrendingObservationError(
                "observer_lock_unavailable",
                f"observer lock could not be acquired: {path}: {error}",
            ) from None
        metadata = canonical_json_bytes(
            {
                "pid": os.getpid(),
                "acquiredAt": _timestamp((clock or (lambda: datetime.now(timezone.utc)))()),
            }
        )
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(metadata)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as error:
            raise TrendingObservationError(
                "observer_lock_unavailable",
                f"observer lock metadata could not be persisted: {path}: {error}",
            ) from None
        yield path
    finally:
        try:
            if acquired:
                _unlock(handle)
        finally:
            handle.close()


def _summary(
    *,
    state: str,
    capture_id: str,
    scheduled_at: datetime,
    bundle: dict[str, Any] | None,
    capture_path: Path,
    captured: bool,
) -> dict[str, Any]:
    observations = bundle.get("observations", []) if bundle else []
    carry_forward_count = sum(
        any(
            source.get("source") == "recent_observation_carry_forward"
            for source in observation.get("recalledBy", [])
        )
        for observation in observations
    )
    return {
        "state": state,
        "captureId": capture_id,
        "scheduledAt": _timestamp(scheduled_at),
        "capturedAt": bundle.get("capturedAt") if bundle else None,
        "coverageState": bundle.get("coverageState") if bundle else None,
        "candidateCount": bundle.get("candidateCount") if bundle else 0,
        "observationCount": bundle.get("observationCount") if bundle else 0,
        "metadataFailureCount": bundle.get("metadataFailureCount") if bundle else 0,
        "successfulQueryCount": bundle.get("successfulQueryCount") if bundle else 0,
        "failedQueryCount": bundle.get("failedQueryCount") if bundle else 0,
        "captureDelaySeconds": bundle.get("captureDelaySeconds") if bundle else None,
        "carryForwardCount": carry_forward_count,
        "newRepositoryCount": len(observations) - carry_forward_count,
        "capturePath": str(capture_path),
        "windowEligible": bundle.get("windowEligible") if bundle else None,
        "captured": captured,
    }


def run_observer(
    *,
    data_dir: Path,
    scheduled_at: datetime,
    timezone_name: str,
    limit: int,
    dry_run: bool,
    token: str | None,
    client: TrendingGitHubClient | None = None,
    clock: Callable[[], datetime] | None = None,
    lock_root: Path | None = None,
) -> dict[str, Any]:
    if timezone_name != SCHEDULE_TIMEZONE:
        raise TrendingObservationError(
            "unsupported_schedule_timezone",
            f"schedule timezone must be {SCHEDULE_TIMEZONE}",
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= DEFAULT_LIMIT:
        raise TrendingObservationError(
            "invalid_candidate_limit",
            f"limit must be between 1 and {DEFAULT_LIMIT}",
        )
    scheduled = _utc(scheduled_at)
    _assert_fixed_phase(scheduled)
    capture_id = capture_id_for_scheduled_at(scheduled)
    target = capture_path_for_scheduled_at(data_dir, scheduled)
    existing = _existing_capture(target, capture_id)
    if existing is not None:
        return _summary(
            state="already_captured",
            capture_id=capture_id,
            scheduled_at=scheduled,
            bundle=existing,
            capture_path=target,
            captured=False,
        )
    if not isinstance(token, str) or not token.strip():
        raise TrendingObservationError(
            "github_token_required",
            "GITHUB_TOKEN is required; anonymous fallback is disabled",
        )
    try:
        with observer_instance_lock(data_dir, lock_root=lock_root, clock=clock) as lock_path:
            existing = _existing_capture(target, capture_id)
            if existing is not None:
                return _summary(
                    state="already_captured",
                    capture_id=capture_id,
                    scheduled_at=scheduled,
                    bundle=existing,
                    capture_path=target,
                    captured=False,
                )
            bundle = collect_capture_bundle(
                data_dir=data_dir,
                scheduled_at=scheduled,
                client=client or GitHubClient(token),
                token=token,
                limit=limit,
                clock=clock,
            )
            if dry_run:
                return _summary(
                    state="dry_run",
                    capture_id=capture_id,
                    scheduled_at=scheduled,
                    bundle=bundle,
                    capture_path=target,
                    captured=False,
                )
            state, stored_path = write_capture_create_only(data_dir, bundle)
            return _summary(
                state=state,
                capture_id=capture_id,
                scheduled_at=scheduled,
                bundle=bundle,
                capture_path=stored_path,
                captured=state == "captured",
            )
    except ObserverAlreadyRunningError as error:
        return {
            "state": "skipped_overlap",
            "captureId": capture_id,
            "scheduledAt": _timestamp(scheduled),
            "capturedAt": None,
            "coverageState": None,
            "candidateCount": 0,
            "observationCount": 0,
            "metadataFailureCount": 0,
            "successfulQueryCount": 0,
            "failedQueryCount": 0,
            "captureDelaySeconds": None,
            "carryForwardCount": 0,
            "newRepositoryCount": 0,
            "capturePath": str(target),
            "windowEligible": None,
            "captured": False,
            "lockPath": str(error.lock_path),
            **({"existingOwner": error.existing_owner} if error.existing_owner else {}),
        }


def audit_observation_store(data_dir: Path) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    bundles: list[dict[str, Any]] = []
    paths_by_capture: dict[str, Path] = {}
    reported_root = str(
        Path(data_dir).expanduser()
        / "observations"
        / "trending"
        / "v1"
        / "captures"
    )
    try:
        paths = _walk_capture_paths(data_dir)
    except TrendingObservationError as error:
        paths = []
        issues.append({"code": error.code, "path": reported_root, "message": str(error)})
    seen_slots: dict[str, Path] = {}
    for path in paths:
        try:
            bundle = load_capture(path)
            slot = bundle["scheduledAt"]
            if slot in seen_slots:
                raise TrendingObservationError(
                    "duplicate_capture_slot",
                    f"scheduledAt is already recorded by {seen_slots[slot]}",
                )
            seen_slots[slot] = path
            bundles.append(bundle)
            paths_by_capture[bundle["captureId"]] = path
        except TrendingObservationError as error:
            issues.append({"code": error.code, "path": str(path), "message": str(error)})
    bundles_by_capture = {bundle["captureId"]: bundle for bundle in bundles}
    ordered_bundles = sorted(
        bundles,
        key=lambda item: parse_timestamp(item["scheduledAt"], field="scheduledAt"),
    )
    earliest_scheduled = (
        parse_timestamp(ordered_bundles[0]["scheduledAt"], field="scheduledAt")
        if ordered_bundles
        else None
    )
    active: deque[tuple[datetime, dict[str, Any]]] = deque()
    active_ids: Counter[int] = Counter()
    for bundle in ordered_bundles:
        bundle_scheduled = parse_timestamp(bundle["scheduledAt"], field="scheduledAt")
        while active and bundle_scheduled - active[0][0] > timedelta(
            hours=TRACKING_WINDOW_HOURS
        ):
            _, expired = active.popleft()
            for observation in expired["observations"]:
                repository_id = observation["githubRepositoryId"]
                active_ids[repository_id] -= 1
                if active_ids[repository_id] <= 0:
                    del active_ids[repository_id]
        carry_ids = set(active_ids)
        outcome_ids = {
            observation["githubRepositoryId"] for observation in bundle["observations"]
        } | {
            failure["githubRepositoryId"] for failure in bundle["metadataFailures"]
        }
        missing_carry = sorted(carry_ids - outcome_ids)
        if missing_carry:
            issues.append(
                {
                    "code": "carry_forward_candidate_missing",
                    "path": str(paths_by_capture[bundle["captureId"]]),
                    "message": (
                        f"capture omits {len(missing_carry)} repository IDs observed "
                        "in the preceding 26-hour phase window"
                    ),
                }
            )
        if len(carry_ids) > DEFAULT_LIMIT:
            issues.append(
                {
                    "code": "tracking_capacity_exceeded",
                    "path": str(paths_by_capture[bundle["captureId"]]),
                    "message": (
                        f"capture exists despite {len(carry_ids)} carry-forward "
                        f"repositories exceeding the global {DEFAULT_LIMIT} limit"
                    ),
                }
            )
        for observation in bundle["observations"]:
            for source in observation["recalledBy"]:
                if source["source"] != "recent_observation_carry_forward":
                    continue
                referenced = bundles_by_capture.get(source["sourceKey"])
                rank = source["sourceRank"]
                source_match = CAPTURE_ID_PATTERN.fullmatch(source["sourceKey"])
                if source_match is None:
                    issues.append(
                        {
                            "code": "carry_forward_reference_invalid",
                            "path": str(paths_by_capture[bundle["captureId"]]),
                            "message": "carry-forward source capture ID is invalid",
                        }
                    )
                    continue
                source_schedule = datetime.strptime(
                    "".join(source_match.groups()), "%Y%m%d%H%M%S"
                ).replace(tzinfo=timezone.utc)
                reference_age = (
                    bundle_scheduled
                    - parse_timestamp(referenced["scheduledAt"], field="scheduledAt")
                    if referenced is not None
                    else None
                )
                if referenced is None:
                    # A 90-day retention boundary can legitimately remove a
                    # source up to 26 hours before a newer referring capture.
                    # Missing references inside the retained store range remain
                    # a hard failure; older ones are unverifiable, not forged.
                    reference_valid = (
                        earliest_scheduled is not None
                        and source_schedule < earliest_scheduled
                    )
                else:
                    reference_valid = (
                        reference_age is not None
                        and timedelta(0)
                        < reference_age
                        <= timedelta(hours=TRACKING_WINDOW_HOURS)
                        and source["capturedAt"] == referenced["capturedAt"]
                        and rank <= len(referenced["observations"])
                        and referenced["observations"][rank - 1]["githubRepositoryId"]
                        == observation["githubRepositoryId"]
                    )
                if not reference_valid:
                    issues.append(
                        {
                            "code": "carry_forward_reference_invalid",
                            "path": str(paths_by_capture[bundle["captureId"]]),
                            "message": (
                                "carry-forward provenance does not identify the same "
                                "repository at its recorded rank in a retained capture"
                            ),
                        }
                    )
        active.append((bundle_scheduled, bundle))
        active_ids.update(
            observation["githubRepositoryId"] for observation in bundle["observations"]
        )
    captured_times = sorted(
        parse_timestamp(bundle["capturedAt"], field="capturedAt") for bundle in bundles
    )
    degraded_count = sum(bundle["coverageState"] == "degraded" for bundle in bundles)
    status = "failed" if issues else ("degraded" if degraded_count else "healthy")
    return {
        "schemaVersion": 1,
        "status": status,
        "captureCount": len(bundles),
        "observationCount": sum(bundle["observationCount"] for bundle in bundles),
        "earliestCapturedAt": _timestamp(captured_times[0]) if captured_times else None,
        "latestCapturedAt": _timestamp(captured_times[-1]) if captured_times else None,
        "eligibleCaptureCount": sum(bundle["windowEligible"] for bundle in bundles),
        "degradedCaptureCount": degraded_count,
        "issueCount": len(issues),
        "issues": issues,
    }
