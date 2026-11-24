from __future__ import annotations

import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from runner.console import emit
from runner.errors import RunError


@dataclass(frozen=True)
class CacheTarget:
    state_file: Path
    cache_key: str
    label: str
    input_files: Sequence[Path]
    database: Path
    report_dir: Path


def path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def remove_incompatible_database(database_file: Path) -> None:
    """Remove a cache database only when its SlopO schema is outdated."""
    from slopo.schema import SCHEMA_VERSION

    if not path_exists(database_file):
        return
    metadata = database_file.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RunError(f"SlopO database is not a regular file: {database_file}")
    try:
        connection = sqlite3.connect(f"file:{database_file}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT version FROM schema_version").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise RunError(
            f"Could not validate SlopO database schema: {database_file}"
        ) from error
    if row == (SCHEMA_VERSION,):
        return

    emit(f"Removing incompatible SlopO cache database: {database_file}")
    for path in (
        database_file,
        Path(f"{database_file}-wal"),
        Path(f"{database_file}-shm"),
    ):
        path.unlink(missing_ok=True)


def cache_status(action: str, target: CacheTarget) -> int:
    from baseline_cache import main as cache_main

    arguments = [
        action,
        "--state-file",
        str(target.state_file),
        "--cache-key",
        target.cache_key,
        "--label",
        target.label,
    ]
    for input_file in target.input_files:
        arguments.extend(("--input-file", str(input_file)))
    arguments.extend(
        ("--db-file", str(target.database), "--report-dir", str(target.report_dir))
    )
    return cache_main(arguments)
