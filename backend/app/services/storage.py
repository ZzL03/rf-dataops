import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from app.core.errors import DomainError


def normalize_path(value: str, *, directory: bool = False) -> str:
    if directory and value in {"", "."}:
        return "."
    if (
        not value
        or len(value) > 512
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or value.startswith("/")
        or any(p in {"", ".", ".."} for p in value.split("/"))
    ):
        raise DomainError("UNSAFE_PATH", "Use a canonical relative path inside the data root", 403)
    return str(PurePosixPath(value))


def signature(info: os.stat_result) -> dict[str, str]:
    return {
        key: str(getattr(info, key))
        for key in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
    }


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


class ControlledRoot:
    """Reject path traversal and links, including HDF5 sidecar/marker paths."""

    def __init__(self, root: Path) -> None:
        try:
            self.root = root.resolve(strict=True)
            if not self.root.is_dir():
                raise OSError("Not a directory")
        except OSError as exc:
            raise DomainError(
                "STORAGE_UNAVAILABLE", "Configured data root is unavailable", 503
            ) from exc

    def checked_path(self, relative: str, *, directory: bool = False) -> Path:
        relative = normalize_path(relative, directory=directory)
        current = self.root
        for part in () if relative == "." else relative.split("/"):
            current = current / part
            if is_link(current.lstat()):
                raise DomainError(
                    "UNSAFE_PATH", "Symbolic links and reparse points are forbidden", 403
                )
        if not current.resolve(strict=True).is_relative_to(self.root):
            raise DomainError("UNSAFE_PATH", "Path escapes the configured data root", 403)
        return current

    @contextmanager
    def open_read(self, relative: str) -> Iterator[BinaryIO]:
        relative = normalize_path(relative)
        descriptor = None
        parent_fd = None
        try:
            # Linux: pin each directory FD and refuse symlinks at every open, not just at resolve().
            if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
                parent_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                parts = relative.split("/")
                for part in parts[:-1]:
                    next_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
                    )
                    os.close(parent_fd)
                    parent_fd = next_fd
                descriptor = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd
                )
            else:
                path = self.checked_path(relative)
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                after = self.checked_path(relative).stat()
                if signature(os.fstat(descriptor)) != signature(after):
                    raise DomainError("SOURCE_CHANGED", "Source changed while opening", 409)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise DomainError("UNSAFE_PATH", "Only regular files are supported", 403)
            stream = os.fdopen(descriptor, "rb")
            descriptor = None
            with stream:
                yield stream
        except FileNotFoundError as exc:
            raise DomainError(
                "SOURCE_MISSING", "Source or required sidecar is missing", 409
            ) from exc
        except OSError as exc:
            raise DomainError("SOURCE_UNREADABLE", "Source is inaccessible or unsafe", 409) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent_fd is not None:
                os.close(parent_fd)

    def read_small(self, relative: str, limit: int = 65536) -> bytes:
        with self.open_read(relative) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise DomainError("SIDECAR_TOO_LARGE", "Metadata sidecar exceeds its size limit")
        return data

    def candidates(self, directory: str, maximum: int) -> list[str]:
        try:
            start = self.checked_path(directory, directory=True)
            if not start.is_dir():
                raise DomainError("INVALID_DIRECTORY", "Scan target must be a directory")
            found = []

            def failed(_error: OSError) -> None:
                raise DomainError("SCAN_UNREADABLE", "A scan directory is inaccessible", 409)

            for base, dirs, files in os.walk(start, followlinks=False, onerror=failed):
                # Unsafe subtrees must not be silently reconciled as missing.
                for name in dirs:
                    if is_link((Path(base) / name).lstat()):
                        raise DomainError("UNSAFE_PATH", "Scan contains a linked directory", 403)
                dirs.sort()
                for name in sorted(files):
                    if Path(name).suffix.lower() in {".bin", ".h5", ".hdf5"}:
                        relative = (Path(base) / name).relative_to(self.root).as_posix()
                        found.append(normalize_path(relative))
                        if len(found) > maximum:
                            raise DomainError(
                                "SCAN_LIMIT", "Narrow the directory: too many candidate files"
                            )
            return found
        except OSError as exc:
            raise DomainError("SCAN_UNREADABLE", "Scan directory is inaccessible", 409) from exc
