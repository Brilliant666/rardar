"""Read-only verification for one exact Rardar Linux release artifact.

The verifier intentionally uses only the Python standard library.  It never
downloads dependencies, runs npm, mutates the release tree, or touches Rardar
runtime/data state.  It is suitable both for CI extraction acceptance and the
production deployment preflight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
REPOSITORY = "Brilliant666/rardar"
TARGET_PLATFORM = "linux"
TARGET_ARCHITECTURE = "x86_64"
BUILDER_OS = "ubuntu-24.04"
NODE_VERSION = "v22.13.1"
PYTHON_WHEEL_VERSION = "3.12"
MANIFEST_NAME = "release-manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
NPM_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

MANIFEST_FIELDS = frozenset(
    {
        "schemaVersion",
        "repository",
        "commitSha",
        "platform",
        "architecture",
        "builderOs",
        "nodeVersion",
        "npmVersion",
        "pythonWheelVersion",
        "packageLockSha256",
        "requirementsLockSha256",
        "verifyWorkflowRunId",
        "verifyWorkflowHeadSha",
        "builtAt",
    }
)

REQUIRED_FILES = (
    "package.json",
    "package-lock.json",
    "requirements.lock",
    "pipeline/runtime.py",
    "pipeline/deployment.py",
    "pipeline/release_artifact.py",
    "node_modules/vite/bin/vite.js",
    "node_modules/vinext/dist/cli.js",
    "vite.config.ts",
    ".openai/hosting.json",
    "app/runtime-readiness.mjs",
    "build/published-data-bridge.ts",
    "build/sites-vite-plugin.ts",
    "worker/index.ts",
    MANIFEST_NAME,
)

REQUIRED_DIRECTORIES = (
    "dist",
    "deploy/systemd",
    "node_modules/vite",
    "node_modules/vinext",
    "wheelhouse",
)

FORBIDDEN_TOP_LEVEL = frozenset(
    {
        "data",
        ".git",
        ".venv",
        ".venv-ci",
        ".vinext",
        ".wrangler",
        "release-stage",
    }
)

FORBIDDEN_CACHE_PATHS = frozenset(
    {
        "node_modules/.cache",
        "node_modules/.vite",
        "node_modules/.vite-temp",
    }
)

ALLOWED_ENVIRONMENT_EXAMPLE = ".env.production.example"


class ReleaseArtifactError(RuntimeError):
    """One release artifact invariant failed."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def _fail(code: str, detail: str) -> None:
    raise ReleaseArtifactError(code, detail)


def _canonical_root(release_root: Path) -> Path:
    candidate = Path(release_root).expanduser()
    try:
        root = candidate.resolve(strict=True)
        metadata = os.lstat(root)
    except OSError as error:
        _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"release root is unavailable: {error}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", "release root must be a real directory")
    return root


def _read_regular_bytes(path: Path, *, maximum: int | None = None) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"cannot open required file {path}: {error}")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"required file is not regular: {path}")
        if maximum is not None and before.st_size > maximum:
            _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", f"manifest exceeds {maximum} bytes")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"required file changed while read: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"required file grew while read: {path}")
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"required file changed while read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(_read_regular_bytes(path)).hexdigest()


def _inspect_required_path(root: Path, relative: str, *, directory: bool) -> None:
    current = root
    for component in Path(relative).parts:
        current /= component
        try:
            metadata = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"required path is missing: {relative}")
        except OSError as error:
            _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"cannot inspect {relative}: {error}")
        if stat.S_ISLNK(metadata.st_mode):
            _fail(
                "FAIL_RELEASE_ARTIFACT_UNSAFE_LINK",
                f"required path cannot traverse a symbolic link: {relative}",
            )
    metadata = os.lstat(current)
    if directory and not stat.S_ISDIR(metadata.st_mode):
        _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"required directory has wrong type: {relative}")
    if not directory and not stat.S_ISREG(metadata.st_mode):
        _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"required file has wrong type: {relative}")


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_tree(
    root: Path,
    *,
    skip_top_level_directories: frozenset[str] = frozenset(),
) -> Iterator[tuple[Path, os.stat_result]]:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"cannot enumerate {directory}: {error}")
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"cannot inspect {path}: {error}")
            yield path, metadata
            relative = _relative_posix(path, root)
            if (
                stat.S_ISDIR(metadata.st_mode)
                and not stat.S_ISLNK(metadata.st_mode)
                and relative not in skip_top_level_directories
            ):
                pending.append(path)


