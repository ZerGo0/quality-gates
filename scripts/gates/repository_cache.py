"""Record, prune, and clear the per-repository quality-gate caches."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPOSITORY_FILE = "repository"
LAST_USED_FILE = "last-used"
MAX_UNUSED_SECONDS = 30 * 24 * 60 * 60


def record_use(cache: Path, repository: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / REPOSITORY_FILE).write_text(f"{repository}\n", encoding="utf-8")
    (cache / LAST_USED_FILE).touch()


def recorded_repository(cache: Path) -> Path | None:
    marker = cache / REPOSITORY_FILE
    if not marker.is_file():
        return None
    text = marker.read_text(encoding="utf-8").strip()
    return Path(text) if text else None


def is_stale(cache: Path, now: float) -> bool:
    repository = recorded_repository(cache)
    if repository is not None and not repository.is_dir():
        return True
    marker = cache / LAST_USED_FILE
    last_used = (marker if marker.is_file() else cache).stat().st_mtime
    return now - last_used > MAX_UNUSED_SECONDS


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


def cache_worktrees(repository: Path, cache: Path) -> list[Path]:
    listing = _git(repository, "worktree", "list", "--porcelain")
    if listing.returncode != 0:
        return []
    resolved_cache = cache.resolve()
    worktrees = (
        Path(line.removeprefix("worktree "))
        for line in listing.stdout.splitlines()
        if line.startswith("worktree ")
    )
    return [path for path in worktrees if path.resolve().is_relative_to(resolved_cache)]


def remove_cache(cache: Path) -> None:
    repository = recorded_repository(cache)
    repository_exists = repository is not None and repository.is_dir()
    if repository_exists:
        for worktree in cache_worktrees(repository, cache):
            _git(repository, "worktree", "remove", "--force", str(worktree))
    shutil.rmtree(cache)
    if repository_exists:
        _git(repository, "worktree", "prune")


def cache_directories(caches_root: Path) -> list[Path]:
    if not caches_root.is_dir():
        return []
    return [
        path
        for path in caches_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    ]


def prune(caches_root: Path, keep: Path, now: float) -> list[Path]:
    removed: list[Path] = []
    for cache in cache_directories(caches_root):
        if cache == keep or not is_stale(cache, now):
            continue
        try:
            remove_cache(cache)
        except OSError as error:
            print(f"quality-gates: could not remove cache {cache}: {error}", file=sys.stderr)
            continue
        removed.append(cache)
    return removed


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError(f"expected an absolute path: {value}")
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the quality-gate caches.")
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare")
    prepare.add_argument("cache", type=_absolute_path)
    prepare.add_argument("repository", type=_absolute_path)
    clear = actions.add_parser("clear")
    clear.add_argument("cache", type=_absolute_path)
    clear_all = actions.add_parser("clear-all")
    clear_all.add_argument("caches_root", type=_absolute_path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    if arguments.action == "prepare":
        record_use(arguments.cache, arguments.repository)
        for cache in prune(arguments.cache.parent, arguments.cache, time.time()):
            print(f"quality-gates: removed unused cache {cache}", file=sys.stderr)
        return 0
    caches = (
        cache_directories(arguments.caches_root)
        if arguments.action == "clear-all"
        else [arguments.cache] if arguments.cache.is_dir() else []
    )
    for cache in caches:
        remove_cache(cache)
        print(f"Removed {cache}")
    if not caches:
        print("No quality-gates cache to remove.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
