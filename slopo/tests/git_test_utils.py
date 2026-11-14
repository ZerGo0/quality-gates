from pathlib import Path

from resolve_git_base import run_git_command


def run_git(repository_root: Path, *arguments: str) -> str:
    result = run_git_command(repository_root, *arguments)
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()