def _forbidden_secret_name(relative: str) -> bool:
    parts = relative.split("/")
    for index, component in enumerate(parts):
        lowered = component.lower()
        if lowered == ".env" or lowered.startswith(".env."):
            if index == 0 and relative == ALLOWED_ENVIRONMENT_EXAMPLE:
                continue
            return True
        if lowered == ".dev.vars" or lowered.startswith(".dev.vars."):
            return True
        if lowered in {
            "rardar.secret",
            "credentials",
            "credentials.json",
            "credentials.yaml",
            "credentials.yml",
            "token",
            "tokens",
            "token.json",
            "tokens.json",
        }:
            return True
    return False


def _validate_symlink(root: Path, path: Path) -> None:
    relative = _relative_posix(path, root)
    try:
        target_text = os.readlink(path)
    except OSError as error:
        _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"cannot read link {relative}: {error}")
    if (
        not target_text
        or os.path.isabs(target_text)
        or target_text.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:[\\/]", target_text)
    ):
        _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"link must use a relative target: {relative}")
    try:
        target = (path.parent / target_text).resolve(strict=True)
    except OSError as error:
        _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"link target is unavailable: {relative}: {error}")
    if target != root and root not in target.parents:
        _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"link escapes release root: {relative}")
    target_relative = _relative_posix(target, root)
    if target_relative == "data" or target_relative.startswith("data/"):
        _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"link targets forbidden data: {relative}")


def _scan_tree(root: Path, *, allow_runtime_venv: bool = False) -> dict[str, int]:
    counts = {"files": 0, "directories": 0, "symlinks": 0}
    ignored = frozenset({".venv"}) if allow_runtime_venv else frozenset()
    for path, metadata in _iter_tree(root, skip_top_level_directories=ignored):
        relative = _relative_posix(path, root)
        if relative in ignored:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                _fail(
                    "FAIL_RELEASE_ARTIFACT_FORBIDDEN_CONTENT",
                    "the deployment-created .venv must be a real directory",
                )
            continue
        first = relative.split("/", 1)[0]
        if first in FORBIDDEN_TOP_LEVEL or relative in FORBIDDEN_CACHE_PATHS:
            _fail("FAIL_RELEASE_ARTIFACT_FORBIDDEN_CONTENT", f"forbidden release path: {relative}")
        if _forbidden_secret_name(relative):
            _fail("FAIL_RELEASE_ARTIFACT_FORBIDDEN_CONTENT", f"secret-like release path: {relative}")
        if stat.S_ISLNK(metadata.st_mode):
            _validate_symlink(root, path)
            counts["symlinks"] += 1
        elif stat.S_ISDIR(metadata.st_mode):
            counts["directories"] += 1
        elif stat.S_ISREG(metadata.st_mode):
            counts["files"] += 1
        else:
            _fail("FAIL_RELEASE_ARTIFACT_FORBIDDEN_CONTENT", f"special file is forbidden: {relative}")
    return counts


def _parse_built_at(value: str) -> None:
    if not isinstance(value, str) or not value:
        _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", "builtAt must be a non-empty RFC3339 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", "builtAt must be RFC3339")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", "builtAt must include a timezone")


def validate_manifest_payload(payload: Any, *, expected_sha: str) -> dict[str, Any]:
    if not COMMIT_PATTERN.fullmatch(expected_sha):
        _fail("FAIL_RELEASE_ARTIFACT_IDENTITY", "expected SHA must be exactly 40 lowercase hex characters")
    if not isinstance(payload, dict) or set(payload) != MANIFEST_FIELDS:
        _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", "manifest fields do not match schemaVersion 1")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", "unsupported release manifest schemaVersion")
    fixed = {
        "repository": REPOSITORY,
        "platform": TARGET_PLATFORM,
        "architecture": TARGET_ARCHITECTURE,
        "builderOs": BUILDER_OS,
        "nodeVersion": NODE_VERSION,
        "pythonWheelVersion": PYTHON_WHEEL_VERSION,
    }
    for name, expected in fixed.items():
        if payload.get(name) != expected:
            _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", f"manifest {name} must be {expected}")
    commit_sha = payload.get("commitSha")
    verify_sha = payload.get("verifyWorkflowHeadSha")
    if not isinstance(commit_sha, str) or not COMMIT_PATTERN.fullmatch(commit_sha):
        _fail("FAIL_RELEASE_ARTIFACT_IDENTITY", "manifest commitSha must be a full lowercase SHA")
    if commit_sha != expected_sha or verify_sha != expected_sha:
        _fail("FAIL_RELEASE_ARTIFACT_IDENTITY", "release and successful Verify SHA must match expected SHA")
    npm_version = payload.get("npmVersion")
    if not isinstance(npm_version, str) or not NPM_VERSION_PATTERN.fullmatch(npm_version):
        _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", "manifest npmVersion is invalid")
    run_id = payload.get("verifyWorkflowRunId")
    if not isinstance(run_id, str) or not run_id.isdigit() or int(run_id) < 1:
        _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", "verifyWorkflowRunId must be a positive decimal string")
    for name in ("packageLockSha256", "requirementsLockSha256"):
        value = payload.get(name)
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", f"manifest {name} must be SHA-256")
    _parse_built_at(payload.get("builtAt"))
    return dict(payload)


