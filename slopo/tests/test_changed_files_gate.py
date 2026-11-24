import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from git_test_utils import run_git  # noqa: E402
from changed_files_gate import (  # noqa: E402
    ReportFormatError,
    clear_generated_reports,
    collect_changed_files,
    find_new_clusters,
    load_cluster_reports,
    parse_cluster_report,
    render_errors,
)


CLUSTER_REPORT = """\
## (1) score 1.08-1.15

Hash: `abc123def456`

### ______ 1 ______

- `backend/changed.go` lines 10-20

```go
func changed()
```

- `backend/existing.go` lines 30-40

```go
func existing()
```

```go
return shared()
```
"""

CLUSTER_REPORT_NAME = "cluster-001.md"
CHANGED_PATH = "backend/changed.go"


class ReportTests(unittest.TestCase):
    def find_clusters_between(
        self, root: Path, current_report: str, base_report: str
    ) -> list:
        current_dir = root / "current"
        base_dir = root / "base"
        current_dir.mkdir()
        base_dir.mkdir()
        (current_dir / CLUSTER_REPORT_NAME).write_text(current_report, encoding="utf-8")
        (base_dir / CLUSTER_REPORT_NAME).write_text(base_report, encoding="utf-8")
        return find_new_clusters(
            load_cluster_reports(current_dir),
            load_cluster_reports(base_dir),
            {CHANGED_PATH},
        )

    def test_does_not_find_inherited_cluster_when_one_location_is_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            matches = self.find_clusters_between(
                Path(temporary_directory),
                CLUSTER_REPORT.replace(
                    "Hash: `abc123def456`", "Hash: `def456abc123`"
                ).replace("lines 10-20", "lines 100-110"),
                CLUSTER_REPORT,
            )

            self.assertEqual(matches, [])

    def test_finds_cluster_with_a_new_changed_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            matches = self.find_clusters_between(
                root,
                CLUSTER_REPORT,
                CLUSTER_REPORT.replace(
                    CHANGED_PATH, "backend/other.go"
                ).replace("func changed()", "func other()"),
            )

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0][0].cluster_hash, "abc123def456")
            self.assertEqual(matches[0][1][0].path, CHANGED_PATH)

            output = render_errors(matches)
            self.assertIn(
                "ERROR: backend/changed.go:10: new duplicate cluster abc123def456",
                output,
            )
            self.assertIn("[existing] backend/existing.go:30-40", output)
            self.assertIn(f"Report: {root / 'current' / CLUSTER_REPORT_NAME}", output)

    def test_counts_clusters_after_a_member_is_added_or_removed(self) -> None:
        removed_member = CLUSTER_REPORT.replace(
            "- `backend/existing.go` lines 30-40",
            "- `backend/existing.go` lines 30-40\n- `backend/removed.go` lines 50-60",
        )
        added_copy = CLUSTER_REPORT.replace(
            "- `backend/changed.go` lines 10-20",
            "- `backend/changed.go` lines 10-20\n- `backend/changed.go` lines 50-60",
        )
        cases = (
            ("duplicate member removed", CLUSTER_REPORT, removed_member, 0),
            ("exact copy added in the same file", added_copy, CLUSTER_REPORT, 1),
        )
        for name, current_report, base_report, expected_count in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary_directory:
                matches = self.find_clusters_between(
                    Path(temporary_directory), current_report, base_report
                )

                self.assertEqual(len(matches), expected_count)

    def test_renders_one_error_for_multiple_changed_locations_in_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_dir = Path(temporary_directory)
            (report_dir / "cluster-001.md").write_text(
                CLUSTER_REPORT, encoding="utf-8"
            )

            matches = find_new_clusters(
                load_cluster_reports(report_dir),
                [],
                {CHANGED_PATH, "backend/existing.go"},
            )
            output = render_errors(matches)

            self.assertEqual(output.count("ERROR:"), 1)
            self.assertIn("[changed] backend/changed.go:10-20", output)
            self.assertIn("[changed] backend/existing.go:30-40", output)

    def test_rejects_cluster_without_multiple_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "cluster-1.md"
            report_path.write_text(
                CLUSTER_REPORT.replace(
                    "- `backend/existing.go` lines 30-40",
                    "not a SlopO location",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ReportFormatError, "at least two duplicate locations"
            ):
                parse_cluster_report(report_path)

    def test_hashes_shared_body_instead_of_context_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "cluster-1.md"
            report_path.write_text(CLUSTER_REPORT, encoding="utf-8")

            report = parse_cluster_report(report_path)

            expected = hashlib.sha256(b"return shared()\n").hexdigest()
            self.assertEqual(
                {location.body_hash for location in report.locations}, {expected}
            )

    def test_clear_removes_only_generated_report_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            report_dir = Path(temporary_directory)
            generated = [report_dir / "index.md", report_dir / "cluster-1.md"]
            preserved = report_dir / "notes.md"
            for path in [*generated, preserved]:
                path.write_text("test", encoding="utf-8")

            clear_generated_reports(report_dir)

            self.assertTrue(preserved.exists())
            self.assertTrue(all(not path.exists() for path in generated))


class ChangedFilesTests(unittest.TestCase):
    def test_collects_committed_local_untracked_and_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            run_git(repository_root, "init", "--quiet")
            run_git(repository_root, "config", "user.email", "test@example.com")
            run_git(repository_root, "config", "user.name", "SlopO Test")

            tracked = repository_root / "tracked.py"
            deleted = repository_root / "deleted.py"
            tracked.write_text("before\n", encoding="utf-8")
            deleted.write_text("before\n", encoding="utf-8")
            run_git(repository_root, "add", "tracked.py", "deleted.py")
            run_git(repository_root, "commit", "--quiet", "-m", "fixture")
            base_commit = run_git(repository_root, "rev-parse", "HEAD")

            committed = repository_root / "committed.py"
            committed.write_text("committed branch change\n", encoding="utf-8")
            run_git(repository_root, "add", "committed.py")
            run_git(repository_root, "commit", "--quiet", "-m", "branch change")

            tracked.write_text("after\n", encoding="utf-8")
            deleted.unlink()
            (repository_root / "untracked.py").write_text("new\n", encoding="utf-8")

            self.assertEqual(
                collect_changed_files(repository_root, base_commit),
                {"committed.py", "deleted.py", "tracked.py", "untracked.py"},
            )

    def test_rename_paths_follow_git_staging_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository_root = Path(temporary_directory)
            run_git(repository_root, "init", "--quiet")
            run_git(repository_root, "config", "user.email", "test@example.com")
            run_git(repository_root, "config", "user.name", "SlopO Test")
            original = repository_root / "original.py"
            renamed = repository_root / "renamed.py"
            original.write_text("def example():\n    return 1\n", encoding="utf-8")
            run_git(repository_root, "add", "original.py")
            run_git(repository_root, "commit", "--quiet", "-m", "fixture")
            base_commit = run_git(repository_root, "rev-parse", "HEAD")

            original.rename(renamed)

            self.assertEqual(
                collect_changed_files(repository_root, base_commit),
                {"original.py", "renamed.py"},
            )

            run_git(repository_root, "add", "-A")

            self.assertEqual(
                collect_changed_files(repository_root, base_commit),
                {"renamed.py"},
            )

if __name__ == "__main__":
    unittest.main()
