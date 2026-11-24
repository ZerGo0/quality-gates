import os
import sys
import tempfile
import unittest
from pathlib import Path

QUALITY_GATES = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUALITY_GATES / "scripts" / "gates"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import repository_cache
from git_test_utils import run_git

NOW = 2_000_000_000.0
UNUSED_FOR_TOO_LONG = NOW - repository_cache.MAX_UNUSED_SECONDS - 1


class RepositoryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.caches = self.root / "repositories"
        self.repository = self.root / "repository"
        self.repository.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def used_cache(self, name: str, repository: Path, last_used: float) -> Path:
        cache = self.caches / name
        repository_cache.record_use(cache, repository)
        os.utime(cache / repository_cache.LAST_USED_FILE, (last_used, last_used))
        return cache

    def test_prune_removes_unused_and_orphaned_caches_only(self) -> None:
        current = self.used_cache("current", self.repository, UNUSED_FOR_TOO_LONG)
        recent = self.used_cache("recent", self.repository, NOW)
        unused = self.used_cache("unused", self.repository, UNUSED_FOR_TOO_LONG)
        orphaned = self.used_cache("orphaned", self.root / "deleted", NOW)

        removed = repository_cache.prune(self.caches, current, NOW)

        self.assertCountEqual(removed, [unused, orphaned])
        self.assertTrue(current.is_dir())
        self.assertTrue(recent.is_dir())

    def test_remove_cache_unregisters_its_worktrees(self) -> None:
        run_git(self.repository, "init", "-q")
        run_git(self.repository, "config", "user.email", "test@example.com")
        run_git(self.repository, "config", "user.name", "Test")
        run_git(self.repository, "commit", "-q", "--allow-empty", "-m", "initial")
        cache = self.used_cache("cache", self.repository, NOW)
        worktree = cache / "slopo" / "base-worktree"
        run_git(self.repository, "worktree", "add", "-q", "--detach", str(worktree))

        repository_cache.remove_cache(cache)

        self.assertFalse(cache.exists())
        self.assertNotIn(
            str(worktree.resolve()), run_git(self.repository, "worktree", "list")
        )


if __name__ == "__main__":
    unittest.main()
