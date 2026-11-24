from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

from runner.cache import path_exists
from runner.console import emit
from runner.errors import RunError
from runner.paths import Paths


def _run_git(repository: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *arguments],
            cwd=repository,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise RunError(
            f"git {' '.join(arguments)} failed: {error.stderr.strip()}"
        ) from error


def prepare_base_worktree(paths: Paths, snapshot_commit: str) -> None:
    """Prepare a clean baseline, discarding edits in this disposable cache."""
    if path_exists(paths.base_worktree):
        metadata = paths.base_worktree.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RunError(
                "The SlopO baseline worktree path is not a regular directory: "
                f"{paths.base_worktree}"
            )
        actual_root = Path(
            _run_git(paths.base_worktree, "rev-parse", "--show-toplevel")
        ).resolve()
        if actual_root != paths.base_worktree.resolve():
            raise RunError(
                f"The SlopO baseline cache is not a Git worktree: {paths.base_worktree}"
            )
        if _run_git(
            paths.base_worktree, "status", "--porcelain", "--untracked-files=all"
        ):
            _run_git(
                paths.repository_root,
                "worktree",
                "remove",
                "--force",
                str(paths.base_worktree),
            )
            emit(
                f"Discarded dirty SlopO baseline at {paths.base_worktree}. "
                "Creating a clean baseline.",
                sys.stderr,
            )
    if not path_exists(paths.base_worktree):
        _run_git(
            paths.repository_root,
            "worktree",
            "add",
            "--force",
            "--quiet",
            "--detach",
            str(paths.base_worktree),
            snapshot_commit,
        )

    cached_commit = _run_git(
        paths.base_worktree, "rev-parse", "--verify", "HEAD^{commit}"
    )
    if cached_commit != snapshot_commit:
        _run_git(
            paths.base_worktree,
            "checkout",
            "--quiet",
            "--detach",
            snapshot_commit,
        )
