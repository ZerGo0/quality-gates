#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


_CLUSTER_FILE_PATTERN = re.compile(r"cluster-\d+\.md")
_HEADER_PATTERN = re.compile(r"^## \(\d+\) score .+$")
_HASH_PATTERN = re.compile(r"^Hash: `([0-9a-f]{12})`$")
_GROUP_PATTERN = re.compile(r"^### ______ \d+ ______$", re.MULTILINE)
_LOCATION_PATTERN = re.compile(r"^- `(.+)` lines (\d+)-(\d+)$")
_CODE_BLOCK_PATTERN = re.compile(r"^```[^\n]*\n(.*?)^```$", re.MULTILINE | re.DOTALL)


class ReportFormatError(ValueError):
    """Raised when SlopO emits a report this gate cannot safely interpret."""


@dataclass(frozen=True)
class Location:
    path: str
    start_line: int
    end_line: int
    body_hash: str


@dataclass(frozen=True)
class ClusterReport:
    path: Path
    cluster_hash: str
    locations: tuple[Location, ...]


def clear_generated_reports(report_dir: Path) -> None:
    if not report_dir.exists():
        return
    _validate_report_directory(report_dir)

    for path in report_dir.iterdir():
        if path.name == "index.md" or _CLUSTER_FILE_PATTERN.fullmatch(path.name):
            if not path.is_symlink() and not path.is_file():
                raise ValueError(f"Generated SlopO report is not a file: {path}")
            path.unlink()


def load_cluster_reports(report_dir: Path) -> list[ClusterReport]:
    if not report_dir.exists():
        return []
    _validate_report_directory(report_dir)

    paths = sorted(
        path
        for path in report_dir.iterdir()
        if _CLUSTER_FILE_PATTERN.fullmatch(path.name)
    )
    return [parse_cluster_report(path) for path in paths]


def parse_cluster_report(path: Path) -> ClusterReport:
    if path.is_symlink() or not path.is_file():
        raise ReportFormatError(f"SlopO cluster report is not a regular file: {path}")

    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or not _HEADER_PATTERN.fullmatch(lines[0]):
        raise ReportFormatError(f"Invalid SlopO cluster header in {path}")

    hashes = [
        match.group(1)
        for line in lines
        if (match := _HASH_PATTERN.fullmatch(line))
    ]
    if len(hashes) != 1:
        raise ReportFormatError(f"Expected exactly one cluster hash in {path}")

    locations: list[Location] = []
    sections = _GROUP_PATTERN.split(path.read_text(encoding="utf-8"))
    for section in sections[1:]:
        section_lines = section.splitlines()
        location_matches = [
            match
            for line in section_lines
            if (match := _LOCATION_PATTERN.fullmatch(line)) is not None
        ]
        if not location_matches:
            continue

        code_blocks = _CODE_BLOCK_PATTERN.findall(section)
        if not code_blocks:
            raise ReportFormatError(f"Missing code block in {path}")
        # SlopO v0.8 may emit context blocks before the shared code body. The
        # final fenced block in each group is always the compared body.
        body = code_blocks[-1]
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

        for match in location_matches:
            relative_path, start_text, end_text = match.groups()
            _validate_relative_report_path(relative_path, path)
            start_line = int(start_text)
            end_line = int(end_text)
            if start_line < 1 or end_line < start_line:
                raise ReportFormatError(
                    f"Invalid line range in {path}: {match.group(0)}"
                )
            locations.append(
                Location(relative_path, start_line, end_line, body_hash)
            )

    if len(locations) < 2:
        raise ReportFormatError(f"Expected at least two duplicate locations in {path}")

    return ClusterReport(path, hashes[0], tuple(locations))


