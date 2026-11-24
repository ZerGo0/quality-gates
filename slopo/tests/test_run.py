import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

SLOPO_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SLOPO_PROJECT))

import runner.baseline_worktree as baseline_worktree
import runner.cache as cache_steps
import runner.embedding_services as embedding_services
import runner.errors as run_errors
import runner.paths as run_paths
from git_test_utils import run_git
from slopo.schema import SCHEMA_VERSION


class RunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "slopo.db"
        self.cache = self.root / "cache"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_keeps_generated_files_in_the_cache_without_repository_files(self) -> None:
        paths = run_paths.Paths.discover(self.root, self.cache)

        self.assertEqual(paths.config, SLOPO_PROJECT / "profiles" / "default.conf.yaml")
        self.assertEqual(paths.database, self.cache / "slopo.db")
        self.assertEqual(paths.report_dir, self.cache / "report")
        self.assertEqual(paths.base_worktree, self.cache / "base-worktree")
        self.assertEqual(paths.base_database, self.cache / "base" / "slopo.db")
        self.assertEqual(paths.ignore_file, self.cache / "slopo.ignore.txt")

    def test_prefers_the_repository_config_and_ignore_file(self) -> None:
        config_dir = self.root / ".quality-gates"
        config_dir.mkdir()
        config = config_dir / "slopo.conf.yaml"
        config.write_text("source_dir: .\n", encoding="utf-8")
        ignore_file = config_dir / "slopo.ignore.txt"
        ignore_file.write_text("", encoding="utf-8")

        paths = run_paths.Paths.discover(self.root, self.cache)

        self.assertEqual(paths.config, config)
        self.assertEqual(paths.ignore_file, ignore_file)

    def test_keeps_the_runtime_in_the_user_cache(self) -> None:
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            paths = run_paths.Paths.discover(self.root, self.cache)

        self.assertEqual(
            paths.runtime_dir, self.root / "cache" / "quality-gates" / "llama.cpp"
        )

    def test_keeps_a_database_with_the_current_schema(self) -> None:
        self._write_schema_version(SCHEMA_VERSION)

        cache_steps.remove_incompatible_database(self.database)

        self.assertTrue(self.database.is_file())

    def test_removes_an_outdated_database_and_its_sidecars(self) -> None:
        self._write_schema_version(SCHEMA_VERSION - 1)
        sidecars = [
            Path(f"{self.database}-wal"),
            Path(f"{self.database}-shm"),
        ]
        for sidecar in sidecars:
            sidecar.write_bytes(b"cache")

        cache_steps.remove_incompatible_database(self.database)

        self.assertFalse(self.database.exists())
        self.assertTrue(all(not sidecar.exists() for sidecar in sidecars))

    def test_rejects_a_symlinked_database(self) -> None:
        target = self.root / "target.db"
        target.write_bytes(b"database")
        self.database.symlink_to(target)

        with self.assertRaisesRegex(run_errors.RunError, "not a regular file"):
            cache_steps.remove_incompatible_database(self.database)

    def test_rejects_a_malformed_database(self) -> None:
        self.database.write_bytes(b"not sqlite")

        with self.assertRaisesRegex(run_errors.RunError, "Could not validate"):
            cache_steps.remove_incompatible_database(self.database)

    def test_stops_the_embedding_process_group(self) -> None:
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        services = embedding_services.EmbeddingServices(
            run_paths.Paths.discover(self.root, self.cache), Path("llama")
        )
        services.adapter = process
        try:
            services.stop()
        finally:
            if process.poll() is None:
                os.killpg(os.getpgid(process.pid), 9)
                process.wait()

        self.assertIsNotNone(process.returncode)
        self.assertIsNone(services.adapter)

    def _write_schema_version(self, version: int) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
            connection.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (version,)
            )


class BaselineWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        run_git(self.repository, "init", "-q")
        run_git(self.repository, "config", "user.name", "Test User")
        run_git(self.repository, "config", "user.email", "test@example.com")
        (self.repository / ".gitignore").write_text("ignored.bin\n")
        (self.repository / "tracked.txt").write_bytes(b"original\n")
        run_git(self.repository, "add", ".")
        run_git(self.repository, "commit", "-qm", "initial")
        self.first_commit = run_git(self.repository, "rev-parse", "HEAD")
        (self.repository / "tracked.txt").write_bytes(b"next snapshot\n")
        run_git(self.repository, "commit", "-qam", "next")
        self.next_commit = run_git(self.repository, "rev-parse", "HEAD")
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.baseline = self.cache / "base-worktree"
        self.paths = run_paths.Paths.discover(self.repository, self.cache)

    def test_discards_dirty_worktree_and_reuses_clean_replacement(self) -> None:
        baseline_worktree.prepare_base_worktree(self.paths, self.first_commit)
        (self.baseline / "tracked.txt").write_bytes(b"staged\n")
        run_git(self.baseline, "add", "tracked.txt")
        contents = {
            "tracked.txt": b"unstaged\x00bytes\n",
            "untracked.bin": b"untracked\x00bytes\n",
            "ignored.bin": b"ignored\x00bytes\n",
        }
        for name, content in contents.items():
            (self.baseline / name).write_bytes(content)
        with patch.object(baseline_worktree, "emit") as output:
            baseline_worktree.prepare_base_worktree(self.paths, self.next_commit)

        self.assertEqual(list(self.cache.glob("base-worktree-preserved-*")), [])
        self.assertEqual((self.baseline / "tracked.txt").read_bytes(), b"next snapshot\n")
        self.assertEqual(run_git(self.baseline, "show", ":tracked.txt"), "next snapshot")
        self.assertFalse((self.baseline / "untracked.bin").exists())
        self.assertFalse((self.baseline / "ignored.bin").exists())
        registrations = run_git(self.repository, "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {self.baseline}", registrations)
        self.assertEqual(registrations.count("worktree "), 2)
        output.assert_any_call(
            f"Discarded dirty SlopO baseline at {self.baseline}. Creating a clean baseline.",
            sys.stderr,
        )
        self.assertEqual(run_git(self.baseline, "rev-parse", "HEAD"), self.next_commit)
        self.assertEqual(run_git(self.baseline, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(run_git(self.baseline, "status", "--porcelain"), "")
        replacement_registration = (self.baseline / ".git").read_bytes()

        baseline_worktree.prepare_base_worktree(self.paths, self.next_commit)

        self.assertEqual((self.baseline / ".git").read_bytes(), replacement_registration)
        self.assertEqual(list(self.cache.glob("base-worktree-preserved-*")), [])

    def test_switches_a_clean_existing_worktree_to_the_requested_snapshot(self) -> None:
        baseline_worktree.prepare_base_worktree(self.paths, self.first_commit)
        registration = (self.baseline / ".git").read_bytes()

        baseline_worktree.prepare_base_worktree(self.paths, self.next_commit)

        self.assertEqual((self.baseline / ".git").read_bytes(), registration)
        self.assertEqual(run_git(self.baseline, "rev-parse", "HEAD"), self.next_commit)
        self.assertEqual(run_git(self.baseline, "status", "--porcelain"), "")
        self.assertEqual(list(self.cache.glob("base-worktree-preserved-*")), [])

    def test_failed_removal_leaves_the_dirty_worktree_untouched(self) -> None:
        baseline_worktree.prepare_base_worktree(self.paths, self.first_commit)
        tracked = self.baseline / "tracked.txt"
        tracked.write_bytes(b"preserve me\n")
        registration = (self.baseline / ".git").read_bytes()
        original_run_git = baseline_worktree._run_git

        def fail_removal(repository: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "remove"):
                raise run_errors.RunError("simulated removal failure")
            return original_run_git(repository, *arguments)

        with patch.object(baseline_worktree, "_run_git", side_effect=fail_removal):
            with self.assertRaisesRegex(run_errors.RunError, "simulated removal failure"):
                baseline_worktree.prepare_base_worktree(self.paths, self.next_commit)

        self.assertEqual(tracked.read_bytes(), b"preserve me\n")
        self.assertEqual((self.baseline / ".git").read_bytes(), registration)
        self.assertEqual(run_git(self.baseline, "rev-parse", "HEAD"), self.first_commit)
        self.assertEqual(list(self.cache.glob("base-worktree-preserved-*")), [])

    def test_rejects_a_symlinked_baseline_without_changing_its_target(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (target / "keep.txt").write_bytes(b"keep\n")
        self.baseline.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(run_errors.RunError, "not a regular directory"):
            baseline_worktree.prepare_base_worktree(self.paths, self.first_commit)

        self.assertTrue(self.baseline.is_symlink())
        self.assertEqual((target / "keep.txt").read_bytes(), b"keep\n")

    def test_rejects_a_directory_inside_another_worktree(self) -> None:
        child = self.repository / "child"
        child.mkdir()
        (child / "keep.txt").write_bytes(b"keep\n")
        paths = replace(self.paths, base_worktree=child)

        with self.assertRaisesRegex(run_errors.RunError, "not a Git worktree"):
            baseline_worktree.prepare_base_worktree(paths, self.first_commit)

        self.assertEqual((child / "keep.txt").read_bytes(), b"keep\n")


if __name__ == "__main__":
    unittest.main()
