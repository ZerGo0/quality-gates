from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_CONFIG_DIR = ".quality-gates"


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
        slopo_project = Path(__file__).resolve().parents[1]
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
