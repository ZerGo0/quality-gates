import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

QUALITY_GATES = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(QUALITY_GATES / "scripts"))

import run_aislop
import run_directories
import aislop_output
import fallow_output


class AislopRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.run_directory = self.root / ".task" / "aislop" / "test-run"
        self.run_directory.mkdir(parents=True)
        self.repository_cache = self.root / "cache"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_aislop_json_prioritizes_changed_line_findings(self) -> None:
        stage = run_aislop.Stage("aislop", ("unused",), self.root)
        payload = {
            "score": 91,
            "summary": {"errors": 1, "warnings": 1},
            "diagnostics": [
                {
                    "filePath": "src/existing.ts",
                    "line": 8,
                    "severity": "warning",
                    "rule": "complexity/file-too-large",
                    "message": "Large file",
                    "changeContext": "existing-file-context",
                },
                {
                    "filePath": "src/changed.ts",
                    "line": 4,
                    "severity": "error",
                    "rule": "eslint/no-undef",
                    "message": "Unknown name",
                    "changeContext": "changed-line",
                },
            ],
        }
        result = run_aislop.StageResult(
            stage=stage,
            exit_code=1,
            stdout=json.dumps(payload),
            stderr="",
            elapsed_seconds=0.1,
            log_path=self.run_directory / "aislop.log",
            report_path=None,
        )

        presentation = aislop_output.present_aislop(result, self.root)

        self.assertEqual(
            presentation.summary,
            "score 91/100; 1 errors, 1 warnings; 1 changed-line, "
            "1 existing-context, 0 unclassified",
        )
        self.assertIn("src/changed.ts:4", presentation.findings[0])
        self.assertIn("src/existing.ts:8", presentation.findings[1])

    def test_changed_aislop_passes_with_only_existing_context_errors(self) -> None:
        stage = run_aislop.Stage("aislop", ("aislop", "scan", "--changes"), self.root)
        existing_error = {
            "filePath": "src/old.ts",
            "line": 3,
            "severity": "error",
            "changeContext": "existing-file-context",
        }
        changed_warning = {
            "filePath": "src/new.ts",
            "line": 9,
            "severity": "warning",
            "changeContext": "changed-line",
        }
        changed_error = {**changed_warning, "severity": "error"}

        def result_for(diagnostics: list[dict[str, object]]) -> run_aislop.StageResult:
            return run_aislop.StageResult(
                stage=stage,
                exit_code=1,
                stdout=json.dumps({"diagnostics": diagnostics}),
                stderr="",
                elapsed_seconds=0.1,
                log_path=self.run_directory / "aislop.log",
                report_path=None,
            )

        self.assertTrue(result_for([existing_error, changed_warning]).passed)
        self.assertFalse(result_for([existing_error, changed_error]).passed)
        full_scan = replace(
            result_for([existing_error]),
            stage=run_aislop.Stage("aislop", ("aislop", "scan"), self.root),
        )
        self.assertFalse(full_scan.passed)

    def test_pipeline_continues_after_failure_and_writes_full_logs(self) -> None:
        stages = (
            run_aislop.Stage(
                "first",
                (
                    sys.executable,
                    "-c",
                    "print('ERROR: src/a.py:3: first failed'); raise SystemExit(2)",
                ),
                self.root,
            ),
            run_aislop.Stage(
                "second",
                (sys.executable, "-c", "print('second completed')"),
                self.root,
            ),
        )

        exit_code = run_aislop.run_pipeline(stages, self.run_directory, self.root)

        self.assertEqual(exit_code, 1)
        self.assertIn("first failed", (self.run_directory / "first.log").read_text())
        second_log = (self.run_directory / "second.log").read_text()
        self.assertIn("second completed", second_log)
        raw_output = (self.run_directory / "raw-output.log").read_text()
        self.assertIn("===== first =====", raw_output)
        self.assertIn("ERROR: src/a.py:3: first failed", raw_output)
        self.assertIn("===== second =====", raw_output)
        self.assertIn("second completed", raw_output)
        manifest = json.loads((self.run_directory / "summary.json").read_text())
        self.assertFalse(manifest["passed"])
        self.assertEqual(
            manifest["rawOutput"], ".task/aislop/test-run/raw-output.log"
        )
        self.assertEqual([stage["exitCode"] for stage in manifest["stages"]], [2, 0])

    def test_parallel_stages_overlap_and_preserve_log_order(self) -> None:
        wait_for_peer = (
            "from pathlib import Path\n"
            "import sys, time\n"
            "own, peer = map(Path, sys.argv[1:3])\n"
            "own.touch()\n"
            "deadline = time.monotonic() + 2\n"
            "while not peer.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "if not peer.exists():\n"
            "    raise SystemExit(3)\n"
            "print(own.name)\n"
            "raise SystemExit(int(sys.argv[3]))\n"
        )
        first_ready = self.root / "first.ready"
        second_ready = self.root / "second.ready"
        stages = (
            run_aislop.Stage(
                "first",
                (sys.executable, "-c", wait_for_peer, str(first_ready), str(second_ready), "2"),
                self.root,
                parallel=True,
            ),
            run_aislop.Stage(
                "second",
                (sys.executable, "-c", wait_for_peer, str(second_ready), str(first_ready), "0"),
                self.root,
                parallel=True,
            ),
            run_aislop.Stage(
                "last",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; assert all(Path(p).exists() for p in sys.argv[1:]); print('last ran')",
                    str(first_ready),
                    str(second_ready),
                ),
                self.root,
            ),
        )

        exit_code = run_aislop.run_pipeline(stages, self.run_directory, self.root)

        self.assertEqual(exit_code, 1)
        manifest = json.loads((self.run_directory / "summary.json").read_text())
        self.assertEqual([stage["exitCode"] for stage in manifest["stages"]], [2, 0, 0])
        raw_output = (self.run_directory / "raw-output.log").read_text()
        self.assertLess(raw_output.index("===== first ====="), raw_output.index("===== second ====="))
        self.assertLess(raw_output.index("===== second ====="), raw_output.index("===== last ====="))
        self.assertIn("last ran", raw_output)
        self.assertEqual(list(self.run_directory.glob(".raw-*.log")), [])

    def test_stages_compare_aislop_and_fallow_with_the_change_base(self) -> None:
        stages = {
            stage.name: stage
            for stage in run_aislop.build_stages(
                self.root, self.run_directory, self.repository_cache, "abc123"
            )
        }

        for name in ("aislop", "fallow"):
            command = stages[name].command
            self.assertEqual(command[command.index("--base") + 1], "abc123")

    def test_full_repository_stages_drop_change_scoping(self) -> None:
        stages = {
            stage.name: stage
            for stage in run_aislop.build_stages(
                self.root, self.run_directory, self.repository_cache, None
            )
        }

        self.assertNotIn("--changes", stages["aislop"].command)
        self.assertIn("--all", stages["anti-slop"].command)
        self.assertNotIn("--changes", stages["ai-cleanup"].command)
        self.assertNotIn("audit", stages["fallow"].command)
        self.assertIn("--all", stages["slopo"].command)

    def test_stages_write_nothing_into_the_repository(self) -> None:
        stages = {
            stage.name: stage
            for stage in run_aislop.build_stages(
                self.root, self.run_directory, self.repository_cache, None
            )
        }

        slopo_command = stages["slopo"].command
        self.assertEqual(
            slopo_command[slopo_command.index("--cache-dir") + 1],
            str(self.repository_cache / "slopo"),
        )
        self.assertEqual(
            stages["fallow"].environment["FALLOW_CACHE_DIR"],
            str(self.repository_cache / "fallow"),
        )
        self.assertEqual(stages["aislop"].environment["AISLOP_NO_HISTORY"], "1")
        fallow_command = stages["fallow"].command
        fallow_output = Path(fallow_command[fallow_command.index("--output-file") + 1])
        self.assertEqual(fallow_output, stages["fallow"].report_source)
        self.assertTrue(fallow_output.is_relative_to(tempfile.gettempdir()))

    def test_run_stage_moves_the_report_into_the_run_directory(self) -> None:
        report_source = self.root / "outside" / "fallow.json"
        report_source.parent.mkdir()
        stage = run_aislop.Stage(
            "fallow",
            (sys.executable, "-c", f"open({str(report_source)!r}, 'w').write('{{}}')"),
            self.root,
            report_filename="fallow.json",
            report_source=report_source,
        )

        result = run_aislop.run_stage(stage, self.run_directory)

        self.assertEqual(result.report_path, self.run_directory / "fallow.json")
        self.assertEqual(result.report_path.read_text(), "{}")
        self.assertFalse(report_source.exists())

    def test_fallow_repository_report_lists_every_finding(self) -> None:
        report = self.run_directory / "fallow.json"
        report.write_text(
            json.dumps(
                {
                    "kind": "combined",
                    "check": {
                        "total_issues": 1,
                        "unused_exports": [
                            {
                                "path": "src/a.ts",
                                "line": 3,
                                "finding_id": "dc1:unused-export:1",
                                "actions": [{"description": "Remove the export"}],
                            }
                        ],
                    },
                    "dupes": {
                        "clone_groups": [
                            {
                                "fingerprint": "dup:1",
                                "line_count": 9,
                                "instances": [
                                    {"file": "src/b.ts", "start_line": 4},
                                    {"file": "src/c.ts", "start_line": 8},
                                ],
                            }
                        ]
                    },
                    "health": {"findings": []},
                }
            ),
            encoding="utf-8",
        )
        stage = run_aislop.Stage("fallow", ("unused",), self.root, report_filename="fallow.json")
        result = run_aislop.StageResult(
            stage=stage,
            exit_code=1,
            stdout="",
            stderr="",
            elapsed_seconds=0.0,
            log_path=self.run_directory / "fallow.log",
            report_path=report,
        )

        presentation = fallow_output.present_fallow(result, self.root)

        self.assertEqual(
            presentation.summary, "1 dead-code issues, 1 clone groups, 0 health findings"
        )
        self.assertEqual(
            presentation.findings,
            (
                "src/a.ts:3 finding: Remove the export",
                "src/b.ts:4 duplication [dup:1]: 2 instances, 9 lines",
            ),
        )

    def test_relevant_text_lines_deduplicates_progress_noise(self) -> None:
        lines = aislop_output.relevant_text_lines(
            "Indexing 100 files\n"
            "ERROR: src/a.py:3: duplicate\n"
            "ERROR: src/a.py:3: duplicate\n"
            "Report: .slopo/repository/report/cluster-1.md\n"
        )

        self.assertEqual(
            lines,
            [
                "ERROR: src/a.py:3: duplicate",
                "Report: .slopo/repository/report/cluster-1.md",
            ],
        )

    def test_fallow_json_reports_new_only_attribution(self) -> None:
        report_path = self.run_directory / "fallow.json"
        report_path.write_text(
            json.dumps(
                {
                    "verdict": "pass",
                    "attribution": {
                        "gate": "new-only",
                        "complexity_introduced": 0,
                        "complexity_inherited": 1,
                        "duplication_introduced": 0,
                        "duplication_inherited": 2,
                    },
                    "duplication": {"clone_groups": []},
                }
            )
        )
        stage = run_aislop.Stage(
            "fallow", ("unused",), self.root, report_filename="fallow.json"
        )
        result = run_aislop.StageResult(
            stage=stage,
            exit_code=0,
            stdout="",
            stderr="",
            elapsed_seconds=0.1,
            log_path=self.run_directory / "fallow.log",
            report_path=report_path,
        )

        presentation = fallow_output.present_fallow(result, self.root)

        self.assertEqual(
            presentation.summary,
            "verdict pass; 0 introduced, 3 inherited; gate new-only",
        )
        self.assertEqual(presentation.findings, ())

    def test_fallow_json_prioritizes_introduced_findings(self) -> None:
        report_path = self.run_directory / "fallow.json"
        report_path.write_text(
            json.dumps(
                {
                    "verdict": "fail",
                    "attribution": {
                        "gate": "new-only",
                        "dead_code_introduced": 1,
                        "dead_code_inherited": 0,
                    },
                    "dead_code": {
                        "unused_files": [
                            {
                                "path": "src/probe.ts",
                                "introduced": True,
                                "actions": [{"description": "Delete this file"}],
                            }
                        ],
                        "workspace_diagnostics": [
                            {"path": "node_modules", "message": "Missing dependencies"}
                        ],
                    },
                    "duplication": {"clone_groups": []},
                }
            )
        )
        stage = run_aislop.Stage(
            "fallow", ("unused",), self.root, report_filename="fallow.json"
        )
        result = run_aislop.StageResult(
            stage=stage,
            exit_code=1,
            stdout="",
            stderr="",
            elapsed_seconds=0.1,
            log_path=self.run_directory / "fallow.log",
            report_path=report_path,
        )

        presentation = fallow_output.present_fallow(result, self.root)

        self.assertEqual(
            presentation.findings,
            ("src/probe.ts introduced finding: Delete this file",),
        )

    def test_raw_output_preserves_non_utf8_bytes(self) -> None:
        stage = run_aislop.Stage(
            "binary",
            (sys.executable, "-c", "import os; os.write(1, b'\\xff\\xfe')"),
            self.root,
        )

        result = run_aislop.run_stage(stage, self.run_directory)

        self.assertEqual(result.exit_code, 0)
        raw_output = (self.run_directory / "raw-output.log").read_bytes()
        self.assertIn(b"\xff\xfe", raw_output)

    def test_raw_output_records_launch_failure(self) -> None:
        stage = run_aislop.Stage(
            "missing", ("quality-gates-command-that-does-not-exist",), self.root
        )

        result = run_aislop.run_stage(stage, self.run_directory)

        self.assertEqual(result.exit_code, 127)
        raw_output = (self.run_directory / "raw-output.log").read_text()
        self.assertIn("Could not start missing", raw_output)
        self.assertIn("===== end exit=127 =====", raw_output)

    def test_retention_keeps_latest_generated_runs_and_unknown_directories(self) -> None:
        artifact_root = self.root / ".task" / "aislop-retention"
        artifact_root.mkdir(parents=True)
        run_names = [
            f"202609{i:02d}T120000Z-{100 + i}-abcdefgh" for i in range(1, 6)
        ]
        for name in run_names:
            run = artifact_root / name
            run.mkdir()
            (run / "raw-output.log").write_text(name)
        unknown = artifact_root / "keep-me"
        unknown.mkdir()
        malformed = artifact_root / "20269999T120000Z-999-abcdefgh"
        malformed.mkdir()
        external = self.root / "external-reports"
        external.mkdir()
        symlink = artifact_root / "20260801T120000Z-98-abcdefgh"
        symlink.symlink_to(external, target_is_directory=True)
        matching_file = artifact_root / "20260802T120000Z-99-abcdefgh"
        matching_file.write_text("not a directory")

        run_directories.prune_run_directories(artifact_root, retain=3)

        remaining = {path.name for path in artifact_root.iterdir()}
        self.assertEqual(
            remaining,
            {
                *run_names[-3:],
                "keep-me",
                malformed.name,
                symlink.name,
                matching_file.name,
            },
        )

    def test_retention_protects_active_runs_and_reclaims_stale_markers(self) -> None:
        artifact_root = self.root / ".task" / "aislop-active"
        artifact_root.mkdir(parents=True)
        stale = artifact_root / "20260901T120000Z-101-abcdefgh"
        oldest = artifact_root / "20260902T120000Z-102-abcdefgh"
        active = artifact_root / "20260903T120000Z-103-abcdefgh"
        newest = artifact_root / "20260904T120000Z-104-abcdefgh"
        for run in (oldest, active, stale, newest):
            run.mkdir()
        (active / run_directories.ACTIVE_MARKER).write_text(f"{os.getpid()}\n")
        (stale / run_directories.ACTIVE_MARKER).write_text("999999999\n")

        run_directories.prune_run_directories(artifact_root, retain=2)

        remaining = {path.name for path in artifact_root.iterdir()}
        self.assertEqual(remaining, {active.name, oldest.name, newest.name})

    def test_run_directory_lifecycle_clears_its_active_marker(self) -> None:
        run_directory = run_directories.create_run_directory(self.root / "reports")

        self.assertTrue((run_directory / run_directories.ACTIVE_MARKER).is_file())

        run_directories.finish_run_directory(run_directory)

        self.assertFalse((run_directory / run_directories.ACTIVE_MARKER).exists())


if __name__ == "__main__":
    unittest.main()