def collect_changed_files(repository_root: Path, base_commit: str) -> set[str]:
    root = repository_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Repository root is not a directory: {root}")

    actual_root = _run_git_paths(root, "rev-parse", "--show-toplevel")
    if len(actual_root) != 1 or Path(actual_root[0]).resolve() != root:
        raise ValueError(f"Expected the git worktree root, got: {root}")

    resolved_base = _run_git_paths(
        root,
        "rev-parse",
        "--verify",
        f"{base_commit}^{{commit}}",
    )
    if len(resolved_base) != 1:
        raise ValueError(f"Expected one Git base commit, got: {base_commit}")

    changed = _run_git_paths(
        root,
        "diff",
        "--name-only",
        "--diff-filter=ACMRD",
        "-z",
        resolved_base[0],
        "--",
    )
    untracked = _run_git_paths(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    return {*changed, *untracked}


def find_new_clusters(
    current_reports: Sequence[ClusterReport],
    base_reports: Sequence[ClusterReport],
    changed_files: set[str],
) -> list[tuple[ClusterReport, tuple[Location, ...]]]:
    base_members = [_member_counts(report) for report in base_reports]
    matches: list[tuple[ClusterReport, tuple[Location, ...]]] = []
    for report in current_reports:
        changed_locations = tuple(
            location for location in report.locations if location.path in changed_files
        )
        current_members = _member_counts(report)
        inherited = any(
            all(count <= members[member] for member, count in current_members.items())
            for members in base_members
        )
        if changed_locations and not inherited:
            matches.append((report, changed_locations))
    return matches


def _member_counts(report: ClusterReport) -> Counter[tuple[str, str]]:
    return Counter((location.path, location.body_hash) for location in report.locations)


def render_errors(
    matches: Sequence[tuple[ClusterReport, tuple[Location, ...]]],
) -> str:
    lines = [
        f"SlopO found {len(matches)} new duplicate-code error"
        f"{'s' if len(matches) != 1 else ''}:"
    ]
    for report, changed_locations in matches:
        anchor = changed_locations[0]
        lines.append(
            f"ERROR: {anchor.path}:{anchor.start_line}: new duplicate cluster "
            f"{report.cluster_hash} includes changed code"
        )
        lines.extend(_cluster_detail_lines(report, set(changed_locations)))
    return "\n".join(lines)


def render_clusters(reports: Sequence[ClusterReport]) -> str:
    lines = [
        f"SlopO found {len(reports)} duplicate-code cluster"
        f"{'s' if len(reports) != 1 else ''}:"
    ]
    for report in reports:
        anchor = report.locations[0]
        lines.append(
            f"ERROR: {anchor.path}:{anchor.start_line}: duplicate cluster "
            f"{report.cluster_hash}"
        )
        lines.extend(_cluster_detail_lines(report, None))
    return "\n".join(lines)


def _cluster_detail_lines(
    report: ClusterReport,
    changed_locations: set[Location] | None,
) -> list[str]:
    lines: list[str] = []
    for location in report.locations:
        marker = ""
        if changed_locations is not None:
            marker = "[changed] " if location in changed_locations else "[existing] "
        lines.append(
            f"  - {marker}{location.path}:{location.start_line}-{location.end_line}"
        )
    lines.append(f"  Report: {report.path}")
    return lines


def _validate_report_directory(report_dir: Path) -> None:
    if report_dir.is_symlink() or not report_dir.is_dir():
        raise ValueError(f"SlopO report path is not a regular directory: {report_dir}")


def _validate_relative_report_path(relative_path: str, report_path: Path) -> None:
    parsed = PurePosixPath(relative_path)
    if parsed.is_absolute() or ".." in parsed.parts or relative_path in {"", "."}:
        raise ReportFormatError(
            f"Invalid repository-relative path in {report_path}: {relative_path!r}"
        )


def _run_git_paths(repository_root: Path, *arguments: str) -> list[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {error}")

    output = result.stdout
    if "-z" not in arguments:
        text = output.decode("utf-8").strip()
        return [] if not text else text.splitlines()
    if output and not output.endswith(b"\0"):
        raise RuntimeError(f"git {' '.join(arguments)} returned malformed output")
    return [part.decode("utf-8") for part in output.split(b"\0") if part]
