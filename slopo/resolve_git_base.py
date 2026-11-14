#!/usr/bin/env python3
"""Resolve the Git snapshot and comparison base for a SlopO audit."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class GitBaseResolutionError(RuntimeError):
    """Raised when the SlopO comparison base cannot be resolved."""


@dataclass(frozen=True)
class ResolvedGitBase:
    snapshot_commit: str
    comparison_commit: str
    description: str


def resolve_git_base(
    repository_root: Path, explicit_base: str | None = None
) -> ResolvedGitBase:
    """Compare main with HEAD and other branches with the default branch."""
    root = _validate_repository_root(repository_root)
    if explicit_base is not None:
        return _resolve_named_base(root, explicit_base, f"base {explicit_base}")

    override = os.environ.get("SLOPO_AUDIT_BASE")
    if override:
        return _resolve_named_base(
            root, override, f"SLOPO_AUDIT_BASE={override}"
        )

    if _current_branch(root) in {"main", "master"}:
        return _resolve_named_base(root, "HEAD", "HEAD on main (uncommitted changes)")

    chosen_base: str | None = None
    chosen_reference: str | None = None
    for reference in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        base = _resolve_automatic_base(root, reference)
        if base is not None and (
            chosen_base is None
            or (base != chosen_base and _is_ancestor(root, chosen_base, base))
        ):
            chosen_base = base
            chosen_reference = reference
    if chosen_base is not None:
        description = (
            f"local {chosen_reference}"
            if chosen_reference in {"main", "master"}
            else f"merge-base with {chosen_reference}"
        )
        return ResolvedGitBase(
            chosen_base, chosen_base, description
        )

    raise GitBaseResolutionError(
        "Could not resolve a comparison base from origin or local main/master"
    )


def _resolve_named_base(
    repository_root: Path, reference: str, description: str
) -> ResolvedGitBase:
    snapshot_commit = _resolve_commit(repository_root, reference)
    if snapshot_commit is None:
        raise GitBaseResolutionError(f"Base does not name a commit: {reference}")
    comparison_commit = _require_merge_base(repository_root, reference)
    return ResolvedGitBase(snapshot_commit, comparison_commit, description)


def _validate_repository_root(repository_root: Path) -> Path:
    root = repository_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Repository root is not a directory: {root}")

    actual_root = run_git_command(root, "rev-parse", "--show-toplevel")
    if actual_root.returncode != 0:
        detail = actual_root.stderr.strip()
        raise ValueError(f"Repository root is not a git worktree: {root}: {detail}")
    worktree_root = Path(actual_root.stdout.strip()).resolve()
    if worktree_root != root:
        raise ValueError(f"Expected the git worktree root, got: {root}")
    return root


def _current_branch(repository_root: Path) -> str | None:
    result = run_git_command(
        repository_root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _resolve_automatic_base(repository_root: Path, reference: str) -> str | None:
    if _resolve_commit(repository_root, reference) is None:
        return None
    return _require_merge_base(repository_root, reference)


def _is_ancestor(repository_root: Path, ancestor: str, descendant: str) -> bool:
    result = run_git_command(
        repository_root, "merge-base", "--is-ancestor", ancestor, descendant
    )
    if result.returncode not in (0, 1):
        raise GitBaseResolutionError(
            f"Could not compare Git bases {ancestor} and {descendant}: {result.stderr.strip()}"
        )
    return result.returncode == 0


def _require_merge_base(repository_root: Path, reference: str) -> str:
    result = run_git_command(repository_root, "merge-base", "HEAD", reference)
    if result.returncode != 0:
        raise GitBaseResolutionError(
            f"Could not find a merge-base with {reference}. Fetch full history or choose another base."
        )
    base = result.stdout.strip()
    if not base:
        raise GitBaseResolutionError(f"Git returned no merge-base with {reference}")
    return base


def _resolve_commit(repository_root: Path, reference: str) -> str | None:
    result = run_git_command(
        repository_root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{reference}^{{commit}}",
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def run_git_command(
    repository_root: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve the Git snapshot and comparison base for SlopO."
    )
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--base", "--changed-since", dest="explicit_base")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        resolved = resolve_git_base(args.repository_root, args.explicit_base)
    except (GitBaseResolutionError, OSError, ValueError) as error:
        print(f"SlopO could not choose a Git base: {error}", file=sys.stderr)
        return 2

    print(f"{resolved.snapshot_commit}\t{resolved.comparison_commit}")
    print(resolved.description, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
