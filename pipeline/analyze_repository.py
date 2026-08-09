"""Read-only repository analyzer used by the Rardar candidate pipeline.

The analyzer intentionally does not install dependencies or execute repository
code. It inspects a shallow checkout (or an existing local path) and emits
structured evidence that can be reviewed by an AI analyzer later.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unicodedata
import urllib.request
import uuid
import zipfile
import zlib
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

from pipeline.project_identity import canonicalize_repository, identity_for_repository

from pipeline.schema_validation import (
    ArtifactKind,
    artifact_write_lock,
    atomic_write_validated_json,
    require_valid,
    strict_json_dumps,
)


MAX_TEXT_BYTES = 512_000
MAX_FILES = 12_000
MAX_ARCHIVE_BYTES = 120_000_000
MAX_ARCHIVE_MEMBERS = 100_000
MAX_EXTRACTED_BYTES = 600_000_000
CLONE_TIMEOUT_SECONDS = 180.0
CLONE_TREE_CLEANUP_SECONDS = 10.0
CLONE_TERM_GRACE_SECONDS = 2.0
CLONE_EXIT_GRACE_SECONDS = 0.5
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_WINDOWS_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_TH32CS_SNAPTHREAD = 0x00000004
_WINDOWS_THREAD_SUSPEND_RESUME = 0x0002
_WINDOWS_FILE_ATTRIBUTE_READONLY = 0x00000001
_MAX_READONLY_CLEANUP_REPAIRS = 1024
_ALLOWED_ARCHIVE_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_WINDOWS_RESERVED_COMPONENTS = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


class _WindowsJobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _WindowsIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _WindowsJobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsJobBasicLimitInformation),
        ("IoInfo", _WindowsIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _WindowsJobBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _WindowsThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]
SKIP_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".wrangler",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
SKIP_FILE_SUFFIXES = {
    ".db",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".log",
    ".pdf",
    ".png",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".tsbuildinfo",
    ".woff",
    ".woff2",
}
TEXT_SUFFIXES = {
    ".c",
    ".cpp",
    ".css",
    ".go",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".md",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
    ".vue",
    ".yaml",
    ".yml",
}


@dataclass
class StaticEvidence:
    repository: str
    source: str
    analyzed_at: str
    scanned_files: int
    language_files: dict[str, int]
    indicators: dict[str, bool]
    counts: dict[str, int]
    license_hint: str | None
    confidence: int
    schemaVersion: int = 1
    projectIdVersion: int | None = None
    projectId: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArchiveExtractionSummary:
    checkout: Path
    total_members: int
    eligible_files: int
    selected_files: int

    @property
    def truncated(self) -> bool:
        return self.eligible_files > self.selected_files


@dataclass(frozen=True)
class _ArchiveMember:
    info: zipfile.ZipInfo
    relative: PurePosixPath


@dataclass(frozen=True)
class _ArchivePlan:
    total_members: int
    eligible_files: int
    selected: tuple[_ArchiveMember, ...]


class RemoteCloneLifecycleError(RuntimeError):
    """A remote-analysis lifecycle failure that must never be degraded."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = " ".join(str(detail).split())[:500]
        self.retained_temporary_root: str | None = None
        super().__init__(f"{code}: {self.detail}")

    def retain_temporary_root(self, root: Path) -> None:
        self.retained_temporary_root = str(root)
        self.args = (
            f"{self.code}: {self.detail}; retained temporary root: {self.retained_temporary_root}",
        )


def _evidence_payload(evidence: StaticEvidence) -> dict[str, object]:
    """Serialize evidence without dropping required nullable facts.

    Local scans intentionally remain Schema v1 and therefore omit only the
    v2 identity fields. Values such as ``license_hint: null`` are part of the
    evidence contract and must stay present.
    """

    payload = asdict(evidence)
    if evidence.projectIdVersion is None and evidence.projectId is None:
        payload.pop("projectIdVersion")
        payload.pop("projectId")
    return payload


def _iter_files(root: Path) -> Iterable[Path]:
    seen = 0
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        directories[:] = sorted(
            item
            for item in directories
            if item.lower() not in SKIP_DIRECTORIES and not (current_path / item).is_symlink()
        )
        for name in sorted(files):
            candidate = current_path / name
            if candidate.is_symlink() or Path(name).suffix.lower() in SKIP_FILE_SUFFIXES:
                continue
            seen += 1
            if seen > MAX_FILES:
                return
            yield candidate


