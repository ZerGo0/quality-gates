#!/usr/bin/env python3
"""Run the repository SlopO changed-files gate."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import chdir
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType
from typing import NoReturn, Sequence


SERVER_HOST = "127.0.0.1"
SERVER_PORT = 17997
ADAPTER_PORT = 17998
HEALTH_POLL_INTERVAL = 0.05
SERVER_HEALTH_TIMEOUT = 120.0
ADAPTER_HEALTH_TIMEOUT = 30.0
MODEL_ALIAS = "jina-code-embeddings-0.5b"
MODEL_REPOSITORY = "jinaai/jina-code-embeddings-0.5b-GGUF"
MODEL_FILE = "jina-code-embeddings-0.5b-Q8_0.gguf"
CURRENT_CACHE_KEY = "current-analysis-v1"
REPOSITORY_CONFIG_DIR = ".quality-gates"


class RunError(RuntimeError):
    """Raised when the repository SlopO gate cannot run safely."""


class SkipRun(RuntimeError):
    """Raised when the opt-in local SlopO runtime is unavailable."""


class SignalExit(BaseException):
    """Raised after a termination signal so services can be cleaned up."""

    def __init__(self, signum: int) -> None:
        self.signum = signum


@dataclass(frozen=True)
class Paths:
    repository_root: Path
    slopo_project: Path
    runtime_dir: Path
    model_cache: Path
    setup_file: Path
    server_log: Path
    adapter_log: Path
    venv_python: Path
    adapter_script: Path
    config: Path
    ignore_file: Path
    dependency_lock: Path
    repository_cache: Path
    database: Path
    report_dir: Path
    analysis_state: Path
    base_worktree: Path
    base_database: Path
    base_report_dir: Path
    base_state: Path
    run_lock: Path

    @classmethod
    def discover(cls, repository_root: Path) -> "Paths":
        slopo_project = Path(__file__).resolve().parent
        cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        runtime_dir = cache_home / "quality-gates" / "llama.cpp"
        repository_config_dir = repository_root / REPOSITORY_CONFIG_DIR
        repository_config = repository_config_dir / "slopo.conf.yaml"
        if not repository_config.is_file():
            repository_config = slopo_project / "profiles" / "default.conf.yaml"
        repository_cache = repository_root / ".slopo" / "repository"
        base_worktree = repository_cache / "base-worktree"
        return cls(
            repository_root=repository_root,
            slopo_project=slopo_project,
            runtime_dir=runtime_dir,
            model_cache=runtime_dir / "cache",
            setup_file=runtime_dir / "setup.txt",
            server_log=runtime_dir / "run-server.log",
            adapter_log=runtime_dir / "run-adapter.log",
            venv_python=slopo_project / ".venv" / "bin" / "python",
            adapter_script=slopo_project / "local_jina_adapter.py",
            config=repository_config,
            ignore_file=repository_config_dir / "slopo.ignore.txt",
            dependency_lock=slopo_project / "uv.lock",
            repository_cache=repository_cache,
            database=repository_cache / "slopo.db",
            report_dir=repository_cache / "report",
            analysis_state=repository_cache / "analysis-cache-state.json",
            base_worktree=base_worktree,
            base_database=base_worktree / ".slopo" / "repository" / "slopo.db",
            base_report_dir=base_worktree / ".slopo" / "repository" / "report",
            base_state=base_worktree / ".slopo" / "repository" / "cache-state.json",
            run_lock=repository_cache / "run.lock",
        )


def _warn_and_skip(reason: str) -> NoReturn:
    raise SkipRun(f"WARN: Slopo skipped: {reason} Run quality-gates setup to opt in.")


def _bootstrap(paths: Paths) -> None:
    if not paths.config.is_file():
        raise RunError(f"No Slopo config found at {paths.config}.")
    if not os.access(paths.venv_python, os.X_OK):
        _warn_and_skip("the Slopo environment is absent.")

    expected_prefix = paths.venv_python.parent.parent.resolve()
    if Path(sys.prefix).resolve() != expected_prefix:
        os.execv(
            paths.venv_python,
            [str(paths.venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        )


def _resolve_llama() -> Path | None:
    executable = shutil.which("llama")
    if executable:
        return Path(executable)
    for candidate in (
        Path.home() / ".local" / "bin" / "llama",
        Path.home() / ".llama-app" / "llama",
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _validate_setup(paths: Paths) -> Path:
    llama = _resolve_llama()
    if llama is None:
        _warn_and_skip("llama.cpp is not installed.")
    if not paths.model_cache.is_dir() or not paths.setup_file.is_file():
        _warn_and_skip("the Jina model is absent.")
    if not paths.adapter_script.is_file():
        _warn_and_skip("the Jina adapter is absent.")
    if not paths.dependency_lock.is_file():
        raise RunError("The locked SlopO dependency file is absent.")

    setup_lines = set(paths.setup_file.read_text(encoding="utf-8").splitlines())
    required = (
        (
            f"model_repository={MODEL_REPOSITORY}",
            "the configured Jina model is not set up.",
        ),
        (
            f"model_file={MODEL_FILE}",
            "the configured Jina model quantization is not set up.",
        ),
        (
            "embedding_dimensions=256",
            "the configured embedding dimensions are not set up.",
        ),
        ("pooling=last", "the required last-token pooling is not set up."),
        ("task=code2code.query", "the recommended code-to-code task is not set up."),
        ("adapter=local-jina-api", "the local Jina adapter is not set up."),
        ("prompt_cache=disabled", "llama.cpp prompt caching is not disabled."),
    )
    for line, reason in required:
        if line not in setup_lines:
            _warn_and_skip(reason)
    return llama


def _configure_environment(paths: Paths) -> None:
    os.environ.update(
        {
            "LLAMA_CACHE": str(paths.model_cache),
            "HF_HUB_OFFLINE": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "DO_NOT_TRACK": "true",
            "SCARF_NO_ANALYTICS": "true",
        }
    )
    existing_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    no_proxy = "127.0.0.1,localhost"
    if existing_no_proxy:
        no_proxy = f"{no_proxy},{existing_no_proxy}"
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy


class EmbeddingServices:
    def __init__(self, paths: Paths, llama: Path) -> None:
        self.paths = paths
        self.llama = llama
        self.server: subprocess.Popen[bytes] | None = None
        self.adapter: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self.server is not None or self.adapter is not None:
            return
        self.server = self._spawn(
            [
                str(self.llama),
                "serve",
                "--offline",
                "--embedding",
                "--hf-repo",
                MODEL_REPOSITORY,
                "--hf-file",
                MODEL_FILE,
                "--alias",
                MODEL_ALIAS,
                "--host",
                SERVER_HOST,
                "--port",
                str(SERVER_PORT),
                "--ctx-size",
                "8192",
                "--ubatch-size",
                "8192",
                "--pooling",
                "last",
                "--embd-normalize",
                "2",
                "--cache-ram",
                "0",
                "--no-webui",
            ],
            self.paths.server_log,
        )
        self._wait_until_ready(
            self.server,
            f"http://{SERVER_HOST}:{SERVER_PORT}/health",
            SERVER_HEALTH_TIMEOUT,
        )
        self.adapter = self._spawn(
            [
                sys.executable,
                str(self.paths.adapter_script),
                "--host",
                SERVER_HOST,
                "--port",
                str(ADAPTER_PORT),
                "--upstream-url",
                f"http://{SERVER_HOST}:{SERVER_PORT}/v1/embeddings",
            ],
            self.paths.adapter_log,
        )
        self._wait_until_ready(
            self.adapter,
            f"http://{SERVER_HOST}:{ADAPTER_PORT}/health",
            ADAPTER_HEALTH_TIMEOUT,
        )

    @staticmethod
    def _spawn(command: Sequence[str], log_path: Path) -> subprocess.Popen[bytes]:
        with log_path.open("wb") as log:
            return subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _wait_until_ready(
        self, process: subprocess.Popen[bytes], health_url: str, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._raise_startup_error()
            try:
                with urllib.request.urlopen(health_url, timeout=0.2) as response:
                    if 200 <= response.status < 300:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(HEALTH_POLL_INTERVAL)
        self._raise_startup_error()

    def _raise_startup_error(self) -> NoReturn:
        details = [
            "Slopo failed because the local embedding service did not become ready."
        ]
        for path in (self.paths.server_log, self.paths.adapter_log):
            if path.is_file():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                details.extend(lines[-80:])
        raise RunError("\n".join(details))

    def stop(self) -> None:
        for process in (self.adapter, self.server):
            if process is None or process.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        self.adapter = None
        self.server = None


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def remove_incompatible_database(database_file: Path) -> None:
    """Remove a cache database only when its SlopO schema is outdated."""
    from slopo.schema import SCHEMA_VERSION

    if not _path_exists(database_file):
        return
    metadata = database_file.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RunError(f"SlopO database is not a regular file: {database_file}")
    try:
        connection = sqlite3.connect(f"file:{database_file}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT version FROM schema_version").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise RunError(
            f"Could not validate SlopO database schema: {database_file}"
        ) from error
    if row == (SCHEMA_VERSION,):
        return

    print(f"Removing incompatible SlopO cache database: {database_file}")
    for path in (
        database_file,
        Path(f"{database_file}-wal"),
        Path(f"{database_file}-shm"),
    ):
        path.unlink(missing_ok=True)


def _load_config(paths: Paths, working_directory: Path):
    from slopo.config import load_config

    with chdir(working_directory):
        return load_config(paths.config)


def _validate_current_config(paths: Paths, config: object) -> None:
    expected_api_base = f"http://{SERVER_HOST}:{ADAPTER_PORT}/v1"
    actual_api_base = config.embedding_params.get("api_base")
    if actual_api_base != expected_api_base:
        raise RunError(
            "Slopo runtime must use the repository-local embedding adapter at "
            f"{expected_api_base}, got {actual_api_base!r}"
        )
    if config.db_file.resolve() != paths.database.resolve():
        raise RunError(
            "SlopO must use the repository cache database at "
            f"{paths.database.resolve()}, got {config.db_file.resolve()}"
        )
    if config.ignore_file.resolve() != paths.ignore_file.resolve():
        raise RunError(
            "SlopO must use the repository ignore file at "
            f"{paths.ignore_file.resolve()}, got {config.ignore_file.resolve()}"
        )
    if config.report_dir.resolve() != paths.report_dir.resolve():
        raise RunError(
            "SlopO changed-files gate must inspect the configured report directory at "
            f"{paths.report_dir.resolve()}, got {config.report_dir.resolve()}"
        )
    if not config.source_dir.is_dir():
        raise RunError(f"SlopO source directory is missing: {config.source_dir}")


def _index(config: object) -> int:
    from slopo.db import create_db, open_db
    from slopo.embedding.db import count_unembedded_units
    from slopo.indexing.command import run_index

    connection = open_db(config) if config.db_file.exists() else create_db(config)
    try:
        run_index(connection, config, print)
        return count_unembedded_units(connection)
    finally:
        connection.close()


def _count_unembedded(config: object) -> int:
    from slopo.db import open_db
    from slopo.embedding.db import count_unembedded_units

    connection = open_db(config)
    try:
        return count_unembedded_units(connection)
    finally:
        connection.close()


def _embed(config: object, missing_count: int, services: EmbeddingServices) -> None:
    from slopo.db import open_db
    from slopo.embedding.command import run_embed

    if missing_count == 0:
        print("No new code units to embed.")
        return
    services.start()
    connection = open_db(config)
    try:
        run_embed(connection, config, print)
    finally:
        connection.close()


def _analyze(config: object) -> None:
    from slopo.db import open_db
    from slopo.embedding.db import count_unembedded_units
    from slopo.result.analysis.command import run_analyze
    from slopo.result.report.filesystem import write_analyze_report

    connection = open_db(config)
    try:
        if count_unembedded_units(connection) > 0:
            raise RunError(
                "Some code units have no embeddings after embedding completed."
            )
        result = run_analyze(connection, config, print)
        if result is not None:
            write_analyze_report(result.clusters, result.units, config.report_dir)
            print(f"Report written to {config.report_dir} directory.")
    finally:
        connection.close()


def _cache_status(
    action: str,
    *,
    state_file: Path,
    cache_key: str,
    label: str,
    input_files: Sequence[Path],
    database: Path,
    report_dir: Path,
) -> int:
    from baseline_cache import main as cache_main

    arguments = [
        action,
        "--state-file",
        str(state_file),
        "--cache-key",
        cache_key,
        "--label",
        label,
    ]
    for input_file in input_files:
        arguments.extend(("--input-file", str(input_file)))
    arguments.extend(("--db-file", str(database), "--report-dir", str(report_dir)))
    return cache_main(arguments)


def _clear_reports(report_dir: Path) -> None:
    from changed_files_gate import clear_generated_reports

    clear_generated_reports(report_dir)


def _run_git(repository: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *arguments],
            cwd=repository,
            stderr=subprocess.PIPE,
            text=True,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise RunError(
            f"git {' '.join(arguments)} failed: {error.stderr.strip()}"
        ) from error


def _prepare_base_worktree(paths: Paths, snapshot_commit: str) -> None:
    """Prepare a clean baseline, discarding edits in this disposable cache."""
    if _path_exists(paths.base_worktree):
        metadata = paths.base_worktree.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RunError(
                "The SlopO baseline worktree path is not a regular directory: "
                f"{paths.base_worktree}"
            )
        actual_root = Path(
            _run_git(paths.base_worktree, "rev-parse", "--show-toplevel")
        ).resolve()
        if actual_root != paths.base_worktree.resolve():
            raise RunError(
                f"The SlopO baseline cache is not a Git worktree: {paths.base_worktree}"
            )
        if _run_git(
            paths.base_worktree, "status", "--porcelain", "--untracked-files=all"
        ):
            _run_git(
                paths.repository_root,
                "worktree",
                "remove",
                "--force",
                str(paths.base_worktree),
            )
            print(
                f"Discarded dirty SlopO baseline at {paths.base_worktree}. "
                "Creating a clean baseline.",
                file=sys.stderr,
            )
    if not _path_exists(paths.base_worktree):
        _run_git(
            paths.repository_root,
            "worktree",
            "add",
            "--force",
            "--quiet",
            "--detach",
            str(paths.base_worktree),
            snapshot_commit,
        )

    cached_commit = _run_git(
        paths.base_worktree, "rev-parse", "--verify", "HEAD^{commit}"
    )
    if cached_commit != snapshot_commit:
        _run_git(
            paths.base_worktree,
            "checkout",
            "--quiet",
            "--detach",
            snapshot_commit,
        )


def _refresh_report(config: object, report_dir: Path) -> None:
    _clear_reports(report_dir)
    _analyze(config)


def _run_current(paths: Paths, services: EmbeddingServices) -> None:
    from slopo.result.analysis.ignore import ensure_ignore_file

    paths.report_dir.mkdir(parents=True, exist_ok=True)
    remove_incompatible_database(paths.database)
    config = _load_config(paths, paths.repository_root)
    _validate_current_config(paths, config)
    with chdir(paths.repository_root):
        missing_count = _index(config)
        _embed(config, missing_count, services)
        config.ignore_file.parent.mkdir(parents=True, exist_ok=True)
        ensure_ignore_file(config.ignore_file)

        cache_inputs = (
            paths.config,
            paths.dependency_lock,
            paths.setup_file,
            paths.ignore_file,
            paths.database,
        )
        cache_status = _cache_status(
            "check",
            state_file=paths.analysis_state,
            cache_key=CURRENT_CACHE_KEY,
            label="SlopO analysis",
            input_files=cache_inputs,
            database=paths.database,
            report_dir=paths.report_dir,
        )
        if cache_status == 1:
            _refresh_report(config, paths.report_dir)
            cache_status = _cache_status(
                "record",
                state_file=paths.analysis_state,
                cache_key=CURRENT_CACHE_KEY,
                label="SlopO analysis",
                input_files=cache_inputs,
                database=paths.database,
                report_dir=paths.report_dir,
            )
        if cache_status != 0:
            raise RunError("The SlopO analysis cache could not be updated.")


def _run_baseline(
    paths: Paths, snapshot_commit: str, services: EmbeddingServices
) -> None:
    from seed_embeddings import seed_embeddings

    _prepare_base_worktree(paths, snapshot_commit)
    paths.base_report_dir.mkdir(parents=True, exist_ok=True)
    remove_incompatible_database(paths.base_database)
    cache_inputs = (
        paths.config,
        paths.dependency_lock,
        paths.setup_file,
        paths.ignore_file,
    )
    cache_status = _cache_status(
        "check",
        state_file=paths.base_state,
        cache_key=snapshot_commit,
        label="SlopO baseline",
        input_files=cache_inputs,
        database=paths.base_database,
        report_dir=paths.base_report_dir,
    )
    if cache_status == 0:
        return
    if cache_status != 1:
        raise RunError("The SlopO baseline cache could not be read.")

    config = replace(
        _load_config(paths, paths.base_worktree),
        ignore_file=paths.ignore_file.resolve(),
    )
    with chdir(paths.base_worktree):
        _index(config)
        copied = seed_embeddings(paths.database, paths.base_database)
        print(f"Reused {copied} embedding{'s' if copied != 1 else ''}.")
        _embed(config, _count_unembedded(config), services)
        _refresh_report(config, paths.base_report_dir)
    cache_status = _cache_status(
        "record",
        state_file=paths.base_state,
        cache_key=snapshot_commit,
        label="SlopO baseline",
        input_files=cache_inputs,
        database=paths.base_database,
        report_dir=paths.base_report_dir,
    )
    if cache_status != 0:
        raise RunError("The SlopO baseline cache could not be updated.")


def _run_changed_files_gate(paths: Paths, comparison_commit: str) -> int:
    from changed_files_gate import (
        collect_changed_files,
        find_new_clusters,
        load_cluster_reports,
        render_errors,
    )

    changed_files = collect_changed_files(paths.repository_root, comparison_commit)
    matches = find_new_clusters(
        load_cluster_reports(paths.report_dir),
        load_cluster_reports(paths.base_report_dir),
        changed_files,
    )
    if not matches:
        print("SlopO found no new duplicate-code errors.")
        return 0
    print(render_errors(matches, paths.repository_root), file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the repository SlopO changed-files gate."
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--base", "--changed-since", dest="explicit_base")
    scope.add_argument("--all", action="store_true", dest="full_repository")
    return parser


def _signal_handler(signum: int, _frame: FrameType | None) -> NoReturn:
    raise SignalExit(signum)


def _run_full_repository_gate(paths: Paths) -> int:
    from changed_files_gate import load_cluster_reports, render_clusters

    reports = load_cluster_reports(paths.report_dir)
    if not reports:
        print("SlopO found no duplicate-code clusters.")
        return 0
    print(render_clusters(reports, paths.repository_root), file=sys.stderr)
    return 1


def _run(paths: Paths, explicit_base: str | None, full_repository: bool) -> int:
    from resolve_git_base import resolve_git_base
    from run_lock import exclusive_lock

    llama = _validate_setup(paths)
    _configure_environment(paths)
    paths.repository_cache.mkdir(parents=True, exist_ok=True)
    services = EmbeddingServices(paths, llama)
    with exclusive_lock(paths.run_lock):
        resolved = None
        if not full_repository:
            resolved = resolve_git_base(paths.repository_root, explicit_base)
            print(resolved.description, file=sys.stderr)
        try:
            _run_current(paths, services)
            if resolved is None:
                return _run_full_repository_gate(paths)
            _run_baseline(paths, resolved.snapshot_commit, services)
            return _run_changed_files_gate(paths, resolved.comparison_commit)
        finally:
            services.stop()


def _repository_root() -> Path:
    toplevel = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True)
    return Path(toplevel.strip())


def main(argv: Sequence[str] | None = None) -> int:
    paths = Paths.discover(_repository_root())
    try:
        _bootstrap(paths)
        sys.stdout.reconfigure(line_buffering=True)
        arguments = _build_parser().parse_args(argv)
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        return _run(paths, arguments.explicit_base, arguments.full_repository)
    except SkipRun as error:
        print(error, file=sys.stderr)
        return 0
    except SignalExit as error:
        return 128 + error.signum
    except Exception as error:
        try:
            from slopo.errors import KNOWN_ERRORS, describe

            message = (
                describe(error).human
                if isinstance(error, KNOWN_ERRORS)
                else str(error)
            )
        except ImportError:
            message = str(error)
        print(f"SlopO failed: {message or type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
