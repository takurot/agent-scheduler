"""No-follow host publication for schemas in worker-writable directories."""

from __future__ import annotations

import os
import secrets
import stat
from contextlib import ExitStack, suppress
from pathlib import Path

_DIR_FD_SUPPORTED = (
    all(
        operation in os.supports_dir_fd
        for operation in (os.open, os.mkdir, os.stat, os.rename, os.unlink)
    )
    and os.stat in os.supports_follow_symlinks
)


def publish_schema(path: Path, data: bytes) -> None:
    """Atomically publish canonical bytes without reading or following the old leaf.

    Pin every ancestor from the filesystem root, including newly created directories.
    All mutations use the pinned parent, so a pathname swap cannot redirect a write
    through a symlink. Recheck ancestor identities and the leaf before publication;
    rename replaces (never follows) a leaf swapped after that final check. Existing
    regular files are replaced rather than truncated, also protecting hardlink targets.
    """
    if not _DIR_FD_SUPPORTED or any(
        not getattr(os, flag, 0) for flag in ("O_DIRECTORY", "O_NOFOLLOW")
    ):
        raise OSError("safe schema publication is unsupported on this platform")
    if ".." in path.parts or "\0" in str(path) or not path.name or path == Path("."):
        raise OSError("unsafe schema path")
    absolute = path.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        with ExitStack() as handles:
            directory = os.open(absolute.anchor, flags)
            handles.callback(os.close, directory)
            ancestors: list[tuple[int, str, int]] = []
            for component in absolute.parts[1:-1]:
                try:
                    child = os.open(component, flags, dir_fd=directory)
                except FileNotFoundError:
                    # A concurrent creator must still pass the no-follow open.
                    with suppress(FileExistsError):
                        os.mkdir(component, mode=0o700, dir_fd=directory)
                    child = os.open(component, flags, dir_fd=directory)
                handles.callback(os.close, child)
                ancestors.append((directory, component, child))
                directory = child

            def validate_destination() -> None:
                for parent, name, descriptor in ancestors:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    pinned = os.fstat(descriptor)
                    if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                        raise OSError("schema directory changed during publication")
                try:
                    target = os.stat(absolute.name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if not stat.S_ISREG(target.st_mode):
                    raise OSError("schema target must be a regular file")

            validate_destination()
            temporary = f".schema-{secrets.token_hex(16)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                validate_destination()
                os.replace(temporary, absolute.name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
    except NotImplementedError as error:
        raise OSError("safe schema publication is unsupported on this platform") from error
