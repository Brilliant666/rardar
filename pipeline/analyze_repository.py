"""Read-only repository analyzer used by the Rardar candidate pipeline.

The analyzer intentionally does not install dependencies or execute repository
code. It inspects a shallow checkout (or an existing local path) and emits
structured evidence that can be reviewed by an AI analyzer later.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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
MAX_ARCHIVE_FILES = 25_000
MAX_EXTRACTED_BYTES = 600_000_000
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
    warnings: list[str] = field(default_factory=list)


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
    value = repo.removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("repository must be a public GitHub owner/name")
    return value


def _git_environment() -> dict[str, str]:
    """Isolate read-only clones from user-level URL rewrites and proxy rules."""
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def _extract_source_archive(archive_path: Path, checkout: Path) -> None:
    checkout = checkout.resolve()
    checkout.mkdir(parents=True, exist_ok=False)
    extracted_files = 0
    extracted_bytes = 0
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            path = Path(item.filename.replace("\\", "/"))
            parts = path.parts[1:]
            if not parts or item.is_dir():
                continue
            if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
                raise RuntimeError(f"unsafe source archive path: {item.filename}")
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue
            relative = Path(*parts)
            if any(part.lower() in SKIP_DIRECTORIES for part in relative.parts[:-1]):
                continue
            if relative.suffix.lower() in SKIP_FILE_SUFFIXES:
                continue
            extracted_files += 1
            extracted_bytes += item.file_size
            if extracted_files > MAX_ARCHIVE_FILES:
                raise RuntimeError(f"source archive exceeds {MAX_ARCHIVE_FILES} files")
            if extracted_bytes > MAX_EXTRACTED_BYTES:
                raise RuntimeError(f"source archive exceeds {MAX_EXTRACTED_BYTES} extracted bytes")
            target = (checkout / relative).resolve()
            if checkout not in target.parents:
                raise RuntimeError(f"source archive escapes checkout: {item.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.file_size > MAX_TEXT_BYTES:
                target.touch()
                continue
            with archive.open(item) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=64 * 1024)


def _download_source_archive(repository: str, directory: Path) -> Path:
    archive_path = directory / "source.zip"
    request = urllib.request.Request(
        f"https://codeload.github.com/{repository}/zip/HEAD",
        headers={"user-agent": "rardar-static-analyzer/0.1"},
    )
    total = 0
    with urllib.request.urlopen(request, timeout=180) as response, archive_path.open("wb") as output:
        while chunk := response.read(64 * 1024):
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise RuntimeError(f"source archive exceeds {MAX_ARCHIVE_BYTES} download bytes")
            output.write(chunk)
    checkout = directory / "archive-repo"
    _extract_source_archive(archive_path, checkout)
    return checkout


def analyze_remote(repo: str) -> StaticEvidence:
    normalized = _validate_repo(repo)
    with tempfile.TemporaryDirectory(prefix="rardar-") as directory:
        checkout = Path(directory, "repo")
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
        clone_error: str | None = None
        try:
            subprocess.run(
                command,
                check=True,
                timeout=180,
                env=_git_environment(),
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired as error:
            clone_error = f"shallow clone timed out after {error.timeout} seconds"
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "git clone failed").strip().splitlines()[-1]
            clone_error = f"shallow clone failed: {detail}"
        source_root = checkout
        if clone_error:
            try:
                source_root = _download_source_archive(normalized, Path(directory))
            except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                raise RuntimeError(
                    f"{clone_error}; source archive fallback failed for {normalized}: {error}"
                ) from None
        evidence = analyze_path(source_root, normalized)
        evidence.source = f"https://github.com/{normalized}"
        if clone_error:
            evidence.warnings.append(
                f"{clone_error}; inspected a bounded official GitHub source archive instead"
            )
        return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate read-only static evidence for a repository")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="public GitHub owner/name")
    source.add_argument("--path", type=Path, help="existing local repository path")
    parser.add_argument("--out", type=Path, help="optional output JSON path")
    arguments = parser.parse_args()

    evidence = analyze_remote(arguments.repo) if arguments.repo else analyze_path(arguments.path)
    payload = asdict(evidence)
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
