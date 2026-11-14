#!/usr/bin/env python3
"""Run the agent quality gates with compact, model-friendly output."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from threading import Event, Lock
from typing import Callable, Iterator, Sequence

from aislop_output import Stage, StageResult, relative_path, render_results

MAX_RETAINED_RUNS = 20
ACTIVE_MARKER = ".active"
RUN_DIRECTORY_NAME = re.compile(
    r"^(?P<timestamp>\d{8}T\d{6}Z)-(?P<pid>\d+)-[A-Za-z0-9_-]+$"
)


QUALITY_GATES = Path(__file__).resolve().parents[1]
NODE_BIN = QUALITY_GATES / "node_modules" / ".bin"


def repository_root() -> Path:
    toplevel = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    )
    return Path(toplevel.strip())


def validate_artifact_root(root: Path) -> Path:
    artifact_root = root / ".task" / "aislop"
    artifact_root.mkdir(parents=True, exist_ok=True)
    if artifact_root.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked report directory: {artifact_root}")
    try:
        artifact_root.resolve().relative_to(root.resolve())
    except ValueError as error:
        raise RuntimeError(
            f"Aislop report directory must remain inside {root}: {artifact_root}"
        ) from error
    return artifact_root


@contextmanager
def retention_lock(artifact_root: Path) -> Iterator[None]:
    lock_path = artifact_root / ".retention.lock"
    with lock_path.open("a+b") as lock_file:
        flock(lock_file.fileno(), LOCK_EX)
        try:
            yield
        finally:
            flock(lock_file.fileno(), LOCK_UN)


def create_run_directory(root: Path) -> Path:
    artifact_root = validate_artifact_root(root)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with retention_lock(artifact_root):
        run_directory = Path(
            tempfile.mkdtemp(prefix=f"{timestamp}-{os.getpid()}-", dir=artifact_root)
        )
        mark_run_active(run_directory)
        prune_run_directories(artifact_root)
    return run_directory


def process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_is_active(run_directory: Path) -> bool:
    marker = run_directory / ACTIVE_MARKER
    if marker.is_symlink():
        return True
    if not marker.exists():
        return False
    if not marker.is_file():
        return True
    try:
        process_id = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return process_is_alive(process_id)


def mark_run_active(run_directory: Path) -> None:
    (run_directory / ACTIVE_MARKER).write_text(f"{os.getpid()}\n", encoding="utf-8")


def clear_active_marker(run_directory: Path) -> None:
    marker = run_directory / ACTIVE_MARKER
    if marker.is_symlink():
        raise RuntimeError(f"Refusing to remove symlinked active marker: {marker}")
    try:
        marker_process_id = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Could not validate aislop active marker: {marker}") from error
    if marker_process_id != os.getpid():
        raise RuntimeError(f"Refusing to remove another process's marker: {marker}")
    marker.unlink(missing_ok=True)


def is_generated_run_name(name: str) -> bool:
    match = RUN_DIRECTORY_NAME.fullmatch(name)
    if match is None or int(match.group("pid")) <= 0:
        return False
    try:
        datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    return True


def generated_run_directories(artifact_root: Path) -> list[Path]:
    directories: list[Path] = []
    with os.scandir(artifact_root) as entries:
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            if is_generated_run_name(entry.name):
                directories.append(Path(entry.path))
    return directories


def remove_generated_run(run_directory: Path, artifact_root: Path) -> None:
    resolved_root = artifact_root.resolve()
    resolved_run = run_directory.resolve()
    if run_directory.is_symlink() or resolved_run.parent != resolved_root:
        raise RuntimeError(f"Refusing to remove unsafe report directory: {run_directory}")
    if not is_generated_run_name(run_directory.name):
        raise RuntimeError(f"Refusing to remove unknown report directory: {run_directory}")
    shutil.rmtree(run_directory)


def prune_run_directories(
    artifact_root: Path, retain: int = MAX_RETAINED_RUNS
) -> None:
    if retain < 1:
        raise ValueError("Quality-gate report retention must be at least one run")
    generated = generated_run_directories(artifact_root)
    active = [directory for directory in generated if run_is_active(directory)]
    completed = sorted(
        (directory for directory in generated if directory not in active),
        key=lambda directory: directory.name,
        reverse=True,
    )
    for expired in completed[retain:]:
        remove_generated_run(expired, artifact_root)


def finish_run_directory(run_directory: Path) -> None:
    artifact_root = run_directory.parent
    with retention_lock(artifact_root):
        clear_active_marker(run_directory)
        prune_run_directories(artifact_root)


def build_stages(root: Path, run_directory: Path) -> tuple[Stage, ...]:
    scripts = QUALITY_GATES / "scripts"
    fallow_report = run_directory / "fallow.json"
    return (
        Stage(
            name="aislop",
            command=(str(NODE_BIN / "aislop"), "scan", "--changes", ".", "--json"),
            cwd=root,
            environment={"DO_NOT_TRACK": "1"},
            parallel=True,
        ),
        Stage(
            name="anti-slop",
            command=("node", str(scripts / "run-anti-slop.ts")),
            cwd=root,
            parallel=True,
        ),
        Stage(
            name="ai-cleanup",
            command=("node", str(scripts / "ai-cleanup.ts"), "--check", "--changes"),
            cwd=root,
            parallel=True,
        ),
        Stage(
            name="fallow",
            command=(
                str(NODE_BIN / "fallow"),
                "audit",
                "--root",
                ".",
                "--format",
                "json",
                "--quiet",
                "--output-file",
                str(fallow_report),
            ),
            cwd=root,
            report_filename="fallow.json",
            parallel=True,
        ),
        Stage(
            name="slopo",
            command=("python3", str(QUALITY_GATES / "slopo" / "run.py")),
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
    root = repository_root()
    run_directory = create_run_directory(root)
    try:
        exit_code = run_pipeline(build_stages(root, run_directory), run_directory, root)
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
