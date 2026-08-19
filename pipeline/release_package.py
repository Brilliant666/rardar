"""Build and safely accept exact Rardar release archives in isolated CI roots."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import inspect
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from pipeline.release_artifact import (
    COMMIT_PATTERN,
    MANIFEST_NAME,
    ReleaseArtifactError,
    SCHEMA_VERSION,
    verify_release_root,
)
from pipeline.release_manifest import create_release_manifest


ARCHIVE_SUFFIX = "-linux-x86_64.tar.gz"


def _fail(code: str, detail: str) -> None:
    raise ReleaseArtifactError(code, detail)


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fresh_directory(path: Path, *, protected: Iterable[Path] = ()) -> Path:
    candidate = Path(path).expanduser().absolute()
    if candidate.exists() or candidate.is_symlink():
        _fail("FAIL_RELEASE_ARTIFACT_BUILD", f"destination must not exist: {candidate}")
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as error:
        _fail("FAIL_RELEASE_ARTIFACT_BUILD", f"destination parent is unavailable: {error}")
    target = parent / candidate.name
    for root in protected:
        canonical = Path(root).resolve(strict=True)
        if _paths_overlap(target, canonical):
            _fail("FAIL_RELEASE_ARTIFACT_BUILD", f"destination overlaps protected source: {target}")
    target.mkdir(mode=0o700)
    return target


def _git_output(source_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(source_root), *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        _fail("FAIL_RELEASE_ARTIFACT_BUILD", f"git {' '.join(arguments)} failed")
    return completed.stdout.strip()


def _validate_member_name(name: str) -> str:
    if not name or name.startswith(("/", "\\")) or "\\" in name:
        _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"unsafe archive member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"unsafe archive member name: {name!r}")
    normalized = path.as_posix().rstrip("/")
    if not normalized:
        _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", "archive member name is empty")
    return normalized


def _validate_tar_members(members: list[tarfile.TarInfo]) -> None:
    names: set[str] = set()
    symlinks: dict[str, str] = {}
    for member in members:
        name = _validate_member_name(member.name)
        if name in names:
            _fail("FAIL_RELEASE_ARTIFACT_ARCHIVE", f"duplicate archive member: {name}")
        names.add(name)
        if member.issym():
            link = member.linkname
            if (
                not link
                or link.startswith(("/", "\\"))
                or "\\" in link
                or (len(link) >= 3 and link[1:3] in {":/", ":\\"})
            ):
                _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"unsafe archive link: {name}")
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), link))
            if target == ".." or target.startswith("../") or target.startswith("/"):
                _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"archive link escapes release: {name}")
            symlinks[name] = target
        elif not (member.isfile() or member.isdir()):
            _fail("FAIL_RELEASE_ARTIFACT_ARCHIVE", f"unsupported archive member type: {name}")
    for name in names:
        parent = PurePosixPath(name).parent
        while parent != PurePosixPath("."):
            if parent.as_posix() in symlinks:
                _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"archive member traverses link: {name}")
            parent = parent.parent
    for name, target in symlinks.items():
        if target not in names:
            _fail("FAIL_RELEASE_ARTIFACT_UNSAFE_LINK", f"archive link target is missing: {name}")


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, mode="r:*") as source:
        members = source.getmembers()
        _validate_tar_members(members)
        try:
            if "filter" in inspect.signature(source.extractall).parameters:
                source.extractall(destination, members=members, filter="data")
            else:
                # All names, types, parents, and link targets were validated
                # above before this compatibility path is reached.
                source.extractall(destination, members=members)
        except (OSError, tarfile.TarError) as error:
            _fail("FAIL_RELEASE_ARTIFACT_ARCHIVE", f"cannot extract release archive: {error}")


def _remove_stage_caches(stage: Path) -> None:
    for relative in (
        ".git",
        ".venv",
        ".venv-ci",
        ".vinext",
        ".wrangler",
        "data",
        "node_modules/.cache",
        "node_modules/.vite",
        "node_modules/.vite-temp",
    ):
        target = stage / relative
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)


def stage_release(
    source_root: Path,
    stage_root: Path,
    *,
    commit_sha: str,
    verify_workflow_run_id: str,
    verify_workflow_head_sha: str,
    npm_version: str,
    built_at: str,
) -> dict[str, Any]:
    if not COMMIT_PATTERN.fullmatch(commit_sha):
        _fail("FAIL_RELEASE_ARTIFACT_IDENTITY", "commit SHA must be exactly 40 lowercase hex characters")
    source = Path(source_root).resolve(strict=True)
    if _git_output(source, "rev-parse", "HEAD") != commit_sha:
        _fail("FAIL_RELEASE_ARTIFACT_IDENTITY", "checked-out builder HEAD differs from exact release SHA")
    stage = _fresh_directory(stage_root, protected=(source,))
    try:
        with tempfile.NamedTemporaryFile(prefix="rardar-source-", suffix=".tar", delete=False) as handle:
            archive_path = Path(handle.name)
        try:
            completed = subprocess.run(
                ["git", "-C", str(source), "archive", "--format=tar", f"--output={archive_path}", commit_sha],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                _fail("FAIL_RELEASE_ARTIFACT_BUILD", "git archive failed for exact release SHA")
            _safe_extract_tar(archive_path, stage)
        finally:
            archive_path.unlink(missing_ok=True)

        _remove_stage_caches(stage)
        for relative in ("node_modules", "dist", "wheelhouse"):
            source_directory = source / relative
            if not source_directory.is_dir() or source_directory.is_symlink():
                _fail("FAIL_RELEASE_ARTIFACT_INCOMPLETE", f"builder output is missing: {relative}")
            destination = stage / relative
            if destination.exists() or destination.is_symlink():
                _fail("FAIL_RELEASE_ARTIFACT_BUILD", f"git archive unexpectedly contained {relative}")
            shutil.copytree(source_directory, destination, symlinks=True)
        _remove_stage_caches(stage)
        create_release_manifest(
            stage,
            commit_sha=commit_sha,
            verify_workflow_run_id=verify_workflow_run_id,
            verify_workflow_head_sha=verify_workflow_head_sha,
            npm_version=npm_version,
            built_at=built_at,
        )
        return verify_release_root(stage, expected_sha=commit_sha, check_host=True)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _stage_paths(stage: Path) -> list[Path]:
    return sorted(stage.rglob("*"), key=lambda path: path.relative_to(stage).as_posix())


def create_archive(
    stage_root: Path,
    archive: Path,
    *,
    expected_sha: str,
    source_date_epoch: int,
) -> dict[str, Any]:
    stage = Path(stage_root).resolve(strict=True)
    verify_release_root(stage, expected_sha=expected_sha, check_host=True)
    if source_date_epoch < 0:
        _fail("FAIL_RELEASE_ARTIFACT_ARCHIVE", "SOURCE_DATE_EPOCH must be non-negative")
    output = Path(archive).expanduser().absolute()
    if output.exists() or output.is_symlink():
        _fail("FAIL_RELEASE_ARTIFACT_ARCHIVE", f"archive destination already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT, dereference=False) as target:
                    for path in _stage_paths(stage):
                        relative = path.relative_to(stage).as_posix()
                        info = target.gettarinfo(str(path), arcname=relative)
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = source_date_epoch
                        info.pax_headers = {}
                        if info.isfile():
                            with path.open("rb") as source:
                                target.addfile(info, source)
                        else:
                            target.addfile(info)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    digest = _sha256_path(output)
    checksum = output.with_name(output.name + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii", newline="\n")
    return {
        "archive": str(output),
        "checksum": str(checksum),
        "sha256": digest,
        "size": output.stat().st_size,
    }


def _verify_checksum(archive: Path, checksum: Path) -> str:
    try:
        text = checksum.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as error:
        _fail("FAIL_RELEASE_ARTIFACT_ARCHIVE", f"cannot read checksum: {error}")
    match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)\n?", text)
    if not match or match.group(2) != archive.name:
        _fail("FAIL_RELEASE_ARTIFACT_ARCHIVE", "checksum file is malformed or names another archive")
    actual = _sha256_path(archive)
    if actual != match.group(1):
        _fail("FAIL_RELEASE_ARTIFACT_ARCHIVE", "archive SHA-256 does not match checksum file")
    return actual


def accept_archive(
    archive: Path,
    checksum: Path,
    extract_root: Path,
    *,
    expected_sha: str,
) -> dict[str, Any]:
    archive_path = Path(archive).resolve(strict=True)
    checksum_path = Path(checksum).resolve(strict=True)
    digest = _verify_checksum(archive_path, checksum_path)
    extraction = _fresh_directory(extract_root, protected=(archive_path, checksum_path))
    try:
        _safe_extract_tar(archive_path, extraction)
        report = verify_release_root(extraction, expected_sha=expected_sha, check_host=True)
    except Exception:
        shutil.rmtree(extraction, ignore_errors=True)
        raise
    return {**report, "archiveSha256": digest, "extractedRoot": str(extraction)}


def build_release(
    source_root: Path,
    stage_root: Path,
    output_directory: Path,
    *,
    commit_sha: str,
    verify_workflow_run_id: str,
    verify_workflow_head_sha: str,
    npm_version: str,
    built_at: str,
) -> dict[str, Any]:
    stage_report = stage_release(
        source_root,
        stage_root,
        commit_sha=commit_sha,
        verify_workflow_run_id=verify_workflow_run_id,
        verify_workflow_head_sha=verify_workflow_head_sha,
        npm_version=npm_version,
        built_at=built_at,
    )
    source = Path(source_root).resolve(strict=True)
    epoch_text = _git_output(source, "show", "-s", "--format=%ct", commit_sha)
    try:
        source_date_epoch = int(epoch_text)
    except ValueError:
        _fail("FAIL_RELEASE_ARTIFACT_BUILD", "git commit timestamp is invalid")
    output = _fresh_directory(output_directory, protected=(source, Path(stage_root).resolve(strict=True)))
    archive = output / f"rardar-release-{commit_sha}{ARCHIVE_SUFFIX}"
    archive_report = create_archive(
        stage_root,
        archive,
        expected_sha=commit_sha,
        source_date_epoch=source_date_epoch,
    )
    manifest_copy = output / MANIFEST_NAME
    shutil.copyfile(Path(stage_root) / MANIFEST_NAME, manifest_copy)
    return {"stage": stage_report, **archive_report, "manifest": str(manifest_copy)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or accept an exact Rardar release archive")
    subcommands = parser.add_subparsers(dest="command", required=True)

    build = subcommands.add_parser("build", help="stage and package one exact checked-out commit")
    build.add_argument("--source-root", required=True, type=Path)
    build.add_argument("--stage-root", required=True, type=Path)
    build.add_argument("--output-directory", required=True, type=Path)
    build.add_argument("--commit-sha", required=True)
    build.add_argument("--verify-workflow-run-id", required=True)
    build.add_argument("--verify-workflow-head-sha", required=True)
    build.add_argument("--npm-version", required=True)
    build.add_argument("--built-at", required=True)

    accept = subcommands.add_parser("accept", help="checksum, safely extract, and verify an archive")
    accept.add_argument("--archive", required=True, type=Path)
    accept.add_argument("--checksum", required=True, type=Path)
    accept.add_argument("--extract-root", required=True, type=Path)
    accept.add_argument("--expected-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "build":
            report = build_release(
                arguments.source_root,
                arguments.stage_root,
                arguments.output_directory,
                commit_sha=arguments.commit_sha,
                verify_workflow_run_id=arguments.verify_workflow_run_id,
                verify_workflow_head_sha=arguments.verify_workflow_head_sha,
                npm_version=arguments.npm_version,
                built_at=arguments.built_at,
            )
        else:
            report = accept_archive(
                arguments.archive,
                arguments.checksum,
                arguments.extract_root,
                expected_sha=arguments.expected_sha,
            )
    except (OSError, ReleaseArtifactError, subprocess.SubprocessError, tarfile.TarError) as error:
        detail = error.as_dict() if isinstance(error, ReleaseArtifactError) else {
            "code": "FAIL_RELEASE_ARTIFACT_BUILD",
            "detail": str(error),
        }
        print(json.dumps({"schemaVersion": SCHEMA_VERSION, "status": "failed", "error": detail}, indent=2))
        return 1
    print(json.dumps({"schemaVersion": SCHEMA_VERSION, "status": "healthy", **report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
