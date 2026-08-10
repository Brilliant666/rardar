"""Fail-closed, read-only deployment readiness checks for Rardar.

The deployment checker deliberately has no repair or initialization behavior.
Every configured path must already exist. Neither mode writes configured
deployment state, starts services, or changes the published generation. SQLite
main/WAL bytes are copied into private temporary scratch for recovery and
integrity checks; SQLite is never opened against the persistent source.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.generations import GenerationProtocolError, resolve_current_generation
from pipeline.data_lock import data_dir_lock_path, manager_dir_lock_path
from pipeline.runtime_settings import (
    PERSISTENT_PATH_VARIABLES,
    RuntimeSettingsError,
    load_runtime_layout,
    load_runtime_settings,
)


SCHEMA_VERSION = 1
APPLICATION_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024
MAX_HTTP_BODY_BYTES = 1024 * 1024
HTTP_TIMEOUT_SECONDS = 5
MAX_MANAGER_STATUS_AGE_SECONDS = 45
MAX_SCHEDULER_HEARTBEAT_AGE_SECONDS = 130
MAX_CLOCK_FUTURE_SKEW_SECONDS = 300
MINIMUM_NODE_VERSION = (22, 13, 0)
MINIMUM_PYTHON_VERSION = (3, 10, 0)
REQUIRED_PATH_VARIABLES = (
    "RARDAR_HOME",
    "RARDAR_DATA_DIR",
    "RARDAR_RUNTIME_DIR",
    "RARDAR_VINEXT_STATE_DIR",
    "RARDAR_DATA_LOCK_DIR",
)
OPTIONAL_MUTABLE_PATH_VARIABLES = (
    "RARDAR_VITE_CACHE_DIR",
    "RARDAR_BACKUP_DIR",
)
REQUIRED_RELEASE_FILES = (
    "package.json",
    "package-lock.json",
    "pipeline/runtime.py",
    "node_modules/vinext/dist/cli.js",
    "node_modules/vite/bin/vite.js",
    "vite.config.ts",
    ".openai/hosting.json",
    "app/runtime-readiness.mjs",
    "build/published-data-bridge.ts",
    "build/sites-vite-plugin.ts",
    "worker/index.ts",
)
REQUIRED_RELEASE_DIRECTORIES = (
    "dist",
    "deploy/systemd",
)
REQUIRED_RELEASE_PATHS = REQUIRED_RELEASE_FILES + REQUIRED_RELEASE_DIRECTORIES
REQUIRED_RUNTIME_VARIABLES = (
    "RARDAR_VINEXT_PORT",
    "RARDAR_RUNTIME_STATUS_PORT",
    "RARDAR_SCHEDULE_AT",
    "RARDAR_SCHEDULE_TIMEZONE",
    "RARDAR_STALE_AFTER_HOURS",
)
REQUIRED_PERSISTENT_PATH_VARIABLES = PERSISTENT_PATH_VARIABLES
CANONICAL_SYSTEMD_PATHS = {
    "RARDAR_HOME": Path("/opt/rardar/current"),
    "RARDAR_DATA_DIR": Path("/var/lib/rardar/data"),
    "RARDAR_RUNTIME_DIR": Path("/var/lib/rardar/runtime"),
    "RARDAR_VINEXT_STATE_DIR": Path("/var/lib/rardar/vinext-state"),
    "RARDAR_DATA_LOCK_DIR": Path("/var/lib/rardar/locks"),
    "RARDAR_VITE_CACHE_DIR": Path("/var/cache/rardar/vite"),
    "RARDAR_BACKUP_DIR": Path("/var/backups/rardar"),
    "WRANGLER_LOG_PATH": Path("/var/log/rardar/wrangler"),
    "WRANGLER_REGISTRY_PATH": Path("/var/lib/rardar/runtime/wrangler-registry"),
    "MINIFLARE_REGISTRY_PATH": Path("/var/lib/rardar/runtime/miniflare-registry"),
}
SYSTEMD_SQLITE_SCRATCH_ROOT = (
    Path("/tmp")
    if sys.platform.startswith("linux")
    else Path(tempfile.gettempdir()).resolve()
)
SQLITE_SNAPSHOT_ATTEMPTS = 3
RARDAR_TABLE_FINGERPRINT = frozenset(
    {"feedback", "decision_events", "project_actions"}
)
RARDAR_KNOWN_TABLES = frozenset(
    {
        "feedback",
        "decision_events",
        "project_actions",
        "project_action_events",
        "project_action_state",
        "feedback_v2",
        "decision_events_v2",
        "project_action_events_v2",
        "project_action_state_v2",
        "project_identity_catalog",
        "project_identity_generation_evidence",
        "project_identity_runtime",
    }
)
_NODE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_SOCKET_INODE = re.compile(r"^socket:\[(\d+)\]$")
_RFC3339_WITH_TIMEZONE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class DeploymentCheckError(RuntimeError):
    """One deployment invariant is not satisfied."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class DeploymentPaths:
    home_configured: Path
    home: Path
    data: Path
    runtime: Path
    vinext_state: Path
    data_locks: Path
    vite_cache: Path | None = None
    backups: Path | None = None

    def as_dict(self) -> dict[str, str]:
        payload = {
            "home": str(self.home),
            "homeConfigured": str(self.home_configured),
            "homeResolved": str(self.home),
            "data": str(self.data),
            "runtime": str(self.runtime),
            "vinextState": str(self.vinext_state),
            "dataLocks": str(self.data_locks),
        }
        if self.vite_cache is not None:
            payload["viteCache"] = str(self.vite_cache)
        if self.backups is not None:
            payload["backups"] = str(self.backups)
        return payload


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _fail(code: str, detail: str) -> None:
    raise DeploymentCheckError(code, detail)


def _reject_link_components(path: Path, *, allow_leaf: bool) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    for index, component in enumerate(parts):
        current /= component
        try:
            metadata = os.lstat(current)
        except OSError as error:
            _fail("deployment_path_unavailable", f"cannot inspect {current}: {error}")
        if stat.S_ISLNK(metadata.st_mode):
            is_leaf = index == len(parts) - 1
            if not (allow_leaf and is_leaf):
                _fail(
                    "deployment_path_symlink",
                    f"deployment path has an unsafe symbolic-link component: {current}",
                )