def _safe_read(path: Path) -> str:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_TEXT_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _is_test_file(relative: str) -> bool:
    path = Path(relative)
    parts = {part.lower() for part in path.parts[:-1]}
    if parts.intersection({"test", "tests", "__tests__", "spec", "specs"}):
        return True
    name = path.name.lower()
    stem = path.stem.lower()
    return (
        stem in {"test", "tests", "spec"}
        or stem.startswith("test_")
        or stem.endswith(("_test", "_spec"))
        or ".test." in name
        or ".spec." in name
    )


def _license_hint(root: Path) -> str | None:
    candidates = [
        path
        for path in root.iterdir()
        if not path.is_symlink() and path.is_file() and path.name.lower().startswith(("license", "copying"))
    ]
    if not candidates:
        return None
    content = _safe_read(candidates[0]).lower()
    signatures = {
        "Apache-2.0": "apache license",
        "MIT": "mit license",
        "GPL": "gnu general public license",
        "BSD": "redistribution and use in source and binary forms",
    }
    return next((name for name, signature in signatures.items() if signature in content), "存在许可证文件")


def analyze_path(root: Path, repository: str = "local") -> StaticEvidence:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")

    files = list(_iter_files(root))
    relative_names = [path.relative_to(root).as_posix().lower() for path in files]
    language_files: dict[str, int] = {}
    todo_count = 0
    test_files = 0

    for path, relative in zip(files, relative_names):
        suffix = path.suffix.lower() or "[none]"
        language_files[suffix] = language_files.get(suffix, 0) + 1
        if _is_test_file(relative):
            test_files += 1
        if suffix in TEXT_SUFFIXES:
            content = _safe_read(path)
            todo_count += len(re.findall(r"\b(?:TODO|FIXME|XXX)\b", content, flags=re.IGNORECASE))

    names = set(relative_names)
    indicators = {
        "readme": any(Path(name).name.startswith("readme") for name in names),
        "license": any(Path(name).name.startswith(("license", "copying")) for name in names),
        "tests": test_files > 0,
        "ci": any(name.startswith(".github/workflows/") for name in names),
        "docker": any(Path(name).name in {"dockerfile", "docker-compose.yml", "compose.yml"} for name in names),
        "dependency_lock": any(Path(name).name in {"package-lock.json", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "cargo.lock", "go.sum"} for name in names),
        "package_manifest": any(Path(name).name in {"package.json", "pyproject.toml", "setup.py", "cargo.toml", "go.mod"} for name in names),
        "examples": any(name.startswith(("examples/", "example/", "demo/")) for name in names),
        "docs": any(name.startswith(("docs/", "doc/")) for name in names),
        "environment_example": any(Path(name).name in {".env.example", ".env.sample"} for name in names),
    }

    confidence = min(95, 35 + sum(6 for present in indicators.values() if present) + min(test_files, 12))
    warnings: list[str] = []
    if len(files) >= MAX_FILES:
        warnings.append(f"file scan stopped at {MAX_FILES} files")
    if not indicators["license"]:
        warnings.append("no license file detected")
    warnings.append("static inspection only; code was not executed")

    return StaticEvidence(
        repository=repository,
        source=str(root),
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        scanned_files=len(files),
        language_files=dict(sorted(language_files.items(), key=lambda item: item[1], reverse=True)[:12]),
        indicators=indicators,
        counts={"test_files": test_files, "todo_markers": todo_count},
        license_hint=_license_hint(root),
        confidence=confidence,
        warnings=warnings,
    )


def _validate_repo(repo: str) -> str:
    # Validate the exact identity input. URLs, whitespace, trailing slashes,
    # and a guessed ``.git`` suffix are not normalized; ``repo.git`` can be a
    # legitimate repository name and must retain its own stable identity.
    canonicalize_repository(repo)
    return repo


def _git_environment() -> dict[str, str]:
    """Isolate read-only clones from user-level URL rewrites and proxy rules."""
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _owned_directory_identity(path: Path) -> tuple[int, int]:
    current = os.lstat(path)
    if (
        stat.S_ISLNK(current.st_mode)
        or _is_reparse_point(path)
        or not stat.S_ISDIR(current.st_mode)
    ):
        raise RuntimeError(f"owned cleanup root is not a regular directory: {path}")
    return current.st_dev, current.st_ino


