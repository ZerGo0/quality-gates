#!/usr/bin/env python3
"""Run the repository SlopO changed-files gate."""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
from contextlib import chdir
from pathlib import Path
from types import FrameType
from typing import NoReturn, Sequence

from runner.analysis import (
    count_unembedded,
    embed,
    index,
    load_config,
    refresh_report,
    validate_current_config,
)
from runner.baseline_worktree import prepare_base_worktree
from runner.cache import CacheTarget, cache_status, remove_incompatible_database
from runner.embedding_services import EmbeddingServices
from runner.errors import RunError, SignalExit, SkipRun
from runner.paths import Paths
from runner.environment import bootstrap, configure_environment, validate_setup


CURRENT_CACHE_KEY = "current-analysis-v1"


def _run_current(paths: Paths, services: EmbeddingServices) -> None:
    from slopo.result.analysis.ignore import ensure_ignore_file

    paths.report_dir.mkdir(parents=True, exist_ok=True)
    remove_incompatible_database(paths.database)
    config = load_config(
        paths, paths.repository_root, paths.database, paths.report_dir
    )
    validate_current_config(config)
    with chdir(paths.repository_root):
        missing_count = index(config)
        embed(config, missing_count, services)
        config.ignore_file.parent.mkdir(parents=True, exist_ok=True)
        ensure_ignore_file(config.ignore_file)

        target = CacheTarget(
            state_file=paths.analysis_state,
            cache_key=CURRENT_CACHE_KEY,
            label="SlopO analysis",
            input_files=(
                paths.config,
                paths.dependency_lock,
                paths.setup_file,
                paths.ignore_file,
                paths.database,
            ),
            database=paths.database,
            report_dir=paths.report_dir,
        )
        status = cache_status("check", target)
        if status == 1:
            refresh_report(config, paths.report_dir)
            status = cache_status("record", target)
        if status != 0:
            raise RunError("The SlopO analysis cache could not be updated.")


def _run_baseline(
    paths: Paths, snapshot_commit: str, services: EmbeddingServices
) -> None:
    from seed_embeddings import seed_embeddings

    prepare_base_worktree(paths, snapshot_commit)
    paths.base_report_dir.mkdir(parents=True, exist_ok=True)
    remove_incompatible_database(paths.base_database)
    target = CacheTarget(
        state_file=paths.base_state,
        cache_key=snapshot_commit,
        label="SlopO baseline",
        input_files=(
            paths.config,
            paths.dependency_lock,
            paths.setup_file,
            paths.ignore_file,
        ),
        database=paths.base_database,
        report_dir=paths.base_report_dir,
    )
    status = cache_status("check", target)
    if status == 0:
        return
    if status != 1:
        raise RunError("The SlopO baseline cache could not be read.")

    config = load_config(
        paths, paths.base_worktree, paths.base_database, paths.base_report_dir
    )
    with chdir(paths.base_worktree):
        index(config)
        copied = seed_embeddings(paths.database, paths.base_database)
        print(f"Reused {copied} embedding{'s' if copied != 1 else ''}.")
        embed(config, count_unembedded(config), services)
        refresh_report(config, paths.base_report_dir)
    if cache_status("record", target) != 0:
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
    print(render_errors(matches), file=sys.stderr)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the repository SlopO changed-files gate."
    )
    parser.add_argument("--cache-dir", type=Path, required=True)
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
    print(render_clusters(reports), file=sys.stderr)
    return 1


def _run(paths: Paths, explicit_base: str | None, full_repository: bool) -> int:
    from resolve_git_base import resolve_git_base
    from run_lock import exclusive_lock

    llama = validate_setup(paths)
    configure_environment(paths)
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
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if not arguments.cache_dir.is_absolute():
        parser.error("--cache-dir must be an absolute path")
    paths = Paths.discover(_repository_root(), arguments.cache_dir)
    try:
        bootstrap(paths)
        sys.stdout.reconfigure(line_buffering=True)
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