def _load_manifest(root: Path, *, expected_sha: str) -> dict[str, Any]:
    raw = _read_regular_bytes(root / MANIFEST_NAME, maximum=MAX_MANIFEST_BYTES)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", f"release manifest is invalid JSON: {error}")
    return validate_manifest_payload(payload, expected_sha=expected_sha)


def _locked_requirements(path: Path) -> list[tuple[str, str]]:
    try:
        text = _read_regular_bytes(path).decode("utf-8")
    except UnicodeDecodeError:
        _fail("FAIL_RELEASE_ARTIFACT_WHEELHOUSE", "requirements.lock must be UTF-8")
    requirements: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line or line.count("==") != 1:
            _fail("FAIL_RELEASE_ARTIFACT_WHEELHOUSE", f"requirement is not exactly pinned: {line}")
        name, version = line.split("==", 1)
        if not name or not version:
            _fail("FAIL_RELEASE_ARTIFACT_WHEELHOUSE", f"invalid locked requirement: {line}")
        requirements.append((re.sub(r"[-_.]+", "_", name).lower(), version.lower()))
    if not requirements:
        _fail("FAIL_RELEASE_ARTIFACT_WHEELHOUSE", "requirements.lock is empty")
    return requirements


def _check_wheelhouse(root: Path) -> dict[str, int]:
    wheelhouse = root / "wheelhouse"
    wheels = sorted(path.name.lower() for path in wheelhouse.iterdir() if path.is_file() and path.suffix == ".whl")
    if not wheels:
        _fail("FAIL_RELEASE_ARTIFACT_WHEELHOUSE", "wheelhouse contains no wheels")
    missing: list[str] = []
    for name, version in _locked_requirements(root / "requirements.lock"):
        prefix = f"{name}-{version}-"
        if not any(wheel.startswith(prefix) for wheel in wheels):
            missing.append(f"{name}=={version}")
    if missing:
        _fail("FAIL_RELEASE_ARTIFACT_WHEELHOUSE", "wheelhouse misses locked requirements: " + ", ".join(missing))
    return {"wheelCount": len(wheels), "requirementCount": len(_locked_requirements(root / "requirements.lock"))}


def _check_host_contract() -> None:
    if platform.system().lower() != "linux":
        return
    machine = platform.machine().lower()
    if machine not in {"x86_64", "amd64"}:
        _fail("FAIL_RELEASE_ARTIFACT_MANIFEST", f"release requires Linux x86_64, not {machine}")


def verify_release_root(
    release_root: Path,
    *,
    expected_sha: str,
    check_host: bool = False,
    allow_runtime_venv: bool = False,
) -> dict[str, Any]:
    """Verify an extracted release without changing any filesystem state."""

    root = _canonical_root(release_root)
    for relative in REQUIRED_FILES:
        _inspect_required_path(root, relative, directory=False)
    for relative in REQUIRED_DIRECTORIES:
        _inspect_required_path(root, relative, directory=True)
    tree = _scan_tree(root, allow_runtime_venv=allow_runtime_venv)
    manifest = _load_manifest(root, expected_sha=expected_sha)
    lock_hashes = {
        "packageLockSha256": sha256_file(root / "package-lock.json"),
        "requirementsLockSha256": sha256_file(root / "requirements.lock"),
    }
    for name, actual in lock_hashes.items():
        if manifest[name] != actual:
            _fail("FAIL_RELEASE_ARTIFACT_LOCK_MISMATCH", f"{name} does not match release bytes")
    wheels = _check_wheelhouse(root)
    if check_host:
        _check_host_contract()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "healthy",
        "releaseRoot": str(root),
        "commitSha": manifest["commitSha"],
        "platform": manifest["platform"],
        "architecture": manifest["architecture"],
        "nodeVersion": manifest["nodeVersion"],
        "npmVersion": manifest["npmVersion"],
        "pythonWheelVersion": manifest["pythonWheelVersion"],
        "verifyWorkflowRunId": manifest["verifyWorkflowRunId"],
        **lock_hashes,
        **tree,
        **wheels,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify an extracted Rardar release artifact")
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser("verify", help="read-only verification of an extracted release")
    verify.add_argument("--release-root", required=True, type=Path)
    verify.add_argument("--expected-sha", required=True)
    verify.add_argument("--check-host", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = verify_release_root(
            arguments.release_root,
            expected_sha=arguments.expected_sha,
            check_host=arguments.check_host,
        )
    except ReleaseArtifactError as error:
        print(json.dumps({"schemaVersion": SCHEMA_VERSION, "status": "failed", "error": error.as_dict()}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
