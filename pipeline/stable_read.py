"""Cross-platform, fail-closed reads for immutable regular-file evidence.

Metadata can reject an unsafe path or an object replacement, but it cannot
prove that file content stayed unchanged.  A successful stable read therefore
requires two complete, independently opened snapshots of the same path to
have identical bytes and SHA-256 digests.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK_SIZE = 1024 * 1024
_FileIdentity = tuple[int, int, int, int, int, int]


class StableReadError(RuntimeError):
    """A regular file could not be proven to have one stable byte value."""

    def __init__(
        self,
        reason: str,
        path: Path,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.reason = reason
        self.path = path
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class StableReadResult:
    content: bytes
    sha256: str
    identity: _FileIdentity


@dataclass(frozen=True)
class _RegularSnapshot:
    content: bytes
    sha256: str
    identity: _FileIdentity


def _file_identity(metadata: os.stat_result) -> _FileIdentity:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _is_filesystem_link(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _changed(path: Path, message: str) -> StableReadError:
    return StableReadError("concurrent_change", path, message, retryable=True)


def _read_regular_snapshot(path: Path) -> _RegularSnapshot:
    """Read one FD-bound snapshot; kept separate for deterministic tests."""

    try:
        path_before = os.lstat(path)
    except OSError as error:
        raise StableReadError(
            "unavailable",
            path,
            f"regular file is unavailable: {path}: {error}",
        ) from None
    if _is_filesystem_link(path_before) or not stat.S_ISREG(path_before.st_mode):
        raise StableReadError(
            "unsafe_type",
            path,
            f"path is not a no-follow regular file: {path}",
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened_before = os.fstat(descriptor)
        if (
            _is_filesystem_link(opened_before)
            or not stat.S_ISREG(opened_before.st_mode)
        ):
            raise StableReadError(
                "unsafe_type",
                path,
                f"opened object is not a regular file: {path}",
            )
        if _file_identity(opened_before) != _file_identity(path_before):
            raise _changed(path, f"path changed while it was opened: {path}")

        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)

        opened_after = os.fstat(descriptor)
        if _file_identity(opened_after) != _file_identity(opened_before):
            raise _changed(path, f"open file changed while it was read: {path}")
    except StableReadError:
        raise
    except OSError as error:
        # ELOOP is the common O_NOFOLLOW result.  Treat every open/read race as
        # fail-closed; only an explicitly classified content change is retried.
        reason = (
            "unsafe_type"
            if getattr(error, "errno", None) == errno.ELOOP
            else "unavailable"
        )
        raise StableReadError(
            reason,
            path,
            f"regular file could not be read safely: {path}: {error}",
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)

    try:
        path_after = os.lstat(path)
    except OSError as error:
        raise _changed(
            path,
            f"path disappeared after it was read: {path}: {error}",
        ) from None
    if _is_filesystem_link(path_after) or not stat.S_ISREG(path_after.st_mode):
        raise StableReadError(
            "unsafe_type",
            path,
            f"path became a filesystem link or unsafe type: {path}",
        )
    if _file_identity(path_after) != _file_identity(opened_after):
        raise _changed(path, f"path changed after it was read: {path}")

    return _RegularSnapshot(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        identity=_file_identity(path_after),
    )


def stable_read(
    path: Path,
    *,
    expected_sha256: str | None = None,
    max_attempts: int = 1,
) -> StableReadResult:
    """Return one proven-stable byte value or fail closed.

    Retries are bounded and apply only to an explicitly observed concurrent
    change.  Immutable evidence should use the default single attempt.  A
    legitimately mutable, atomically replaced pointer may opt into a small
    retry count and will still return only a complete old or new version.
    """

    path = Path(path)
    if max_attempts < 1 or isinstance(max_attempts, bool):
        raise ValueError("max_attempts must be a positive integer")
    if expected_sha256 is not None and not SHA256_PATTERN.fullmatch(expected_sha256):
        raise StableReadError(
            "invalid_expected_digest",
            path,
            "expected SHA-256 must be a lowercase hexadecimal digest",
        )

    last_change: StableReadError | None = None
    for attempt in range(max_attempts):
        try:
            first = _read_regular_snapshot(path)
            second = _read_regular_snapshot(path)
            if (
                first.content != second.content
                or first.sha256 != second.sha256
                or _object_identity_from_tuple(first.identity)
                != _object_identity_from_tuple(second.identity)
                or first.identity != second.identity
            ):
                raise _changed(path, f"file changed between complete snapshots: {path}")
            if expected_sha256 is not None and second.sha256 != expected_sha256:
                raise StableReadError(
                    "digest_mismatch",
                    path,
                    f"stable file digest does not match the expected SHA-256: {path}",
                )
            return StableReadResult(second.content, second.sha256, second.identity)
        except StableReadError as error:
            if not error.retryable or attempt + 1 >= max_attempts:
                raise
            last_change = error
    assert last_change is not None
    raise last_change


def _object_identity_from_tuple(identity: _FileIdentity) -> tuple[int, int, int]:
    return (identity[0], identity[1], stat.S_IFMT(identity[2]))


def stable_read_bytes(
    path: Path,
    *,
    expected_sha256: str | None = None,
    max_attempts: int = 1,
) -> bytes:
    return stable_read(
        path,
        expected_sha256=expected_sha256,
        max_attempts=max_attempts,
    ).content
