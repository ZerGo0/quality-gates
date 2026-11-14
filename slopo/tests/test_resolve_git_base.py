import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from git_test_utils import run_git  # noqa: E402
from resolve_git_base import GitBaseResolutionError, resolve_git_base  # noqa: E402


class ResolveGitBaseTests(unittest.TestCase):
    def test_main_uses_head_even_when_tracking_origin_main(self) -> None:
        with self._repository() as repository_root:
            old_main_sha = self._main(repository_root, "main.txt", "old\n", "old main")
            run_git(repository_root, "update-ref", "refs/remotes/origin/main", old_main_sha)
            run_git(repository_root, "config", "branch.main.remote", "origin")
            run_git(repository_root, "config", "branch.main.merge", "refs/heads/main")
            head_sha = self._commit(repository_root, "main.txt", "new\n", "new main")
            (repository_root / "main.txt").write_text("uncommitted\n", encoding="utf-8")

            resolved = resolve_git_base(repository_root)

            self.assertEqual(resolved.snapshot_commit, head_sha)
            self.assertEqual(resolved.comparison_commit, head_sha)
            self.assertNotEqual(resolved.comparison_commit, old_main_sha)

    def test_feature_uses_fork_point_despite_upstream_and_main_movement(self) -> None:
        for advance_remote_main, advance_local_main in (
            (False, False),
            (True, False),
            (False, True),
        ):
            with self.subTest(
                remote=advance_remote_main, local=advance_local_main
            ), self._repository() as repository_root:
                remote_main_sha = self._main(repository_root)
                run_git(
                    repository_root,
                    "update-ref",
                    "refs/remotes/origin/main",
                    remote_main_sha,
                )
                expected_base = remote_main_sha
                if advance_local_main:
                    expected_base = self._commit(
                        repository_root, "local-main.txt", "local\n", "local main"
                    )
                run_git(repository_root, "checkout", "-q", "-b", "feature")
                published = self._commit(
                    repository_root, "feature.txt", "published\n", "published"
                )
                self._track_feature(repository_root, published)
                unpublished = self._commit(
                    repository_root, "unpublished.txt", "unpublished\n", "unpublished"
                )
                if advance_remote_main:
                    run_git(repository_root, "checkout", "-q", "main")
                    remote_tip = self._commit(
                        repository_root, "remote-main.txt", "remote\n", "remote main"
                    )
                    run_git(
                        repository_root,
                        "update-ref",
                        "refs/remotes/origin/main",
                        remote_tip,
                    )
                    run_git(repository_root, "checkout", "-q", "feature")
                (repository_root / "feature.txt").write_text(
                    "uncommitted\n", encoding="utf-8"
                )

                resolved = resolve_git_base(repository_root)

                self.assertEqual(resolved.snapshot_commit, expected_base)
                self.assertEqual(resolved.comparison_commit, expected_base)
                self.assertNotEqual(resolved.comparison_commit, published)
                self.assertNotEqual(resolved.comparison_commit, unpublished)

    def test_uses_explicit_override_before_all_other_candidates(self) -> None:
        with self._repository() as repository_root:
            main_sha = self._commit(repository_root, "main.txt", "main\n", "main")
            run_git(repository_root, "branch", "-M", "main")
            run_git(repository_root, "checkout", "-q", "-b", "feature")
            feature_sha = self._commit(
                repository_root, "feature.txt", "feature\n", "feature"
            )

            with patch.dict(os.environ, {"SLOPO_AUDIT_BASE": "HEAD"}):
                resolved = resolve_git_base(repository_root)

            self.assertEqual(resolved.snapshot_commit, feature_sha)
            self.assertEqual(resolved.comparison_commit, feature_sha)
            self.assertEqual(resolved.description, "SLOPO_AUDIT_BASE=HEAD")
            self.assertNotEqual(resolved.snapshot_commit, main_sha)

    def test_uses_origin_head_when_no_upstream_is_configured(self) -> None:
        with self._repository() as repository_root:
            main_sha = self._commit(repository_root, "main.txt", "main\n", "main")
            run_git(repository_root, "branch", "-M", "main")
            run_git(repository_root, "checkout", "-q", "-b", "feature")
            self._commit(repository_root, "feature.txt", "feature\n", "feature")
            run_git(repository_root, "update-ref", "refs/remotes/origin/HEAD", main_sha)
            run_git(repository_root, "update-ref", "refs/remotes/origin/main", main_sha)

            resolved = resolve_git_base(repository_root)

            self.assertEqual(resolved.snapshot_commit, main_sha)
            self.assertEqual(resolved.comparison_commit, main_sha)
            self.assertEqual(resolved.description, "merge-base with origin/HEAD")

    def test_rejects_invalid_explicit_override_without_falling_back(self) -> None:
        with self._repository() as repository_root:
            self._commit(repository_root, "main.txt", "main\n", "main")
            run_git(repository_root, "branch", "-M", "main")

            with patch.dict(os.environ, {"SLOPO_AUDIT_BASE": "missing/base"}):
                with self.assertRaisesRegex(
                    GitBaseResolutionError,
                    "Base does not name a commit",
                ):
                    resolve_git_base(repository_root)

    def test_uses_local_main_when_detached_without_remote(self) -> None:
        with self._repository() as repository_root:
            main_sha = self._commit(repository_root, "main.txt", "main\n", "main")
            run_git(repository_root, "branch", "-M", "main")
            run_git(repository_root, "checkout", "-q", "--detach")

            resolved = resolve_git_base(repository_root)

            self.assertEqual(resolved.snapshot_commit, main_sha)
            self.assertEqual(resolved.comparison_commit, main_sha)
            self.assertEqual(resolved.description, "local main")

    def test_requires_the_exact_git_worktree_root(self) -> None:
        with self._repository() as repository_root:
            self._commit(repository_root, "main.txt", "main\n", "main")
            child = repository_root / "child"
            child.mkdir()

            with self.assertRaisesRegex(ValueError, "git worktree root"):
                resolve_git_base(child)

    def test_cli_writes_only_sha_to_stdout_and_description_to_stderr(self) -> None:
        with self._repository() as repository_root:
            main_sha = self._commit(repository_root, "main.txt", "main\n", "main")
            run_git(repository_root, "branch", "-M", "main")
            script = Path(__file__).resolve().parents[1] / "resolve_git_base.py"

            result = subprocess.run(
                [sys.executable, str(script), "--repository-root", str(repository_root)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertEqual(result.stdout, f"{main_sha}\t{main_sha}\n")
            self.assertTrue(result.stderr.strip())

    def test_named_diverged_base_snapshots_tip_but_compares_from_merge_base(
        self,
    ) -> None:
        with self._repository() as repository_root:
            initial_sha = self._commit(
                repository_root, "initial.txt", "initial\n", "initial"
            )
            run_git(repository_root, "branch", "-M", "main")
            run_git(repository_root, "checkout", "-q", "-b", "feature")
            self._commit(repository_root, "feature.txt", "feature\n", "feature")
            run_git(repository_root, "checkout", "-q", "main")
            main_sha = self._commit(
                repository_root, "main.txt", "main\n", "main change"
            )
            run_git(repository_root, "checkout", "-q", "feature")

            resolved = resolve_git_base(repository_root, "main")

            self.assertEqual(resolved.snapshot_commit, main_sha)
            self.assertEqual(resolved.comparison_commit, initial_sha)

    def test_empty_environment_override_uses_automatic_detection(self) -> None:
        with self._repository() as repository_root:
            main_sha = self._main(repository_root)

            with patch.dict(os.environ, {"SLOPO_AUDIT_BASE": ""}):
                resolved = resolve_git_base(repository_root)

            self.assertEqual(resolved.snapshot_commit, main_sha)
            self.assertEqual(resolved.comparison_commit, main_sha)

    def test_explicit_base_overrides_invalid_environment_base(self) -> None:
        with self._repository() as repository_root:
            main_sha = self._commit(repository_root, "main.txt", "main\n", "main")
            run_git(repository_root, "branch", "-M", "main")

            with patch.dict(os.environ, {"SLOPO_AUDIT_BASE": "missing"}):
                resolved = resolve_git_base(repository_root, "main")

            self.assertEqual(resolved.snapshot_commit, main_sha)

    def _repository(self) -> tempfile.TemporaryDirectory[str]:
        temporary_directory = tempfile.TemporaryDirectory()
        repository_root = Path(temporary_directory.name)
        run_git(repository_root, "init", "--quiet")
        run_git(repository_root, "config", "user.email", "test@example.com")
        run_git(repository_root, "config", "user.name", "SlopO Test")
        return _RepositoryContext(temporary_directory, repository_root)

    def _commit(
        self, repository_root: Path, name: str, contents: str, message: str
    ) -> str:
        (repository_root / name).write_text(contents, encoding="utf-8")
        run_git(repository_root, "add", name)
        run_git(repository_root, "commit", "--quiet", "-m", message)
        return run_git(repository_root, "rev-parse", "HEAD")

    def _main(
        self,
        repository_root: Path,
        name: str = "main.txt",
        contents: str = "main\n",
        message: str = "main",
    ) -> str:
        commit = self._commit(repository_root, name, contents, message)
        run_git(repository_root, "branch", "-M", "main")
        return commit

    def _track_feature(self, repository_root: Path, feature_commit: str) -> None:
        run_git(
            repository_root, "update-ref", "refs/remotes/origin/feature", feature_commit
        )
        run_git(repository_root, "config", "branch.feature.remote", "origin")
        run_git(repository_root, "config", "branch.feature.merge", "refs/heads/feature")


class _RepositoryContext:
    def __init__(
        self, temporary_directory: tempfile.TemporaryDirectory[str], repository_root: Path
    ) -> None:
        self._temporary_directory = temporary_directory
        self.repository_root = repository_root

    def __enter__(self) -> Path:
        return self.repository_root

    def __exit__(self, *unused: object) -> None:
        self._temporary_directory.cleanup()