def _verify_owned_directory_identity(
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    if _owned_directory_identity(path) != expected_identity:
        raise RuntimeError(f"owned cleanup root changed identity: {path}")


def _preflight_owned_tree(path: Path, *, allow_leaf_symlinks: bool) -> None:
    with os.scandir(path) as entries:
        for entry in entries:
            child = Path(entry.path)
            current = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(current.st_mode):
                if not allow_leaf_symlinks:
                    raise RuntimeError(f"owned cleanup tree contains a symbolic link: {child}")
                continue
            if _is_reparse_point(child):
                raise RuntimeError(f"owned cleanup tree contains a reparse point: {child}")
            if stat.S_ISDIR(current.st_mode):
                _preflight_owned_tree(child, allow_leaf_symlinks=allow_leaf_symlinks)
                continue
            if not stat.S_ISREG(current.st_mode):
                raise RuntimeError(f"owned cleanup tree contains an unsupported entry: {child}")


def _remove_owned_tree(
    root: Path,
    expected_identity: tuple[int, int],
    *,
    allow_leaf_symlinks: bool,
) -> None:
    """Remove one analyzer-owned tree without following filesystem links.

    Windows Git marks partial-clone promisor packs read-only. The narrowly
    scoped rmtree callback clears only that bit on an identity-bound regular
    file after process-tree shutdown. ACL failures, sharing violations,
    hardlinks, reparse points, and any identity change remain fail-closed.
    """

    root = root.absolute()
    _verify_owned_directory_identity(root, expected_identity)
    _preflight_owned_tree(root, allow_leaf_symlinks=allow_leaf_symlinks)
    _verify_owned_directory_identity(root, expected_identity)
    repaired_paths: set[str] = set()

    def remove_readonly_file(
        function: object,
        failed_path: str,
        error_info: tuple[object, BaseException, object],
    ) -> None:
        error = error_info[1]
        candidate = Path(failed_path).absolute()
        normalized_candidate = os.path.normcase(str(candidate))
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            raise error
        if (
            os.name != "nt"
            or function not in {os.unlink, os.remove}
            or not isinstance(error, PermissionError)
            or getattr(error, "winerror", None) != 5
            or not relative.parts
            or normalized_candidate in repaired_paths
            or len(repaired_paths) >= _MAX_READONLY_CLEANUP_REPAIRS
        ):
            raise error

        _verify_owned_directory_identity(root, expected_identity)
        parent = root
        for component in relative.parts[:-1]:
            parent /= component
            parent_status = os.lstat(parent)
            if (
                stat.S_ISLNK(parent_status.st_mode)
                or _is_reparse_point(parent)
                or not stat.S_ISDIR(parent_status.st_mode)
            ):
                raise error

        current = os.lstat(candidate)
        current_identity = (current.st_dev, current.st_ino, current.st_size)
        attributes = getattr(current, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(candidate)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or not attributes & _WINDOWS_FILE_ATTRIBUTE_READONLY
        ):
            raise error

        os.chmod(candidate, current.st_mode | stat.S_IWRITE)
        after = os.lstat(candidate)
        if (
            (after.st_dev, after.st_ino, after.st_size) != current_identity
            or stat.S_ISLNK(after.st_mode)
            or _is_reparse_point(candidate)
            or not stat.S_ISREG(after.st_mode)
            or getattr(after, "st_file_attributes", 0)
            & _WINDOWS_FILE_ATTRIBUTE_READONLY
        ):
            raise RuntimeError(f"owned cleanup entry changed during readonly repair: {candidate}")
        repaired_paths.add(normalized_candidate)
        function(failed_path)  # type: ignore[operator]

    shutil.rmtree(root, onerror=remove_readonly_file)
    if os.path.lexists(root):
        raise RuntimeError(f"owned cleanup tree still exists: {root}")


def _unsafe_archive_path(filename: str) -> RuntimeError:
    return RuntimeError(f"unsafe source archive path: {filename}")


def _validate_archive_component(component: str, filename: str) -> None:
    if (
        component in {"", ".", ".."}
        or component.endswith((" ", "."))
        or ":" in component
        or any(ord(character) < 32 or ord(character) == 127 for character in component)
        or len(component.encode("utf-8")) > 255
    ):
        raise _unsafe_archive_path(filename)
    if component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_COMPONENTS:
        raise _unsafe_archive_path(filename)


def _archive_member_kind(item: zipfile.ZipInfo) -> str:
    if item.is_dir():
        return "directory"
    mode = (item.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        return "symlink"
    if file_type in {0, stat.S_IFREG}:
        return "file"
    return "special"


def _preflight_source_archive(archive: zipfile.ZipFile) -> _ArchivePlan:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise RuntimeError(f"source archive exceeds {MAX_ARCHIVE_MEMBERS} members")

    repository_root: str | None = None
    path_kinds: dict[str, str] = {}
    eligible: list[_ArchiveMember] = []
    for item in infos:
        original_filename = getattr(item, "orig_filename", item.filename)
        filename = item.filename
        if (
            not filename
            or "\x00" in original_filename
            or "\\" in filename
            or filename.startswith(("/", "//"))
            or item.flag_bits & 0x1
            or item.compress_type not in _ALLOWED_ARCHIVE_COMPRESSION
            or item.file_size < 0
            or item.compress_size < 0
        ):
            raise _unsafe_archive_path(filename)

        stripped = filename[:-1] if item.is_dir() and filename.endswith("/") else filename
        raw_parts = stripped.split("/")
        if (
            not raw_parts
            or len(raw_parts) > 256
            or any(part in {"", ".", ".."} for part in raw_parts)
        ):
            raise _unsafe_archive_path(filename)
        for component in raw_parts:
            _validate_archive_component(component, filename)

        current_root = raw_parts[0]
        if repository_root is None:
            repository_root = current_root
        elif unicodedata.normalize("NFC", current_root) != unicodedata.normalize(
            "NFC", repository_root
        ):
            raise RuntimeError("source archive contains multiple repository roots")

        relative_parts = raw_parts[1:]
        kind = _archive_member_kind(item)
        if kind == "special":
            raise RuntimeError(f"unsupported source archive member: {filename}")
        if not relative_parts:
            if kind != "directory":
                raise _unsafe_archive_path(filename)
            continue

        relative_text = "/".join(relative_parts)
        if len(relative_text.encode("utf-8")) > 4096:
            raise _unsafe_archive_path(filename)
        relative = PurePosixPath(*relative_parts)
        key = "/".join(unicodedata.normalize("NFC", part).casefold() for part in relative.parts)
        if key in path_kinds:
            raise RuntimeError(f"duplicate source archive path: {filename}")
        path_kinds[key] = kind

        if kind == "file" and not any(
            part.casefold() in SKIP_DIRECTORIES for part in relative.parts[:-1]
        ) and relative.suffix.casefold() not in SKIP_FILE_SUFFIXES:
            eligible.append(_ArchiveMember(info=item, relative=relative))

    if repository_root is None:
        raise RuntimeError("source archive is empty")

    for key, kind in path_kinds.items():
        parts = key.split("/")
        for length in range(1, len(parts)):
            parent_kind = path_kinds.get("/".join(parts[:length]))
            if parent_kind is not None and parent_kind != "directory":
                raise RuntimeError(f"source archive file-directory collision: {key}")
    eligible.sort(
        key=lambda member: unicodedata.normalize("NFC", member.relative.as_posix()).encode("utf-8")
    )
    selected = tuple(eligible[:MAX_FILES])
    selected_bytes = sum(member.info.file_size for member in selected)
    if selected_bytes > MAX_EXTRACTED_BYTES:
        raise RuntimeError(
            f"source archive selected files exceed {MAX_EXTRACTED_BYTES} extracted bytes"
        )
    return _ArchivePlan(
        total_members=len(infos),
        eligible_files=len(eligible),
        selected=selected,
    )


def _cleanup_created_checkout(
    checkout: Path,
    identity: tuple[int, int] | None,
) -> None:
    if not os.path.lexists(checkout):
        return
    current = os.lstat(checkout)
    if (
        (identity is not None and (current.st_dev, current.st_ino) != identity)
        or stat.S_ISLNK(current.st_mode)
        or _is_reparse_point(checkout)
        or not stat.S_ISDIR(current.st_mode)
    ):
        raise RuntimeError(f"source archive checkout changed during cleanup: {checkout}")
    shutil.rmtree(checkout)
    if os.path.lexists(checkout):
        raise RuntimeError(f"source archive checkout cleanup failed: {checkout}")


def _extract_source_archive(archive_path: Path, checkout: Path) -> ArchiveExtractionSummary:
    checkout = checkout.absolute()
    parent = checkout.parent
    if not parent.is_dir() or parent.is_symlink() or _is_reparse_point(parent):
        raise RuntimeError(f"unsafe source archive checkout parent: {parent}")
    if os.path.lexists(checkout):
        raise RuntimeError(f"source archive checkout already exists: {checkout}")

    with zipfile.ZipFile(archive_path) as archive:
        plan = _preflight_source_archive(archive)
        staging = parent / f".{checkout.name}.partial-{uuid.uuid4().hex}"
        identity: tuple[int, int] | None = None
        staging_created_by_us = False
        try:
            staging.mkdir(parents=False, exist_ok=False)
            staging_created_by_us = True
            created = os.lstat(staging)
            identity = (created.st_dev, created.st_ino)
            actual_total = 0
            staging_root = staging.resolve(strict=True)
            for member in plan.selected:
                target = staging.joinpath(*member.relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                resolved_parent = target.parent.resolve(strict=True)
                if resolved_parent != staging_root and staging_root not in resolved_parent.parents:
                    raise RuntimeError(
                        f"source archive escapes checkout: {member.info.filename}"
                    )

                write_content = member.info.file_size <= MAX_TEXT_BYTES
                crc = 0
                actual_file = 0
                with archive.open(member.info) as source, target.open("xb") as destination:
                    while chunk := source.read(64 * 1024):
                        actual_file += len(chunk)
                        actual_total += len(chunk)
                        if actual_file > member.info.file_size or actual_total > MAX_EXTRACTED_BYTES:
                            raise RuntimeError(
                                f"source archive exceeds {MAX_EXTRACTED_BYTES} extracted bytes"
                            )
                        crc = zlib.crc32(chunk, crc)
                        if write_content:
                            destination.write(chunk)
                if actual_file != member.info.file_size or crc & 0xFFFFFFFF != member.info.CRC:
                    raise RuntimeError(f"source archive member integrity mismatch: {member.info.filename}")

            final = os.lstat(staging)
            if (
                identity is None
                or (final.st_dev, final.st_ino) != identity
                or stat.S_ISLNK(final.st_mode)
                or _is_reparse_point(staging)
            ):
                raise RuntimeError("source archive checkout changed during extraction")
            if os.path.lexists(checkout):
                raise RuntimeError(f"source archive checkout appeared during extraction: {checkout}")
            os.replace(staging, checkout)
        except BaseException as error:
            if staging_created_by_us:
                try:
                    _cleanup_created_checkout(staging, identity)
                except Exception as cleanup_error:
                    raise RuntimeError(
                        "source archive extraction failed and checkout cleanup failed: "
                        f"{cleanup_error}"
                    ) from error
            raise

    return ArchiveExtractionSummary(
        checkout=checkout,
        total_members=plan.total_members,
        eligible_files=plan.eligible_files,
        selected_files=len(plan.selected),
    )


def _download_source_archive(repository: str, directory: Path) -> ArchiveExtractionSummary:
    archive_path = directory / "source.zip"
    partial_path = directory / "source.zip.part"
    if os.path.lexists(archive_path) or os.path.lexists(partial_path):
        raise RuntimeError("source archive download target already exists")
    request = urllib.request.Request(
        f"https://codeload.github.com/{repository}/zip/HEAD",
        headers={"user-agent": "rardar-static-analyzer/0.1"},
    )
    total = 0
    partial_identity: tuple[int, int] | None = None
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw_length = getattr(response, "headers", {}).get("Content-Length")
            if raw_length is not None:
                try:
                    content_length = int(raw_length)
                except (TypeError, ValueError) as error:
                    raise RuntimeError("invalid source archive Content-Length") from error
                if content_length < 0 or content_length > MAX_ARCHIVE_BYTES:
                    raise RuntimeError(
                        f"source archive exceeds {MAX_ARCHIVE_BYTES} download bytes"
                    )
            with partial_path.open("xb") as output:
                opened = os.fstat(output.fileno())
                partial_identity = (opened.st_dev, opened.st_ino)
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise RuntimeError(
                            f"source archive exceeds {MAX_ARCHIVE_BYTES} download bytes"
                        )
                    output.write(chunk)
        os.replace(partial_path, archive_path)
    except BaseException as error:
        if partial_identity is not None and os.path.lexists(partial_path):
            try:
                current = os.lstat(partial_path)
                if (
                    (current.st_dev, current.st_ino) != partial_identity
                    or stat.S_ISLNK(current.st_mode)
                    or _is_reparse_point(partial_path)
                    or not stat.S_ISREG(current.st_mode)
                ):
                    raise RuntimeError("source archive partial download changed during cleanup")
                partial_path.unlink()
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"source archive download failed and partial cleanup failed: {cleanup_error}"
                ) from error
        raise

    checkout = directory / "archive-repo"
    return _extract_source_archive(archive_path, checkout)


def _windows_kernel32() -> object:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsThreadEntry),
    ]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsThreadEntry),
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _windows_error(operation: str) -> OSError:
    return OSError(f"{operation} failed with Windows error {ctypes.get_last_error()}")


