"""Small cross-platform process locks shared by all service entry points."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def host_lock_path(service: str, identity: str = "") -> Path:
    suffix = ""
    if identity:
        suffix = "-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"channelguard-{service}{suffix}.lock"


class ProcessLock:
    """Non-blocking lock retained until close/process exit."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write("\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
        except (OSError, BlockingIOError):
            handle.close()
            return False

        self.handle = handle
        try:
            if os.name == "nt":
                # Keep byte zero present and locked for msvcrt; metadata begins
                # after it so rewriting diagnostics cannot release the region.
                handle.seek(1)
                handle.truncate()
            else:
                handle.seek(0)
                handle.truncate()
            handle.write(f"pid={os.getpid()} cwd={Path.cwd()}\n")
            handle.flush()
        except OSError:
            pass
        return True

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"process lock already held: {self.path}")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