def _required_directory(
    name: str,
    source: Mapping[str, str],
    *,
    allow_leaf_symlink: bool = False,
) -> Path:
    raw = source.get(name)
    if not raw or not raw.strip():
        _fail("deployment_path_missing", f"{name} is required")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        _fail("deployment_path_not_absolute", f"{name} must be an absolute path")
    if any(component in {".", ".."} for component in candidate.parts):
        _fail("deployment_path_not_canonical", f"{name} must be traversal-free")
    if not candidate.exists() or not candidate.is_dir():
        _fail("deployment_path_unavailable", f"{name} must name an existing directory")
    _reject_link_components(candidate, allow_leaf=allow_leaf_symlink)
    try:
        canonical = candidate.resolve(strict=True)
    except OSError as error:
        _fail("deployment_path_unavailable", f"cannot resolve {name}: {error}")
    return canonical


def _validate_resolved_home_target(configured: Path, resolved: Path) -> None:
    """Permit one configured leaf link without trusting another target link."""

    if not configured.is_symlink():
        return
    try:
        raw_target = Path(os.readlink(configured))
    except OSError as error:
        _fail("deployment_home_target_unavailable", f"cannot inspect RARDAR_HOME target: {error}")
    if any(component in {".", ".."} for component in raw_target.parts):
        _fail(
            "deployment_home_target_unsafe",
            "RARDAR_HOME leaf symlink target must be traversal-free",
        )
    target = raw_target if raw_target.is_absolute() else configured.parent / raw_target
    target = target.absolute()
    _reject_link_components(target, allow_leaf=False)
    try:
        target_resolved = target.resolve(strict=True)
    except OSError as error:
        _fail("deployment_home_target_unavailable", f"cannot resolve RARDAR_HOME target: {error}")
    if target_resolved != resolved:
        _fail(
            "deployment_home_target_mismatch",
            "RARDAR_HOME target changed while deployment paths were validated",
        )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _load_paths(source: Mapping[str, str]) -> DeploymentPaths:
    resolved = {
        name: _required_directory(
            name,
            source,
            allow_leaf_symlink=name == "RARDAR_HOME",
        )
        for name in REQUIRED_PATH_VARIABLES
    }
    for name in OPTIONAL_MUTABLE_PATH_VARIABLES:
        if source.get(name, "").strip():
            resolved[name] = _required_directory(name, source)
    pairs = list(resolved.items())
    for index, (first_name, first_path) in enumerate(pairs):
        for second_name, second_path in pairs[index + 1 :]:
            if _paths_overlap(first_path, second_path):
                _fail(
                    "deployment_paths_overlap",
                    f"{first_name} and {second_name} must not overlap",
                )
    home_configured = Path(source["RARDAR_HOME"]).expanduser().absolute()
    _validate_resolved_home_target(home_configured, resolved["RARDAR_HOME"])
    return DeploymentPaths(
        home_configured=home_configured,
        home=resolved["RARDAR_HOME"],
        data=resolved["RARDAR_DATA_DIR"],
        runtime=resolved["RARDAR_RUNTIME_DIR"],
        vinext_state=resolved["RARDAR_VINEXT_STATE_DIR"],
        data_locks=resolved["RARDAR_DATA_LOCK_DIR"],
        vite_cache=resolved.get("RARDAR_VITE_CACHE_DIR"),
        backups=resolved.get("RARDAR_BACKUP_DIR"),
    )


def _check_canonical_systemd_layout(source: Mapping[str, str]) -> dict[str, str]:
    """Bind the v1 checker to the writable paths declared by its static unit."""

    checked: dict[str, str] = {}
    for name, expected in CANONICAL_SYSTEMD_PATHS.items():
        raw = source.get(name)
        if not isinstance(raw, str) or not raw.strip():
            _fail("deployment_layout_missing", f"{name} is required by the systemd v1 profile")
        configured = Path(raw).expanduser()
        if not configured.is_absolute():
            _fail("deployment_layout_noncanonical", f"{name} must use the systemd v1 path {expected}")
        configured = configured.absolute()
        if configured != expected:
            _fail("deployment_layout_noncanonical", f"{name} must use the systemd v1 path {expected}")
        checked[name] = str(expected)
    return checked