def _close_windows_handle(kernel32: object, handle: int) -> None:
    if handle and not kernel32.CloseHandle(wintypes.HANDLE(handle)):
        raise _windows_error("CloseHandle")


def _create_windows_job() -> tuple[object, int]:
    kernel32 = _windows_kernel32()
    raw_handle = kernel32.CreateJobObjectW(None, None)
    if not raw_handle:
        raise _windows_error("CreateJobObjectW")
    handle = int(raw_handle)
    information = _WindowsJobExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = (
        _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    if not kernel32.SetInformationJobObject(
        wintypes.HANDLE(handle),
        _WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = _windows_error("SetInformationJobObject")
        try:
            _close_windows_handle(kernel32, handle)
        except OSError as cleanup_error:
            raise RemoteCloneLifecycleError(
                "remote_clone_process_tree_cleanup_failed", cleanup_error
            ) from error
        raise error
    return kernel32, handle


def _resume_windows_process(kernel32: object, process: subprocess.Popen[bytes]) -> None:
    raw_snapshot = kernel32.CreateToolhelp32Snapshot(_WINDOWS_TH32CS_SNAPTHREAD, 0)
    snapshot = int(raw_snapshot)
    if snapshot == ctypes.c_void_p(-1).value:
        raise _windows_error("CreateToolhelp32Snapshot")
    resumed = 0
    try:
        entry = _WindowsThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        has_entry = bool(kernel32.Thread32First(wintypes.HANDLE(snapshot), ctypes.byref(entry)))
        while has_entry:
            if entry.th32OwnerProcessID == process.pid:
                raw_thread = kernel32.OpenThread(
                    _WINDOWS_THREAD_SUSPEND_RESUME,
                    False,
                    entry.th32ThreadID,
                )
                if not raw_thread:
                    raise _windows_error("OpenThread")
                thread = int(raw_thread)
                try:
                    if kernel32.ResumeThread(wintypes.HANDLE(thread)) == 0xFFFFFFFF:
                        raise _windows_error("ResumeThread")
                    resumed += 1
                finally:
                    _close_windows_handle(kernel32, thread)
            has_entry = bool(
                kernel32.Thread32Next(wintypes.HANDLE(snapshot), ctypes.byref(entry))
            )
    finally:
        _close_windows_handle(kernel32, snapshot)
    if resumed == 0:
        raise RuntimeError("suspended clone process has no resumable thread")


def _terminate_uncontained_windows_process(
    process: subprocess.Popen[bytes],
    kernel32: object,
    job_handle: int,
    cause: BaseException,
) -> None:
    cleanup_error: BaseException | None = None
    try:
        process.kill()
        process.wait(timeout=CLONE_TREE_CLEANUP_SECONDS)
    except BaseException as error:
        cleanup_error = error
    try:
        _close_windows_handle(kernel32, job_handle)
    except OSError as error:
        cleanup_error = cleanup_error or error
    if cleanup_error is not None:
        raise RemoteCloneLifecycleError(
            "remote_clone_process_tree_cleanup_failed", cleanup_error
        ) from cause
    raise RemoteCloneLifecycleError("remote_clone_containment_failed", cause) from cause


def _spawn_clone(command: list[str], environment: dict[str, str]) -> subprocess.Popen[bytes]:
    options: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": environment,
        "close_fds": True,
        "shell": False,
    }
    if os.name != "nt":
        options["start_new_session"] = True
        return subprocess.Popen(command, **options)

    kernel32, job_handle = _create_windows_job()
    options["creationflags"] = (
        _WINDOWS_CREATE_SUSPENDED
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    try:
        process = subprocess.Popen(command, **options)
    except OSError as error:
        try:
            _close_windows_handle(kernel32, job_handle)
        except OSError as cleanup_error:
            raise RemoteCloneLifecycleError(
                "remote_clone_process_tree_cleanup_failed", cleanup_error
            ) from error
        raise
    try:
        process_handle = wintypes.HANDLE(int(getattr(process, "_handle")))
        if not kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job_handle), process_handle
        ):
            raise _windows_error("AssignProcessToJobObject")
        setattr(process, "_rardar_job_handle", job_handle)
        _resume_windows_process(kernel32, process)
    except BaseException as error:
        if getattr(process, "_rardar_job_handle", None) == job_handle:
            try:
                _terminate_clone_tree(process)
            except RemoteCloneLifecycleError as cleanup_error:
                raise cleanup_error from error
            raise RemoteCloneLifecycleError(
                "remote_clone_containment_failed", error
            ) from error
        _terminate_uncontained_windows_process(process, kernel32, job_handle, error)
    return process


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _windows_job_active_processes(kernel32: object, job_handle: int) -> int:
    information = _WindowsJobBasicAccountingInformation()
    if not kernel32.QueryInformationJobObject(
        wintypes.HANDLE(job_handle),
        _WINDOWS_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
        None,
    ):
        raise _windows_error("QueryInformationJobObject")
    return int(information.ActiveProcesses)


