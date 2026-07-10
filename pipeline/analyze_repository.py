"""Read-only repository analyzer used by the Rardar candidate pipeline.

The analyzer intentionally does not install dependencies or execute repository
code. It inspects a shallow checkout (or an existing local path) and emits
structured evidence that can be reviewed by an AI analyzer later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


MAX_TEXT_BYTES = 512_000
MAX_FILES = 12_000
SKIP_DIRECTORIES = {
    ".git",
    ".next",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
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
    warnings: list[str] = field(default_factory=list)


def _iter_files(root: Path) -> Iterable[Path]:
    seen = 0
    for current, directories, files in os.walk(root):
        directories[:] = sorted(item for item in directories if item not in SKIP_DIRECTORIES)
        for name in sorted(files):
            seen += 1
            if seen > MAX_FILES:
                return
            yield Path(current, name)


def _safe_read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _license_hint(root: Path) -> str | None:
    candidates = [path for path in root.iterdir() if path.is_file() and path.name.lower().startswith(("license", "copying"))]
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
        if "test" in Path(relative).name.lower() or "/tests/" in f"/{relative}":
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
            raise RuntimeError(f"shallow clone timed out after {error.timeout} seconds: {normalized}") from None
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "git clone failed").strip().splitlines()[-1]
            raise RuntimeError(f"shallow clone failed for {normalized}: {detail}") from None
        evidence = analyze_path(checkout, normalized)
        evidence.source = f"https://github.com/{normalized}"
        return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate read-only static evidence for a repository")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", help="public GitHub owner/name")
    source.add_argument("--path", type=Path, help="existing local repository path")
    parser.add_argument("--out", type=Path, help="optional output JSON path")
    arguments = parser.parse_args()

    evidence = analyze_remote(arguments.repo) if arguments.repo else analyze_path(arguments.path)
    payload = json.dumps(asdict(evidence), ensure_ascii=False, indent=2)
    if arguments.out:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)
        arguments.out.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