def _validate_sqlite_scratch_root(
    scratch_root: Path,
    protected_roots: tuple[Path, ...],
) -> Path:
    """Validate the fixed PrivateTmp-backed root without consulting temp env vars."""

    candidate = Path(scratch_root).expanduser()
    if not candidate.is_absolute() or any(component in {".", ".."} for component in candidate.parts):
        _fail("sqlite_scratch_unsafe", "SQLite scratch root must be an absolute canonical path")
    try:
        _reject_link_components(candidate, allow_leaf=False)
        metadata = os.lstat(candidate)
        resolved = candidate.resolve(strict=True)
    except DeploymentCheckError as error:
        _fail("sqlite_scratch_unsafe", f"SQLite scratch root is not trustworthy: {error.detail}")
    except OSError as error:
        _fail("sqlite_scratch_unavailable", f"cannot inspect SQLite scratch root: {error}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("sqlite_scratch_unsafe", "SQLite scratch root must be a real directory")
    mode = stat.S_IMODE(metadata.st_mode)
    if os.name != "nt" and mode & (stat.S_IWGRP | stat.S_IWOTH) and not mode & stat.S_ISVTX:
        _fail(
            "sqlite_scratch_unsafe",
            "shared-writable SQLite scratch root must have the sticky bit",
        )
    if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
        _fail("sqlite_scratch_unavailable", "SQLite scratch root is not usable by the service user")

    # A fixed /tmp may contain isolated test roots, but it must never itself be
    # equal to or located inside the release or persistent state. The concrete
    # mkdtemp directory is checked symmetrically after its atomic creation.
    for protected in protected_roots:
        canonical = Path(protected).resolve(strict=True)
        if resolved == canonical or canonical in resolved.parents:
            _fail(
                "sqlite_scratch_overlap",
                f"SQLite scratch root overlaps protected deployment state: {canonical}",
            )
    return resolved


def _validate_created_sqlite_scratch(
    scratch: Path,
    scratch_root: Path,
    protected_roots: tuple[Path, ...],
) -> Path:
    """Bind one newly-created scratch directory to its trusted root and boundaries."""

    try:
        metadata = os.lstat(scratch)
        resolved = scratch.resolve(strict=True)
    except OSError as error:
        _fail("sqlite_scratch_unavailable", f"cannot inspect SQLite scratch directory: {error}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("sqlite_scratch_unsafe", "SQLite scratch directory must be a real directory")
    if resolved.parent != scratch_root:
        _fail("sqlite_scratch_unsafe", "SQLite scratch directory escaped its fixed root")
    for protected in protected_roots:
        canonical = Path(protected).resolve(strict=True)
        if _paths_overlap(resolved, canonical):
            _fail(
                "sqlite_scratch_overlap",
                f"SQLite scratch directory overlaps protected deployment state: {canonical}",
            )
    return resolved


def _required_absolute_file(name: str, source: Mapping[str, str]) -> Path:
    raw = source.get(name)
    if not raw or not raw.strip():
        _fail("deployment_tool_missing", f"{name} is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        _fail("deployment_tool_not_absolute", f"{name} must be an absolute path")
    if not path.exists() or not path.is_file():
        _fail("deployment_tool_missing", f"{name} does not name an existing file")
    return path


def _parse_node_version(value: str) -> tuple[int, int, int]:
    match = _NODE_VERSION.fullmatch(value.strip())
    if not match:
        _fail("node_version_invalid", f"Node returned an invalid version: {value.strip()!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _check_toolchain(source: Mapping[str, str]) -> dict[str, str]:
    if sys.version_info < MINIMUM_PYTHON_VERSION:
        _fail("python_version_unsupported", "Python 3.10 or newer is required")
    configured_python = _required_absolute_file("RARDAR_PYTHON", source)
    try:
        configured_identity = configured_python.resolve(strict=True)
        running_identity = Path(sys.executable).resolve(strict=True)
    except OSError as error:
        _fail("python_unavailable", f"cannot resolve the Python interpreter: {error}")
    if configured_identity != running_identity:
        _fail(
            "python_interpreter_mismatch",
            "RARDAR_PYTHON must identify the interpreter running the deployment check",
        )

    node = _required_absolute_file("RARDAR_NODE", source)
    try:
        completed = subprocess.run(
            [str(node), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _fail("node_unavailable", f"cannot execute Node: {error}")
    if completed.returncode != 0:
        _fail("node_unavailable", "Node --version exited unsuccessfully")
    node_version = _parse_node_version(completed.stdout)
    if node_version < MINIMUM_NODE_VERSION:
        _fail("node_version_unsupported", "Node 22.13.0 or newer is required")
    return {
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "pythonPath": str(configured_python),
        "node": ".".join(str(part) for part in node_version),
        "nodePath": str(node),
    }


def _check_release(home: Path) -> dict[str, Any]:
    try:
        home = home.resolve(strict=True)
    except OSError as error:
        _fail("deployment_home_target_unavailable", f"cannot resolve release root: {error}")
    if home != APPLICATION_ROOT.resolve(strict=True):
        _fail(
            "deployment_home_mismatch",
            "RARDAR_HOME does not resolve to the release running this deployment check",
        )
    missing: list[str] = []
    unsafe: list[str] = []
    for relative in REQUIRED_RELEASE_PATHS:
        path = home / relative
        if not path.exists():
            missing.append(relative)
            continue
        current = home
        for component in Path(relative).parts:
            current /= component
            try:
                metadata = os.lstat(current)
            except OSError as error:
                _fail("release_path_unavailable", f"cannot inspect release path {current}: {error}")
            if stat.S_ISLNK(metadata.st_mode):
                unsafe.append(str(current.relative_to(home)))
                break
    if missing:
        _fail("release_incomplete", "release files are missing: " + ", ".join(missing))
    if unsafe:
        _fail("release_path_symlink", "release files must not be symlinks: " + ", ".join(unsafe))
    wrong_type = [
        relative
        for relative in REQUIRED_RELEASE_FILES
        if not (home / relative).is_file()
    ] + [
        relative
        for relative in REQUIRED_RELEASE_DIRECTORIES
        if not (home / relative).is_dir()
    ]
    if wrong_type:
        _fail(
            "release_path_type_invalid",
            "release paths have an unexpected type: " + ", ".join(wrong_type),
        )
    environment_search_roots = (home, home / "deploy" / "systemd")
    untrusted_environment_files = sorted(
        str(path.relative_to(home)).replace(os.sep, "/")
        for search_root in environment_search_roots
        if search_root.is_dir()
        for path in search_root.iterdir()
        if (
            path.name.startswith(".dev.vars")
            or (
                path.name.startswith(".env")
                and path.name != ".env.production.example"
            )
        )
    )
    if untrusted_environment_files:
        _fail(
            "release_environment_file_forbidden",
            "release-local environment files are forbidden: "
            + ", ".join(untrusted_environment_files),
        )
    return {"requiredPathCount": len(REQUIRED_RELEASE_PATHS)}


def _minimum_free_bytes(source: Mapping[str, str]) -> int:
    raw = source.get("RARDAR_DEPLOY_MIN_FREE_BYTES", str(DEFAULT_MIN_FREE_BYTES))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _fail("disk_threshold_invalid", "RARDAR_DEPLOY_MIN_FREE_BYTES must be an integer")
    if value < 1:
        _fail("disk_threshold_invalid", "RARDAR_DEPLOY_MIN_FREE_BYTES must be positive")
    return value


def _check_mutable_directory(name: str, path: Path) -> dict[str, Any]:
    """Require a private writable root owned by the effective service user."""

    try:
        metadata = os.lstat(path)
    except OSError as error:
        _fail("deployment_path_unavailable", f"cannot inspect {name}: {error}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("deployment_path_unsafe", f"{name} must be a real directory")

    mode = stat.S_IMODE(metadata.st_mode)
    owner_id = getattr(metadata, "st_uid", None)
    if os.name != "nt":
        effective_owner = os.geteuid()
        if owner_id != effective_owner:
            _fail(
                "deployment_path_owner_mismatch",
                f"{name} must be owned by effective uid {effective_owner}",
            )
        required_owner_mode = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        if mode & required_owner_mode != required_owner_mode:
            _fail(
                "deployment_path_unsafe_mode",
                f"{name} must grant its owner read, write and traverse access",
            )
        if mode & stat.S_IWOTH:
            _fail("deployment_path_unsafe_mode", f"{name} must not be world-writable")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        _fail("deployment_path_not_writable", f"{name} is not readable and writable")
    return {
        "path": str(path),
        "mode": format(mode, "04o"),
        **({"ownerId": owner_id} if owner_id is not None else {}),
    }


def _check_storage(paths: DeploymentPaths, source: Mapping[str, str]) -> dict[str, Any]:
    threshold = _minimum_free_bytes(source)
    free_by_path: dict[str, int] = {}
    mutable_paths: dict[str, Path | None] = {
        "RARDAR_DATA_DIR": paths.data,
        "RARDAR_RUNTIME_DIR": paths.runtime,
        "RARDAR_VINEXT_STATE_DIR": paths.vinext_state,
        "RARDAR_DATA_LOCK_DIR": paths.data_locks,
        "RARDAR_VITE_CACHE_DIR": paths.vite_cache,
        "RARDAR_BACKUP_DIR": paths.backups,
    }
    for name in (
        "WRANGLER_LOG_PATH",
        "WRANGLER_REGISTRY_PATH",
        "MINIFLARE_REGISTRY_PATH",
    ):
        mutable_paths[name] = _required_directory(name, source)
    directory_security: dict[str, dict[str, Any]] = {}
    for name, path in mutable_paths.items():
        if path is None:
            continue
        directory_security[name] = _check_mutable_directory(name, path)
        try:
            free = shutil.disk_usage(path).free
        except OSError as error:
            _fail("disk_check_failed", f"cannot inspect free space for {name}: {error}")
        if free < threshold:
            _fail("disk_space_insufficient", f"{name} has only {free} free bytes")
        free_by_path[name] = free
    return {
        "minimumFreeBytes": threshold,
        "freeBytes": free_by_path,
        "directories": directory_security,
    }


def _probe_lock_file(path: Path) -> str:
    """Probe an existing lock without creating it or changing its bytes."""

    if not os.path.lexists(path):
        return "absent"
    try:
        metadata = os.lstat(path)
    except OSError as error:
        _fail("deployment_lock_unavailable", f"cannot inspect lock {path}: {error}")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail("deployment_lock_unsafe", f"deployment lock is not a regular file: {path}")
    try:
        handle = path.open("rb")
    except OSError as error:
        _fail("deployment_lock_unavailable", f"cannot open lock {path}: {error}")
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                _fail("deployment_lock_held", f"deployment lock is held: {path}")
            _fail("deployment_lock_unavailable", f"cannot probe lock {path}: {error}")
        return "available"
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as error:
                handle.close()
                _fail("deployment_lock_release_failed", f"cannot release lock {path}: {error}")
        handle.close()


def _check_idle_locks(paths: DeploymentPaths) -> dict[str, Any]:
    writer = data_dir_lock_path(paths.data, lock_root=paths.data_locks)
    scheduler = writer.parent / "scheduler-instances" / writer.name
    lock_paths = {
        "manager": manager_dir_lock_path(paths.data, paths.data_locks),
        "dataWriter": writer,
        "scheduler": scheduler,
    }
    return {
        "status": "available",
        "files": {
            name: {"path": str(path), "state": _probe_lock_file(path)}
            for name, path in lock_paths.items()
        },
    }


def _check_runtime_contract(
    source: Mapping[str, str],
    paths: DeploymentPaths,
) -> dict[str, Any]:
    """Validate the exact configuration consumed by the foreground Manager."""

    for name in (*REQUIRED_RUNTIME_VARIABLES, *REQUIRED_PERSISTENT_PATH_VARIABLES):
        raw = source.get(name)
        if not isinstance(raw, str) or not raw.strip():
            _fail("runtime_configuration_missing", f"{name} is required for deployment")

    try:
        layout = load_runtime_layout(source, application_root=APPLICATION_ROOT)
        settings = load_runtime_settings(source)
    except RuntimeSettingsError as error:
        _fail("runtime_configuration_invalid", str(error))

    expected_layout = (
        paths.home,
        paths.data,
        paths.runtime,
        paths.data_locks,
    )
    actual_layout = (
        layout.home,
        layout.data_dir,
        layout.runtime_dir,
        layout.data_lock_dir,
    )
    if actual_layout != expected_layout:
        _fail(
            "runtime_configuration_mismatch",
            "deployment paths do not match the foreground Manager contract",
        )

    persistent_paths: dict[str, Path] = {}
    persistent_security: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_PERSISTENT_PATH_VARIABLES:
        path = _required_directory(name, source)
        if path == paths.home or paths.home in path.parents or path in paths.home.parents:
            _fail(
                "deployment_paths_overlap",
                f"{name} must remain outside the immutable release",
            )
        persistent_paths[name] = path
        persistent_security[name] = _check_mutable_directory(name, path)

    if persistent_paths["RARDAR_VINEXT_STATE_DIR"] != paths.vinext_state:
        _fail(
            "runtime_configuration_mismatch",
            "RARDAR_VINEXT_STATE_DIR does not match the deployment state path",
        )
    if paths.vite_cache is None or persistent_paths["RARDAR_VITE_CACHE_DIR"] != paths.vite_cache:
        _fail(
            "runtime_configuration_mismatch",
            "RARDAR_VITE_CACHE_DIR does not match the deployment cache path",
        )

    # Wrangler and Miniflare state may live below RARDAR_RUNTIME_DIR, as in the
    # versioned environment example, but must not overlap immutable or business
    # data roots and must remain distinct from each other.
    tool_path_names = (
        "WRANGLER_LOG_PATH",
        "WRANGLER_REGISTRY_PATH",
        "MINIFLARE_REGISTRY_PATH",
    )
    protected_roots = tuple(
        path
        for path in (
            paths.home,
            paths.data,
            paths.vinext_state,
            paths.data_locks,
            paths.vite_cache,
            paths.backups,
        )
        if path is not None
    )
    for name in tool_path_names:
        path = persistent_paths[name]
        if any(_paths_overlap(path, protected) for protected in protected_roots):
            _fail("deployment_paths_overlap", f"{name} overlaps protected deployment state")
        if path == paths.runtime or path in paths.runtime.parents:
            _fail(
                "deployment_paths_overlap",
                f"{name} must be a child of RARDAR_RUNTIME_DIR or a separate root",
            )
    for index, first_name in enumerate(tool_path_names):
        for second_name in tool_path_names[index + 1 :]:
            if _paths_overlap(persistent_paths[first_name], persistent_paths[second_name]):
                _fail(
                    "deployment_paths_overlap",
                    f"{first_name} and {second_name} must not overlap",
                )

    return {
        "host": "127.0.0.1",
        "vinextPort": layout.vinext_port,
        "runtimeStatusPort": layout.runtime_status_port,
        "scheduleAt": settings.schedule_at,
        "scheduleTimezone": settings.schedule_timezone,
        "staleAfterHours": settings.stale_after_hours,
        "persistentPaths": {
            name: str(path) for name, path in persistent_paths.items()
        },
        "persistentPathSecurity": persistent_security,
    }


def _walk_regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            _fail("vinext_state_unreadable", f"cannot enumerate {directory}: {error}")
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    _fail("vinext_state_symlink", f"Vinext state contains a symlink: {path}")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(path)
                else:
                    _fail("vinext_state_unsafe_entry", f"Vinext state contains a non-regular entry: {path}")
            except OSError as error:
                _fail("vinext_state_unreadable", f"cannot inspect {path}: {error}")
    return sorted(files)


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError as error:
        _fail("vinext_state_unreadable", f"cannot inspect {path}: {error}")


def _file_digest(path: Path) -> tuple[int, int, int, int, int, str]:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail("sqlite_snapshot_unsafe", f"SQLite state is not a regular file: {path}")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        _fail("sqlite_snapshot_failed", f"cannot read SQLite state {path}: {error}")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        digest.hexdigest(),
    )


def _sqlite_source_members(path: Path) -> tuple[Path, ...]:
    members = [path]
    for suffix in ("-wal", "-journal"):
        candidate = path.with_name(path.name + suffix)
        if os.path.lexists(candidate):
            members.append(candidate)
    return tuple(members)


def _copy_stable_sqlite_snapshot(path: Path, destination: Path) -> None:
    """Copy a byte-stable main/WAL view without opening the source in SQLite."""

    for _ in range(SQLITE_SNAPSHOT_ATTEMPTS):
        members_before = _sqlite_source_members(path)
        before = {member.name: _file_digest(member) for member in members_before}
        for member in members_before:
            suffix = member.name[len(path.name) :]
            target = destination.with_name(destination.name + suffix)
            try:
                shutil.copyfile(member, target)
            except OSError as error:
                _fail("sqlite_snapshot_failed", f"cannot copy SQLite state {member}: {error}")
        members_after = _sqlite_source_members(path)
        after = {member.name: _file_digest(member) for member in members_after}
        copied = {
            name: _file_digest(destination.with_name(destination.name + name[len(path.name) :]))
            for name in before
        }
        copied_content = {name: fingerprint[-1] for name, fingerprint in copied.items()}
        source_content = {name: fingerprint[-1] for name, fingerprint in before.items()}
        if before == after and copied_content == source_content:
            return
        for candidate in destination.parent.iterdir():
            candidate.unlink(missing_ok=True)
    _fail("sqlite_snapshot_unstable", f"SQLite state changed while it was copied: {path}")


def _check_sqlite(
    path: Path,
    *,
    scratch_root: Path,
    protected_roots: tuple[Path, ...],
) -> tuple[set[str], int]:
    # SQLite may keep committed schema and data only in WAL even after a clean
    # Miniflare stop. Opening the source with mode=ro can create or update SHM,
    # while immutable=1 ignores WAL. Copy a stable main/WAL view into private
    # scratch, then allow SQLite to recover and inspect only that copy.
    try:
        scratch = tempfile.TemporaryDirectory(
            prefix="rardar-deployment-sqlite-",
            dir=str(scratch_root),
        )
    except OSError as error:
        _fail("sqlite_snapshot_failed", f"cannot create SQLite inspection scratch: {error}")
    with scratch:
        scratch_directory = _validate_created_sqlite_scratch(
            Path(scratch.name),
            scratch_root,
            protected_roots,
        )
        snapshot = scratch_directory / "database.sqlite"
        _copy_stable_sqlite_snapshot(path, snapshot)
        uri = snapshot.as_uri() + "?mode=rw"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1)
            try:
                schema_version_before = connection.execute("PRAGMA schema_version").fetchone()
                result = connection.execute("PRAGMA quick_check(1)").fetchall()
                if result != [("ok",)]:
                    _fail("sqlite_integrity_failed", f"SQLite quick_check failed for {path}")
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                schema_version_after = connection.execute("PRAGMA schema_version").fetchone()
            finally:
                connection.close()
        except DeploymentCheckError:
            raise
        except (OSError, sqlite3.Error) as error:
            _fail("sqlite_integrity_failed", f"cannot verify SQLite database {path}: {error}")
    if (
        not schema_version_before
        or not schema_version_after
        or schema_version_before != schema_version_after
        or not isinstance(schema_version_before[0], int)
        or isinstance(schema_version_before[0], bool)
    ):
        _fail("sqlite_schema_unstable", f"SQLite schema was not stable for {path}")
    return (
        {str(row[0]) for row in rows if row and isinstance(row[0], str)},
        schema_version_before[0],
    )


def _check_vinext_state(
    root: Path,
    *,
    scratch_root: Path | None = None,
    protected_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    effective_protected_roots = tuple({root.resolve(strict=True), *protected_roots})
    effective_scratch_root = _validate_sqlite_scratch_root(
        SYSTEMD_SQLITE_SCRATCH_ROOT if scratch_root is None else scratch_root,
        effective_protected_roots,
    )
    sqlite_files: list[dict[str, Any]] = []
    rardar_databases: list[dict[str, Any]] = []
    for path in _walk_regular_files(root):
        if not _is_sqlite(path):
            continue
        tables, schema_version = _check_sqlite(
            path,
            scratch_root=effective_scratch_root,
            protected_roots=effective_protected_roots,
        )
        relative = path.relative_to(root).as_posix()
        known_tables = sorted(tables & RARDAR_KNOWN_TABLES)
        summary = {
            "path": relative,
            "schemaVersion": schema_version,
            "tableCount": len(tables),
            "rardarTables": known_tables,
        }
        sqlite_files.append(summary)
        if RARDAR_TABLE_FINGERPRINT.issubset(tables):
            rardar_databases.append(summary)
    if not sqlite_files:
        _fail("d1_database_missing", "Vinext state contains no SQLite database")
    if not rardar_databases:
        _fail("rardar_d1_database_missing", "no SQLite database contains the Rardar table fingerprint")
    if len(rardar_databases) != 1:
        _fail(
            "rardar_d1_database_ambiguous",
            "Vinext state must contain exactly one SQLite database with the Rardar table fingerprint",
        )
    return {
        "scratchRoot": str(effective_scratch_root),
        "sqliteFileCount": len(sqlite_files),
        "rardarDatabaseCount": 1,
        "rardarDatabase": rardar_databases[0],
        "databases": sqlite_files,
    }


def _check_generation(data_dir: Path) -> dict[str, Any]:
    try:
        current = resolve_current_generation(data_dir, verify_audit=True)
    except GenerationProtocolError as error:
        _fail("published_generation_invalid", f"{error.code}: {error}")
    if current.legacy or not current.generation_id or current.pointer is None or current.manifest is None:
        _fail("published_generation_missing", "deployment requires an audited published generation")
    audit = current.manifest.get("audit")
    if not isinstance(audit, dict):
        _fail("published_generation_invalid", "generation manifest has no audit summary")
    error_count = audit.get("errorCount")
    status = audit.get("status")
    if error_count != 0 or status not in {"healthy", "degraded"}:
        _fail("published_generation_audit_failed", "current generation did not pass semantic audit")
    return {
        "generationId": current.generation_id,
        "root": str(current.root),
        "schema": "healthy",
        "audit": status,
        "auditErrorCount": error_count,
        "auditWarningCount": audit.get("warningCount"),
    }


def _check_persistent_state(
    environ: Mapping[str, str] | None,
    *,
    require_idle_locks: bool,
) -> dict[str, Any]:
    source = dict(os.environ if environ is None else environ)
    paths = _load_paths(source)
    runtime_contract = _check_runtime_contract(source, paths)
    systemd_layout = _check_canonical_systemd_layout(source)
    persistent_paths = runtime_contract.get("persistentPaths")
    if not isinstance(persistent_paths, dict):
        _fail("runtime_configuration_invalid", "runtime contract has no persistent paths")
    sqlite_protected_roots = tuple(
        {
            paths.home,
            paths.home_configured,
            paths.data,
            paths.runtime,
            paths.vinext_state,
            paths.data_locks,
            *(
                path
                for path in (paths.vite_cache, paths.backups)
                if path is not None
            ),
            *(
                Path(value)
                for value in persistent_paths.values()
                if isinstance(value, str)
            ),
        }
    )
    sqlite_scratch_root = _validate_sqlite_scratch_root(
        SYSTEMD_SQLITE_SCRATCH_ROOT,
        sqlite_protected_roots,
    )
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "status": "healthy",
        "mode": "offline",
        "checkedAt": _checked_at(),
        "paths": paths.as_dict(),
        "systemdLayout": systemd_layout,
        "toolchain": _check_toolchain(source),
        "release": _check_release(paths.home),
        "storage": _check_storage(paths, source),
        "runtimeContract": runtime_contract,
        "locks": (
            _check_idle_locks(paths)
            if require_idle_locks
            else {"status": "not_checked", "reason": "online_runtime_owns_locks"}
        ),
        "generation": _check_generation(paths.data),
        "d1": _check_vinext_state(
            paths.vinext_state,
            scratch_root=sqlite_scratch_root,
            protected_roots=sqlite_protected_roots,
        ),
    }
    return payload


def check_offline(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run all offline checks, including lock availability, without writes."""

    return _check_persistent_state(environ, require_idle_locks=True)


def _read_http_body(response: http.client.HTTPResponse) -> bytes:
    body = response.read(MAX_HTTP_BODY_BYTES + 1)
    if len(body) > MAX_HTTP_BODY_BYTES:
        _fail("http_response_too_large", "deployment health response exceeds the size limit")
    return body


def _http_request(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=HTTP_TIMEOUT_SECONDS)
    try:
        connection.request("GET", path, headers={"accept": "application/json", "connection": "close"})
        response = connection.getresponse()
        return response.status, _read_http_body(response)
    except (OSError, http.client.HTTPException) as error:
        _fail("runtime_http_unavailable", f"GET {path} on loopback port {port} failed: {error}")
    finally:
        connection.close()


def _http_json(port: int, path: str) -> dict[str, Any]:
    status, body = _http_request(port, path)
    if status != 200:
        _fail("runtime_http_unhealthy", f"GET {path} returned HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail("runtime_http_invalid", f"GET {path} did not return valid UTF-8 JSON: {error}")
    if not isinstance(payload, dict):
        _fail("runtime_http_invalid", f"GET {path} did not return a JSON object")
    return payload


def _http_ok(port: int, path: str) -> None:
    status, _ = _http_request(port, path)
    if status != 200:
        _fail("runtime_http_unhealthy", f"GET {path} returned HTTP {status}")


def _pid_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except OSError:
        return False
    return True


def _require_pid(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not _pid_is_alive(value):
        _fail("runtime_process_unavailable", f"{key} is not a live process")
    return value


def _parse_rfc3339(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_WITH_TIMEZONE.fullmatch(value):
        _fail("runtime_status_invalid", f"{field} must be RFC3339 with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("runtime_status_invalid", f"{field} is not a real calendar timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("runtime_status_invalid", f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _require_recent_timestamp(value: Any, field: str, maximum_age_seconds: int) -> str:
    parsed = _parse_rfc3339(value, field)
    age = (_utc_now() - parsed).total_seconds()
    if age > maximum_age_seconds:
        _fail("runtime_status_stale", f"{field} is stale")
    if age < -MAX_CLOCK_FUTURE_SKEW_SECONDS:
        _fail("runtime_status_invalid", f"{field} is too far in the future")
    assert isinstance(value, str)
    return value


def _decode_proc_address(raw: str, family: int) -> str:
    encoded = bytes.fromhex(raw)
    if family == socket.AF_INET:
        encoded = encoded[::-1]
    else:
        encoded = b"".join(encoded[index : index + 4][::-1] for index in range(0, 16, 4))
    return socket.inet_ntop(family, encoded)


def _linux_listeners(port: int, proc_root: Path = Path("/proc")) -> list[tuple[str, str]]:
    listeners: list[tuple[str, str]] = []
    for relative, family in (("net/tcp", socket.AF_INET), ("net/tcp6", socket.AF_INET6)):
        path = proc_root / relative
        try:
            lines = path.read_text(encoding="ascii").splitlines()[1:]
        except OSError as error:
            _fail("runtime_listener_unavailable", f"cannot inspect {path}: {error}")
        for line in lines:
            columns = line.split()
            if len(columns) < 10 or columns[3] != "0A":
                continue
            address_hex, port_hex = columns[1].split(":", 1)
            if int(port_hex, 16) != port:
                continue
            try:
                address = _decode_proc_address(address_hex, family)
            except (OSError, ValueError) as error:
                _fail("runtime_listener_invalid", f"cannot decode listener on port {port}: {error}")
            listeners.append((address, columns[9]))
    return listeners


def _process_socket_inodes(process_id: int, proc_root: Path = Path("/proc")) -> set[str]:
    inodes: set[str] = set()
    directory = proc_root / str(process_id) / "fd"
    try:
        entries = list(directory.iterdir())
    except OSError as error:
        _fail("runtime_process_unavailable", f"cannot inspect file descriptors for PID {process_id}: {error}")
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        match = _SOCKET_INODE.fullmatch(target)
        if match:
            inodes.add(match.group(1))
    return inodes


def _check_loopback_listener(port: int, owner_pid: int) -> dict[str, Any]:
    if not sys.platform.startswith("linux"):
        _fail("online_platform_unsupported", "online deployment checks require Linux /proc")
    listeners = _linux_listeners(port)
    if not listeners:
        _fail("runtime_listener_missing", f"no listener exists on port {port}")
    if len(listeners) != 1:
        _fail("runtime_listener_not_unique", f"port {port} has multiple listening sockets")
    for address, _ in listeners:
        try:
            loopback = ipaddress.ip_address(address).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            _fail("runtime_listener_public", f"port {port} is bound to non-loopback address {address}")
    expected_inodes = {inode for _, inode in listeners}
    if not expected_inodes & _process_socket_inodes(owner_pid):
        _fail("runtime_listener_owner_mismatch", f"PID {owner_pid} does not own port {port}")
    return {"port": port, "addresses": sorted({address for address, _ in listeners})}


def _check_process_command(process_id: int, expected: tuple[str, ...]) -> None:
    if not sys.platform.startswith("linux"):
        _fail("online_platform_unsupported", "online deployment checks require Linux /proc")
    path = Path("/proc") / str(process_id) / "cmdline"
    try:
        command = path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except OSError as error:
        _fail("runtime_process_unavailable", f"cannot read command line for PID {process_id}: {error}")
    if not all(fragment in command for fragment in expected):
        _fail("runtime_process_identity_mismatch", f"PID {process_id} has an unexpected command line")


def _check_runtime(
    status: Mapping[str, Any],
    expected_contract: Mapping[str, Any],
    expected_paths: Mapping[str, Any],
) -> dict[str, Any]:
    website_port = expected_contract["vinextPort"]
    status_port = expected_contract["runtimeStatusPort"]
    if status.get("schemaVersion") != SCHEMA_VERSION:
        _fail("runtime_status_invalid", "runtime status schemaVersion is unsupported")
    checked_at = _require_recent_timestamp(
        status.get("checkedAt"),
        "checkedAt",
        MAX_MANAGER_STATUS_AGE_SECONDS,
    )
    services = status.get("services")
    data = status.get("data")
    if not isinstance(services, dict) or not isinstance(data, dict):
        _fail("runtime_status_invalid", "runtime status is missing services or data")
    website = services.get("website")
    scheduler = services.get("scheduler")
    if not isinstance(website, dict) or not isinstance(scheduler, dict):
        _fail("runtime_status_invalid", "runtime status is missing managed services")
    state = status.get("state")
    stale_degraded = (
        state == "degraded"
        and data.get("freshness") == "stale"
        and website.get("state") == "healthy"
        and scheduler.get("state") == "healthy"
    )
    if state != "healthy" and not stale_degraded:
        _fail("runtime_status_unhealthy", "runtime is neither healthy nor stale-data degraded")
    if website.get("state") != "healthy" or scheduler.get("state") != "healthy":
        _fail("runtime_service_unhealthy", "website and scheduler must both be healthy")

    runtime_layout = status.get("runtime")
    if not isinstance(runtime_layout, dict):
        _fail("runtime_status_invalid", "runtime status has no runtime layout")
    expected_runtime = {
        "host": "127.0.0.1",
        "home": expected_paths["homeResolved"],
        "dataDir": expected_paths["data"],
        "runtimeDir": expected_paths["runtime"],
        "dataLockDir": expected_paths["dataLocks"],
        "vinextPort": website_port,
        "runtimeStatusPort": status_port,
        "statusUrl": f"http://127.0.0.1:{status_port}/status",
    }
    for name, expected in expected_runtime.items():
        if runtime_layout.get(name) != expected:
            _fail(
                "runtime_configuration_mismatch",
                f"runtime status {name} does not match the deployment environment",
            )

    manager_pid = _require_pid(status, "managerPid")
    website_pid = _require_pid(website, "pid")
    scheduler_pid = _require_pid(scheduler, "pid")
    if len({manager_pid, website_pid, scheduler_pid}) != 3:
        _fail("runtime_process_identity_mismatch", "managed service PIDs must be distinct")
    if (
        scheduler.get("telemetryTrusted") is not True
        or scheduler.get("reportedProcessId") != scheduler_pid
    ):
        _fail("scheduler_telemetry_untrusted", "scheduler telemetry is not bound to its live PID")
    heartbeat_at = _require_recent_timestamp(
        scheduler.get("heartbeatAt"),
        "services.scheduler.heartbeatAt",
        MAX_SCHEDULER_HEARTBEAT_AGE_SECONDS,
    )
    schedule = status.get("schedule")
    if not isinstance(schedule, dict):
        _fail("runtime_status_invalid", "runtime status has no schedule")
    if (
        schedule.get("at") != expected_contract["scheduleAt"]
        or schedule.get("timezone") != expected_contract["scheduleTimezone"]
    ):
        _fail(
            "runtime_configuration_mismatch",
            "runtime schedule does not match the deployment environment",
        )
    expected_stale_seconds = expected_contract["staleAfterHours"] * 60 * 60
    if data.get("staleAfterSeconds") != expected_stale_seconds:
        _fail(
            "runtime_configuration_mismatch",
            "runtime stale threshold does not match the deployment environment",
        )
    next_run_at = schedule.get("nextRunAt")
    _parse_rfc3339(next_run_at, "schedule.nextRunAt")
    _check_process_command(manager_pid, ("pipeline.runtime", "service"))
    expected_vite_cli = str(
        Path(expected_paths["home"]) / "node_modules" / "vite" / "bin" / "vite.js"
    )
    _check_process_command(
        website_pid,
        (
            expected_vite_cli,
            "--configLoader",
            "runner",
            "--host",
            "127.0.0.1",
            "--port",
            str(website_port),
            "--strictPort",
        ),
    )
    _check_process_command(scheduler_pid, ("pipeline.scheduler",))
    return {
        "state": state,
        "checkedAt": checked_at,
        "schedulerHeartbeatAt": heartbeat_at,
        "nextRunAt": next_run_at,
        "managerPid": manager_pid,
        "websitePid": website_pid,
        "schedulerPid": scheduler_pid,
        "listeners": {
            "website": _check_loopback_listener(website_port, website_pid),
            "status": _check_loopback_listener(status_port, manager_pid),
        },
        "status": status,
    }


def check_online(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run offline checks first, then inspect one running loopback runtime."""

    source = dict(os.environ if environ is None else environ)
    offline = _check_persistent_state(source, require_idle_locks=False)
    if not sys.platform.startswith("linux"):
        _fail("online_platform_unsupported", "online deployment checks require Linux /proc")
    website_port = offline["runtimeContract"]["vinextPort"]
    status_port = offline["runtimeContract"]["runtimeStatusPort"]
    if website_port == status_port:
        _fail("runtime_ports_collide", "website and runtime status ports must be distinct")

    runtime_status = _http_json(status_port, "/status")
    runtime = _check_runtime(
        runtime_status,
        offline["runtimeContract"],
        offline["paths"],
    )
    health = _http_json(website_port, "/api/health")
    health_state = health.get("status")
    stale = health_state == "degraded" and health.get("reason") == "published_data_stale"
    if health_state != "healthy" and not stale:
        _fail("website_health_invalid", "health must be healthy or explicitly stale-data degraded")
    health_schedule = health.get("schedule")
    health_data = health.get("data")
    if (
        not isinstance(health_schedule, dict)
        or health_schedule.get("at") != offline["runtimeContract"]["scheduleAt"]
        or health_schedule.get("timezone")
        != offline["runtimeContract"]["scheduleTimezone"]
        or not isinstance(health_data, dict)
        or health_data.get("staleAfterSeconds")
        != offline["runtimeContract"]["staleAfterHours"] * 60 * 60
    ):
        _fail(
            "runtime_configuration_mismatch",
            "website health configuration does not match the deployment environment",
        )
    expected_generation = offline["generation"]["generationId"]
    if health.get("generationId") != expected_generation:
        _fail("runtime_generation_mismatch", "health generation does not match filesystem current")
    runtime_generation = runtime_status.get("data", {}).get("currentGenerationId")
    website_generation = runtime_status.get("services", {}).get("website", {}).get("generationId")
    if runtime_generation != expected_generation or website_generation != expected_generation:
        _fail("runtime_generation_mismatch", "runtime status generation does not match filesystem current")
    for path in ("/", "/signals", "/search"):
        _http_ok(website_port, path)
    final_generation = _check_generation(Path(offline["paths"]["data"]))["generationId"]
    if final_generation != expected_generation:
        _fail("runtime_generation_changed", "current generation changed during deployment check")
    return {
        **offline,
        "mode": "online",
        "checkedAt": _checked_at(),
        "runtime": runtime,
        "http": {
            "health": health_state,
            "reason": health.get("reason"),
            "generationId": expected_generation,
            "paths": ["/api/health", "/", "/signals", "/search"],
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Rardar deployment readiness without repair")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="run a fail-closed deployment readiness check")
    mode = check.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help="validate files and persistent state")
    mode.add_argument("--online", action="store_true", help="also validate the running loopback service")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    mode = "online" if arguments.online else "offline"
    try:
        payload = check_online() if arguments.online else check_offline()
    except DeploymentCheckError as error:
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "status": "failed",
            "mode": mode,
            "checkedAt": _checked_at(),
            "error": error.as_dict(),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    except Exception as error:  # pragma: no cover - last-resort stable CLI envelope
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "status": "failed",
            "mode": mode,
            "checkedAt": _checked_at(),
            "error": {"code": "deployment_check_failed", "detail": str(error)},
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