def _terminate_clone_tree_windows(
    process: subprocess.Popen[bytes],
    deadline: float,
) -> bool:
    job_handle = getattr(process, "_rardar_job_handle", None)
    if not isinstance(job_handle, int) or job_handle <= 0:
        raise RuntimeError("clone process is missing its Windows Job Object")
    kernel32 = _windows_kernel32()
    try:
        active_processes = _windows_job_active_processes(kernel32, job_handle)
        natural_exit_deadline = min(
            deadline,
            time.monotonic() + CLONE_EXIT_GRACE_SECONDS,
        )
        while active_processes and time.monotonic() < natural_exit_deadline:
            time.sleep(0.05)
            active_processes = _windows_job_active_processes(kernel32, job_handle)
        had_active_processes = active_processes > 0
        if active_processes and not kernel32.TerminateJobObject(
            wintypes.HANDLE(job_handle), 1
        ):
            raise _windows_error("TerminateJobObject")
        while active_processes:
            if _remaining_seconds(deadline) <= 0:
                raise RuntimeError("Windows clone job did not become empty")
            time.sleep(0.05)
            active_processes = _windows_job_active_processes(kernel32, job_handle)
        remaining = _remaining_seconds(deadline)
        if remaining <= 0:
            raise RuntimeError("cleanup deadline expired")
        process.wait(timeout=remaining)
    except BaseException:
        try:
            kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1)
        except Exception:
            pass
        try:
            _close_windows_handle(kernel32, job_handle)
        except OSError:
            pass
        setattr(process, "_rardar_job_handle", None)
        raise
    _close_windows_handle(kernel32, job_handle)
    setattr(process, "_rardar_job_handle", None)
    return had_active_processes


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(
    process: subprocess.Popen[bytes],
    process_group: int,
    deadline: float,
) -> bool:
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.05)
    return False


