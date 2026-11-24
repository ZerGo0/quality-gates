"""Create, mark, and prune the per-run quality-gate report directories."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Iterator

MAX_RETAINED_RUNS = 20
ACTIVE_MARKER = ".active"
RUN_DIRECTORY_NAME = re.compile(
    r"^(?P<timestamp>\d{8}T\d{6}Z)-(?P<pid>\d+)-[A-Za-z0-9_-]+$"
)


def validate_artifact_root(artifact_root: Path) -> Path:
    if artifact_root.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked report directory: {artifact_root}")
    artifact_root.mkdir(parents=True, exist_ok=True)
    return artifact_root


@contextmanager
def retention_lock(artifact_root: Path) -> Iterator[None]:
    lock_path = artifact_root / ".retention.lock"
    with lock_path.open("a+b") as lock_file:
        flock(lock_file.fileno(), LOCK_EX)
        try:
            yield
        finally:
            flock(lock_file.fileno(), LOCK_UN)


def create_run_directory(reports: Path) -> Path:
    artifact_root = validate_artifact_root(reports)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with retention_lock(artifact_root):
        run_directory = Path(
            tempfile.mkdtemp(prefix=f"{timestamp}-{os.getpid()}-", dir=artifact_root)
        )
        mark_run_active(run_directory)
        prune_run_directories(artifact_root)
    return run_directory


def process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_is_active(run_directory: Path) -> bool:
    marker = run_directory / ACTIVE_MARKER
    if marker.is_symlink():
        return True
    if not marker.exists():
        return False
    if not marker.is_file():
        return True
    try:
        process_id = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return process_is_alive(process_id)


def mark_run_active(run_directory: Path) -> None:
    (run_directory / ACTIVE_MARKER).write_text(f"{os.getpid()}\n", encoding="utf-8")


def clear_active_marker(run_directory: Path) -> None:
    marker = run_directory / ACTIVE_MARKER
    if marker.is_symlink():
        raise RuntimeError(f"Refusing to remove symlinked active marker: {marker}")
    try:
        marker_process_id = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Could not validate aislop active marker: {marker}") from error
    if marker_process_id != os.getpid():
        raise RuntimeError(f"Refusing to remove another process's marker: {marker}")
    marker.unlink(missing_ok=True)


def is_generated_run_name(name: str) -> bool:
    match = RUN_DIRECTORY_NAME.fullmatch(name)
    if match is None or int(match.group("pid")) <= 0:
        return False
    try:
        datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True


def generated_run_directories(artifact_root: Path) -> list[Path]:
    directories: list[Path] = []
    with os.scandir(artifact_root) as entries:
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if is_generated_run_name(entry.name):
                directories.append(Path(entry.path))
    return directories


def remove_generated_run(run_directory: Path, artifact_root: Path) -> None:
    resolved_root = artifact_root.resolve()
    resolved_run = run_directory.resolve()
    if run_directory.is_symlink() or resolved_run.parent != resolved_root:
        raise RuntimeError(f"Refusing to remove unsafe report directory: {run_directory}")
    if not is_generated_run_name(run_directory.name):
        raise RuntimeError(f"Refusing to remove unknown report directory: {run_directory}")
    shutil.rmtree(run_directory)


def prune_run_directories(
    artifact_root: Path, retain: int = MAX_RETAINED_RUNS
) -> None:
    if retain < 1:
        raise ValueError("Quality-gate report retention must be at least one run")
    generated = generated_run_directories(artifact_root)
    active = [directory for directory in generated if run_is_active(directory)]
    completed = sorted(
        (directory for directory in generated if directory not in active),
        key=lambda directory: directory.name,
        reverse=True,
    )
    for expired in completed[retain:]:
        remove_generated_run(expired, artifact_root)


def finish_run_directory(run_directory: Path) -> None:
    artifact_root = run_directory.parent
    with retention_lock(artifact_root):
        clear_active_marker(run_directory)
        prune_run_directories(artifact_root)
