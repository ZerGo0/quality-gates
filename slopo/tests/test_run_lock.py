import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


RUN_LOCK = Path(__file__).resolve().parents[1] / "run_lock.py"


class RunLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.lock_file = self.root / "slopo.lock"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_serializes_two_processes(self) -> None:
        events = self.root / "events.txt"
        first_command = (
            "from pathlib import Path; import time; "
            f"events = Path({str(events)!r}); "
            "events.write_text('first-start\\n'); "
            "time.sleep(0.4); "
            "events.open('a').write('first-end\\n')"
        )
        first = subprocess.Popen(
            self._command(sys.executable, "-c", first_command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self._wait_for(events)

            second_command = (
                "from pathlib import Path; "
                f"Path({str(events)!r}).open('a').write('second\\n')"
            )
            second = subprocess.run(
                self._command(sys.executable, "-c", second_command),
                check=False,
                capture_output=True,
                text=True,
            )
            first_stdout, first_stderr = first.communicate(timeout=5)
        finally:
            if first.poll() is None:
                first.terminate()
                first.communicate(timeout=5)

        self.assertEqual(first.returncode, 0, first_stdout + first_stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("SlopO is already running; waiting for it to finish.", second.stderr)
        self.assertEqual(events.read_text(encoding="utf-8"), "first-start\nfirst-end\nsecond\n")

    def test_propagates_the_child_exit_code(self) -> None:
        result = self._run(sys.executable, "-c", "raise SystemExit(37)")

        self.assertEqual(result.returncode, 37)

    def test_sets_the_lock_environment_marker(self) -> None:
        result = self._run(
            sys.executable,
            "-c",
            "import os; print(os.environ['SLOPO_RUN_LOCK_HELD'])",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "1\n")

    def test_rejects_a_symlink_lock_file(self) -> None:
        target = self.root / "target.lock"
        target.write_text("", encoding="utf-8")
        self.lock_file.symlink_to(target)

        result = self._run(sys.executable, "-c", "pass")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Lock file is not a regular file", result.stderr)

    def test_rejects_a_missing_lock_directory(self) -> None:
        self.lock_file = self.root / "missing" / "slopo.lock"

        result = self._run(sys.executable, "-c", "pass")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Lock directory is missing", result.stderr)

    def test_rejects_an_empty_command(self) -> None:
        result = subprocess.run(
            [sys.executable, str(RUN_LOCK), "--lock-file", str(self.lock_file)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("A command is required after --.", result.stderr)

    def _command(self, *command: str) -> list[str]:
        return [sys.executable, str(RUN_LOCK), "--lock-file", str(self.lock_file), "--", *command]

    def _run(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._command(*command), check=False, capture_output=True, text=True
        )

    def _wait_for(self, path: Path) -> None:
        deadline = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail("First locked process did not start in time.")
            time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