def _terminate_clone_group_posix(
    process: subprocess.Popen[bytes],
    deadline: float,
) -> bool:
    process_group = process.pid
    had_active_processes = _process_group_exists(process_group)
    if had_active_processes:
        natural_exit_deadline = min(
            deadline,
            time.monotonic() + CLONE_EXIT_GRACE_SECONDS,
        )
        if _wait_for_process_group_exit(process, process_group, natural_exit_deadline):
            had_active_processes = False
    if not had_active_processes:
        remaining = _remaining_seconds(deadline)
        if remaining <= 0:
            raise RuntimeError("cleanup deadline expired")
        process.wait(timeout=remaining)
        return False
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as error:
        raise RemoteCloneLifecycleError(
            "remote_clone_process_tree_cleanup_failed", error
        ) from error
    term_deadline = min(deadline, time.monotonic() + CLONE_TERM_GRACE_SECONDS)
    if not _wait_for_process_group_exit(process, process_group, term_deadline):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RemoteCloneLifecycleError(
                "remote_clone_process_tree_cleanup_failed", error
            ) from error
        if not _wait_for_process_group_exit(process, process_group, deadline):
            raise RemoteCloneLifecycleError(
                "remote_clone_process_tree_cleanup_failed", "clone process group did not exit"
            )
    try:
        process.wait(timeout=_remaining_seconds(deadline))
    except subprocess.TimeoutExpired as error:
        raise RemoteCloneLifecycleError(
            "remote_clone_process_tree_cleanup_failed", "clone root did not exit"
        ) from error
    return True


