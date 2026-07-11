"""Cross-process serialization for writers of a Rardar data directory."""

from __future__ import annotations

import errno
import hashlib
import os
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, ParamSpec, TypeVar


P = ParamSpec("P")
R = TypeVar("R")


def _default_lock_root() -> Path:
    # Keep this independent from RARDAR_RUNTIME_DIR: the manager and a manual
    # refresh may be launched with different runtime settings, but must still
    # contend on the same lock for the same canonical data directory.
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "state"
    return base / "Rardar" / "runtime" / "data-locks"


def data_dir_lock_path(data_dir: Path, lock_root: Path | None = None) -> Path:
    """Return one stable, user-local lock path for a canonical data directory."""
    canonical = os.path.normcase(str(data_dir.expanduser().resolve()))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return (lock_root or _default_lock_root()) / f"data-{digest}.lock"


def _try_lock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def data_dir_lock(
    data_dir: Path,
    *,
    lock_root: Path | None = None,
    timeout: float | None = None,
    poll_interval: float = 0.05,
) -> Iterator[None]:
    """Exclusively lock a data directory, waiting until the active writer exits."""
    path = data_dir_lock_path(data_dir, lock_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()

        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while True:
            try:
                _try_lock(handle)
                acquired = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for Rardar data lock: {data_dir}") from error
                time.sleep(max(0.01, poll_interval))
        yield
    finally:
        try:
            if acquired:
                _unlock(handle)
        finally:
            handle.close()


def locked_data_dir(function: Callable[..., R]) -> Callable[..., R]:
    """Wrap a function whose first argument is the data directory it mutates."""

    @wraps(function)
    def wrapper(data_dir: Path, *args: P.args, **kwargs: P.kwargs) -> R:
        canonical = data_dir.expanduser().resolve()
        with data_dir_lock(canonical):
            return function(canonical, *args, **kwargs)

    return wrapper
