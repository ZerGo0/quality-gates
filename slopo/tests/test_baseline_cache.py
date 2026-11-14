import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CACHE = Path(__file__).resolve().parents[1] / "baseline_cache.py"


class ReportCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.state = self.root / "state.json"
        self.input_file = self.root / "profile.yaml"
        self.input_file.write_text("threshold: 3\n", encoding="utf-8")
        self.db_file = self.root / "baseline.db"
        self.db_file.write_bytes(b"database")
        self.report_dir = self.root / "reports"
        self.report_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_records_then_hits(self) -> None:
        recorded = self._run("record")
        checked = self._run("check")

        self.assertEqual(recorded.returncode, 0)
        self.assertEqual(recorded.stdout, "SlopO test cache saved.\n")
        self.assertEqual(checked.returncode, 0)
        self.assertEqual(checked.stdout, "SlopO test cache hit.\n")

    def test_key_or_input_change_misses(self) -> None:
        self.assertEqual(self._run("record").returncode, 0)

        self.assertEqual(self._run("check", cache_key="next").returncode, 1)
        self.input_file.write_text("threshold: 4\n", encoding="utf-8")
        self.assertEqual(self._run("check").returncode, 1)

    def test_database_content_can_be_fingerprinted_as_an_input(self) -> None:
        self.assertEqual(self._run("record", fingerprint_db=True).returncode, 0)
        self.db_file.write_bytes(b"changed database")

        result = self._run("check", fingerprint_db=True)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "SlopO test cache miss.\n")

    def test_missing_state_is_a_miss(self) -> None:
        result = self._run("check")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "SlopO test cache miss.\n")

    def test_malformed_state_is_an_error(self) -> None:
        self.state.write_text('{"fingerprint":"not enough"}\n', encoding="utf-8")

        result = self._run("check")

        self.assertEqual(result.returncode, 2)
        self.assertIn("State file has an invalid format", result.stderr)

    def test_missing_cached_artifact_is_a_miss(self) -> None:
        self.assertEqual(self._run("record").returncode, 0)
        self.db_file.unlink()

        result = self._run("check")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "SlopO test cache miss.\n")

    def test_changed_report_is_a_miss(self) -> None:
        report = self.report_dir / "cluster-001.md"
        report.write_text("before\n", encoding="utf-8")
        self.assertEqual(self._run("record").returncode, 0)
        report.write_text("after\n", encoding="utf-8")

        result = self._run("check")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "SlopO test cache miss.\n")

    def test_rejects_symlinked_artifacts(self) -> None:
        linked_db = self.root / "linked.db"
        linked_db.symlink_to(self.db_file)

        result = self._run("record", db_file=linked_db)

        self.assertEqual(result.returncode, 2)
        self.assertIn("Database file is not a regular file", result.stderr)

    def test_rejects_a_dangling_symlink_during_check(self) -> None:
        self.assertEqual(self._run("record").returncode, 0)
        self.db_file.unlink()
        self.db_file.symlink_to(self.root / "missing.db")

        result = self._run("check")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Database file is not a regular file", result.stderr)

    def test_recorded_state_is_atomic_and_strict_json(self) -> None:
        result = self._run("record")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            self.state.read_bytes(),
            json.dumps(
                {
                    "fingerprint": json.loads(self.state.read_text())["fingerprint"],
                    "format_version": 3,
                    "report_fingerprint": json.loads(self.state.read_text())[
                        "report_fingerprint"
                    ],
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n",
        )
        self.assertEqual(list(self.root.glob(f".{self.state.name}.*.tmp")), [])

    def _run(
        self,
        action: str,
        *,
        cache_key: str = "test-key",
        db_file: Path | None = None,
        fingerprint_db: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        arguments = [
            sys.executable,
            str(CACHE),
            action,
            "--state-file",
            str(self.state),
            "--cache-key",
            cache_key,
            "--label",
            "SlopO test",
            "--input-file",
            str(self.input_file),
            "--db-file",
            str(db_file or self.db_file),
            "--report-dir",
            str(self.report_dir),
        ]
        if fingerprint_db:
            arguments.extend(("--input-file", str(self.db_file)))
        return subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
