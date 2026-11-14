#!/usr/bin/env python3
"""Run one SlopO command at a time using an advisory file lock."""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence


class LockPathError(ValueError):
    """Raised when a lock path cannot be used safely."""


def _validate_lock_parent(lock_file: Path) -> None:
    parent = lock_file.parent
    try:
        metadata = parent.lstat()
    except FileNotFoundError as error:
        raise LockPathError(f"Lock directory is missing: {parent}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LockPathError(f"Lock directory is not a regular directory: {parent}")


def _open_lock_file(lock_file: Path) -> int:
    _validate_lock_parent(lock_file)
    if not lock_file.name:
        raise LockPathError(f"Lock file path is invalid: {lock_file}")

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDWR | no_follow
    try:
        parent_descriptor = os.open(
            lock_file.parent, os.O_RDONLY | os.O_DIRECTORY | no_follow
        )
    except OSError as error:
        raise LockPathError(f"Could not open lock directory: {lock_file.parent}") from error

    try:
        if not stat.S_ISDIR(os.fstat(parent_descriptor).st_mode):
            raise LockPathError(f"Lock directory is not a regular directory: {lock_file.parent}")
        try:
            descriptor = os.open(
                lock_file.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_descriptor,
            )
        except FileExistsError:
            try:
                descriptor = os.open(lock_file.name, flags, dir_fd=parent_descriptor)
            except OSError as error:
                raise LockPathError(
                    f"Lock file is not a regular file: {lock_file}"
                ) from error
        except OSError as error:
            raise LockPathError(f"Could not create lock file: {lock_file}") from error

        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise LockPathError(f"Lock file is not a regular file: {lock_file}")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(parent_descriptor)


@contextmanager
def exclusive_lock(lock_file: Path) -> Iterator[None]:
    """Hold the exclusive advisory lock at *lock_file*."""
    descriptor = _open_lock_file(lock_file)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            print(
                "SlopO is already running; waiting for it to finish. "
                f"Lock detail: {error}",
                file=sys.stderr,
            )
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def run_with_lock(lock_file: Path, command: Sequence[str]) -> int:
    """Run *command* while holding the exclusive advisory lock at *lock_file*."""
    if not command:
        raise ValueError("A command is required after --.")

    with exclusive_lock(lock_file):
        environment = os.environ.copy()
        environment["SLOPO_RUN_LOCK_HELD"] = "1"
        return subprocess.run(command, check=False, env=environment).returncode


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a command under the SlopO lock.")
    parser.add_argument("--lock-file", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.lock_file:
        print("run_lock: Lock file path cannot be empty.", file=sys.stderr)
        return 2
    if not args.command or args.command[0] != "--":
        print("run_lock: A command is required after --.", file=sys.stderr)
        return 2

    try:
        return run_with_lock(Path(args.lock_file), args.command[1:])
    except (LockPathError, OSError, ValueError) as error:
        print(f"run_lock: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
