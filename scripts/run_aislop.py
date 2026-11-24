#!/usr/bin/env python3
"""Run the agent quality gates with compact, model-friendly output."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Sequence

from aislop_output import Stage, StageResult, relative_path, render_results
from run_directories import create_run_directory, finish_run_directory

QUALITY_GATES = Path(__file__).resolve().parents[1]
NODE_BIN = QUALITY_GATES / "node_modules" / ".bin"
REPOSITORY_CACHE_VARIABLE = "QUALITY_GATES_REPOSITORY_CACHE"


def repository_root() -> Path:
    toplevel = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    )
    return Path(toplevel.strip())


def repository_cache_directory() -> Path:
    repository_cache = Path(os.environ.get(REPOSITORY_CACHE_VARIABLE, ""))
    if not repository_cache.is_absolute():
        raise SystemExit(f"{REPOSITORY_CACHE_VARIABLE} must be an absolute path")
    return repository_cache


def build_stages(
    root: Path, run_directory: Path, repository_cache: Path, base: str | None
) -> tuple[Stage, ...]:
    scripts = QUALITY_GATES / "scripts"
    fallow_report = Path(tempfile.gettempdir()) / f"{run_directory.name}-fallow.json"
    full_repository = base is None
    aislop_scope = () if full_repository else ("--changes", "--base", base)
    aislop_flags = ("--json", "--ignore", "knip/skipped")
    file_scope = ("--all",) if full_repository else ()
    cleanup_scope = () if full_repository else ("--changes",)
    fallow_scope = (
        ("--fail-on-issues",) if full_repository else ("audit", "--base", base)
    )
    return (
        Stage(
            name="aislop",
            command=(str(NODE_BIN / "aislop"), "scan", *aislop_scope, ".", *aislop_flags),
            cwd=root,
            environment={"DO_NOT_TRACK": "1", "AISLOP_NO_HISTORY": "1"},
            parallel=True,
        ),
        Stage(
            name="anti-slop",
            command=("node", str(scripts / "run-anti-slop.ts"), *file_scope),
            cwd=root,
            parallel=True,
        ),
        Stage(
            name="ai-cleanup",
            command=("node", str(scripts / "ai-cleanup.ts"), "--check", *cleanup_scope),
            cwd=root,
            parallel=True,
        ),
        Stage(
            name="fallow",
            command=(
                str(NODE_BIN / "fallow"),
                *fallow_scope,
                "--root",
                ".",
                "--format",
                "json",
                "--quiet",
                "--output-file",
                str(fallow_report),
            ),
            cwd=root,
            environment={"FALLOW_CACHE_DIR": str(repository_cache / "fallow")},
            report_filename="fallow.json",
            report_source=fallow_report,
            parallel=True,
        ),
        Stage(
            name="slopo",
            command=(
                "python3",
                str(QUALITY_GATES / "slopo" / "run.py"),
                "--cache-dir",
                str(repository_cache / "slopo"),
                *file_scope,
            ),
            cwd=root,
            parallel=True,
        ),
    )


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_streamed(
    stage: Stage,
    raw_output_path: Path,
    on_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> tuple[int, bytes]:
    header = "\n".join(
        (
            f"===== {stage.name} =====",
            f"$ {shlex.join(stage.command)}",
            f"cwd={stage.cwd}",
            "--- combined stdout/stderr ---",
            "",
        )
    ).encode("utf-8")
    with raw_output_path.open("ab") as raw_output:
        raw_output.write(header)
        raw_output.flush()
        try:
            process = subprocess.Popen(
                stage.command,
                cwd=stage.cwd,
                env={**os.environ, **stage.environment},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as error:
            output = f"Could not start {stage.name}: {error}\n".encode("utf-8")
            raw_output.write(output)
            raw_output.write(b"===== end exit=127 =====\n")
            raw_output.flush()
            return 127, output

        if on_start is not None:
            on_start(process)

        chunks: list[bytes] = []
        try:
            if process.stdout is None:
                raise RuntimeError(f"Could not capture output for {stage.name}")
            with process.stdout:
                while chunk := process.stdout.read1(64 * 1024):
                    chunks.append(chunk)
                    raw_output.write(chunk)
                    raw_output.flush()
            exit_code = process.wait()
        except KeyboardInterrupt:
            raw_output.write(b"\n===== interrupted =====\n")
            raw_output.flush()
            stop_process(process)
            raise

        if chunks and not chunks[-1].endswith(b"\n"):
            raw_output.write(b"\n")
        raw_output.write(f"===== end exit={exit_code} =====\n".encode("utf-8"))
        raw_output.flush()
    return exit_code, b"".join(chunks)


def run_stage(
    stage: Stage,
    run_directory: Path,
    raw_output_path: Path | None = None,
    on_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> StageResult:
    stage.validate()
    started = time.monotonic()
    exit_code, output_bytes = run_streamed(
        stage, raw_output_path or run_directory / "raw-output.log", on_start
    )
    elapsed_seconds = time.monotonic() - started
    stdout = output_bytes.decode("utf-8", errors="replace")
    log_path = run_directory / f"{stage.name}.log"
    log_path.write_text(
        "\n".join(
            (
                f"$ {shlex.join(stage.command)}",
                f"exit_code={exit_code}",
                "",
                "--- combined stdout/stderr ---",
                stdout.rstrip(),
                "",
            )
        ),
        encoding="utf-8",
    )
    report_path = None
    if stage.report_filename is not None:
        report_path = run_directory / stage.report_filename
    if stage.report_source is not None and stage.report_source.is_file():
        shutil.move(stage.report_source, report_path)
    return StageResult(
        stage=stage,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        elapsed_seconds=elapsed_seconds,
        log_path=log_path,
        report_path=report_path,
    )


def run_pipeline(stages: Sequence[Stage], run_directory: Path, root: Path) -> int:
    results: list[StageResult] = []
    index = 0
    while index < len(stages):
        stage = stages[index]
        if not stage.parallel:
            print(f"Running {stage.name} ({index + 1}/{len(stages)})...", flush=True)
            results.append(run_stage(stage, run_directory))
            index += 1
            continue

        group_start = index
        while index < len(stages) and stages[index].parallel:
            index += 1
        group = stages[group_start:index]
        raw_paths = [
            run_directory / f".raw-{position}-{item.name}.log"
            for position, item in enumerate(group, start=group_start + 1)
        ]
        active_processes: list[subprocess.Popen[bytes]] = []
        process_lock = Lock()
        cancelled = Event()

        def register_process(process: subprocess.Popen[bytes]) -> None:
            with process_lock:
                active_processes.append(process)
            if cancelled.is_set():
                stop_process(process)

        with ThreadPoolExecutor(max_workers=len(group)) as executor:
            futures = []
            try:
                for position, (item, raw_path) in enumerate(
                    zip(group, raw_paths), start=group_start + 1
                ):
                    print(f"Running {item.name} ({position}/{len(stages)})...", flush=True)
                    futures.append(
                        executor.submit(
                            run_stage, item, run_directory, raw_path, register_process
                        )
                    )
                group_results = [future.result() for future in futures]
            except BaseException:
                cancelled.set()
                with process_lock:
                    processes = tuple(active_processes)
                for process in processes:
                    stop_process(process)
                raise
        with (run_directory / "raw-output.log").open("ab") as raw_output:
            for raw_path in raw_paths:
                with raw_path.open("rb") as stage_output:
                    shutil.copyfileobj(stage_output, raw_output)
                raw_path.unlink()
        results.extend(group_results)
    render_results(results, run_directory, root)
    return 0 if all(result.passed for result in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the agent quality gates.")
    parser.add_argument("--all", action="store_true", dest="full_repository")
    arguments = parser.parse_args()
    root = repository_root()
    repository_cache = repository_cache_directory()
    run_directory = create_run_directory(repository_cache / "reports")
    try:
        base = (
            None
            if arguments.full_repository
            else os.environ.get("QUALITY_GATES_BASE", "HEAD")
        )
        exit_code = run_pipeline(
            build_stages(root, run_directory, repository_cache, base),
            run_directory,
            root,
        )
    except KeyboardInterrupt:
        partial_logs = relative_path(run_directory, root)
        print(f"\nAISLOP interrupted. Partial logs: {partial_logs}/")
        exit_code = 130
    try:
        finish_run_directory(run_directory)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"ERROR: Could not enforce aislop report retention: {error}", file=sys.stderr)
        return exit_code if exit_code != 0 else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