def _terminate_clone_tree(
    process: subprocess.Popen[bytes],
    *,
    timeout_seconds: float = CLONE_TREE_CLEANUP_SECONDS,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    try:
        if os.name == "nt":
            return _terminate_clone_tree_windows(process, deadline)
        return _terminate_clone_group_posix(process, deadline)
    except RemoteCloneLifecycleError:
        raise
    except Exception as error:
        raise RemoteCloneLifecycleError(
            "remote_clone_process_tree_cleanup_failed", error
        ) from error


def _run_bounded_clone(
    command: list[str],
    environment: dict[str, str],
    *,
    timeout_seconds: float = CLONE_TIMEOUT_SECONDS,
) -> str | None:
    try:
        process = _spawn_clone(command, environment)
    except OSError as error:
        detail = " ".join(str(error).split())[:240]
        return f"shallow clone could not start: {detail}"
    timed_out = False
    wait_error: BaseException | None = None
    return_code: int | None = None
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    except BaseException as error:
        wait_error = error
    try:
        had_active_processes = _terminate_clone_tree(process)
    except RemoteCloneLifecycleError as cleanup_error:
        raise cleanup_error from wait_error
    if wait_error is not None:
        raise wait_error
    if timed_out:
        return f"shallow clone timed out after {timeout_seconds:g} seconds"
    if return_code == 0:
        if had_active_processes:
            raise RemoteCloneLifecycleError(
                "remote_clone_unexpected_descendants",
                "successful clone root exited while contained descendants were still active",
            )
        return None
    if return_code is None:
        raise RuntimeError("clone process produced no exit status")
    return f"shallow clone failed with exit code {return_code}"


def _remove_partial_checkout(checkout: Path, temporary_root: Path) -> None:
    if not os.path.lexists(checkout):
        return
    try:
        if checkout.parent.resolve(strict=True) != temporary_root.resolve(strict=True):
            raise RuntimeError("partial checkout is outside the analyzer temporary root")
        current = os.lstat(checkout)
        if (
            stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(checkout)
            or not stat.S_ISDIR(current.st_mode)
        ):
            raise RuntimeError("partial checkout is not a regular directory")
        _remove_owned_tree(
            checkout,
            (current.st_dev, current.st_ino),
            allow_leaf_symlinks=True,
        )
    except (OSError, RuntimeError) as error:
        raise RemoteCloneLifecycleError(
            "remote_clone_checkout_cleanup_failed", error
        ) from error


def _analyze_remote_in_temporary_root(normalized: str, temporary_root: Path) -> StaticEvidence:
    checkout = temporary_root / "repo"
    command = [
        "git",
        "clone",
        "--depth",
        "1",
        "--filter=blob:limit=512k",
        "--no-tags",
        "--single-branch",
        f"https://github.com/{normalized}.git",
        str(checkout),
    ]
    clone_error = _run_bounded_clone(command, _git_environment())
    source_root = checkout
    archive_summary: ArchiveExtractionSummary | None = None
    if clone_error:
        _remove_partial_checkout(checkout, temporary_root)
        try:
            archive_summary = _download_source_archive(normalized, temporary_root)
            source_root = archive_summary.checkout
        except RemoteCloneLifecycleError:
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise RuntimeError(
                f"{clone_error}; source archive fallback failed for {normalized}: {error}"
            ) from None
    evidence = analyze_path(source_root, normalized)
    identity = identity_for_repository(normalized)
    evidence.schemaVersion = 2
    evidence.projectIdVersion = identity.project_id_version
    evidence.projectId = identity.project_id
    evidence.source = f"https://github.com/{normalized}"
    if clone_error:
        evidence.warnings.append(
            f"{clone_error}; inspected a bounded official GitHub source archive instead"
        )
    if archive_summary is not None and archive_summary.truncated:
        evidence.warnings.append(
            "official source archive deterministic selection capped at "
            f"{archive_summary.selected_files} of {archive_summary.eligible_files} eligible files"
        )
    return evidence


def analyze_remote(repo: str) -> StaticEvidence:
    normalized = _validate_repo(repo)
    temporary_root = Path(tempfile.mkdtemp(prefix="rardar-"))
    temporary_root_identity = _owned_directory_identity(temporary_root)
    active_error: BaseException | None = None
    try:
        return _analyze_remote_in_temporary_root(normalized, temporary_root)
    except BaseException as error:
        active_error = error
        raise
    finally:
        preserve_for_diagnostics = isinstance(active_error, RemoteCloneLifecycleError) and (
            active_error.code
            in {
                "remote_clone_process_tree_cleanup_failed",
                "remote_clone_checkout_cleanup_failed",
            }
        )
        if not preserve_for_diagnostics:
            try:
                _remove_owned_tree(
                    temporary_root,
                    temporary_root_identity,
                    allow_leaf_symlinks=True,
                )
            except (OSError, RuntimeError) as cleanup_error:
                detail = str(cleanup_error)
                if active_error is not None:
                    detail = f"{detail}; previous error: {active_error}"
                failure = RemoteCloneLifecycleError(
                    "remote_analysis_temporary_cleanup_failed", detail
                )
                failure.retain_temporary_root(temporary_root)
                raise failure from cleanup_error
        elif isinstance(active_error, RemoteCloneLifecycleError):
            active_error.retain_temporary_root(temporary_root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate read-only static evidence for a repository")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="public GitHub owner/name")
    source.add_argument("--path", type=Path, help="existing local repository path")
    parser.add_argument("--out", type=Path, help="optional output JSON path")
    arguments = parser.parse_args()

    evidence = analyze_remote(arguments.repo) if arguments.repo else analyze_path(arguments.path)
    payload = _evidence_payload(evidence)
    expected_repository = _validate_repo(arguments.repo) if arguments.repo else None
    validated = require_valid(
        ArtifactKind.STATIC_EVIDENCE,
        payload,
        source_path=arguments.out,
        expected_repository=expected_repository,
    )
    if arguments.out:
        with artifact_write_lock(arguments.out):
            atomic_write_validated_json(
                arguments.out,
                ArtifactKind.STATIC_EVIDENCE,
                validated,
                expected_repository=expected_repository,
            )
    else:
        print(strict_json_dumps(validated))


if __name__ == "__main__":
    main()
