#!/usr/bin/env python3
"""Keep a small, safe cache marker for a SlopO report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Sequence


_FORMAT_VERSION = 3
_FINGERPRINT_VERSION = b"slopo-report-cache-fingerprint-v1"
_REPORT_FINGERPRINT_VERSION = b"slopo-baseline-report-fingerprint-v1"
_CLUSTER_REPORT_PATTERN = re.compile(r"cluster-\d+\.md")


class CacheStateError(ValueError):
    """Raised when a cache path or state cannot be safely used."""


def fingerprint(cache_key: str, input_files: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    _add_field(digest, b"format", _FINGERPRINT_VERSION)
    _add_field(digest, b"cache-key", cache_key.encode("utf-8"))
    for path in sorted(input_files, key=lambda candidate: os.fsencode(candidate)):
        _validate_regular_file(path, "Input file")
        _add_field(digest, b"input-path", os.fsencode(path))
        _add_field(digest, b"input-content", path.read_bytes())
    return digest.hexdigest()


def _add_field(digest: hashlib._Hash, tag: bytes, value: bytes) -> None:
    digest.update(len(tag).to_bytes(8, "big"))
    digest.update(tag)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _validate_regular_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise CacheStateError(f"{label} is missing: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CacheStateError(f"{label} is not a regular file: {path}")


def _validate_report_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise CacheStateError(f"Report directory is missing: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CacheStateError(f"Report directory is not a regular directory: {path}")


def _validate_state_parent(state_file: Path) -> None:
    parent = state_file.parent
    try:
        metadata = parent.lstat()
    except FileNotFoundError as error:
        raise CacheStateError(f"State directory is missing: {parent}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CacheStateError(f"State directory is not a regular directory: {parent}")


def _load_state(state_file: Path) -> tuple[str, str] | None:
    try:
        metadata = state_file.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CacheStateError(f"State file is not a regular file: {state_file}")
    try:
        parsed = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CacheStateError(f"State file is not valid JSON: {state_file}") from error
    if (
        isinstance(parsed, dict)
        and type(parsed.get("format_version")) is int
        and parsed["format_version"] != _FORMAT_VERSION
    ):
        return None
    if (
        not isinstance(parsed, dict)
        or set(parsed)
        != {"format_version", "fingerprint", "report_fingerprint"}
        or type(parsed["format_version"]) is not int
        or parsed["format_version"] != _FORMAT_VERSION
        or not _is_sha256(parsed["fingerprint"])
        or not _is_sha256(parsed["report_fingerprint"])
    ):
        raise CacheStateError(f"State file has an invalid format: {state_file}")
    return parsed["fingerprint"], parsed["report_fingerprint"]


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _report_fingerprint(report_dir: Path) -> str:
    _validate_report_directory(report_dir)
    digest = hashlib.sha256()
    _add_field(digest, b"format", _REPORT_FINGERPRINT_VERSION)
    reports = sorted(
        path
        for path in report_dir.iterdir()
        if path.name == "index.md" or _CLUSTER_REPORT_PATTERN.fullmatch(path.name)
    )
    for path in reports:
        _validate_regular_file(path, "Report file")
        _add_field(digest, b"report-name", os.fsencode(path.name))
        _add_field(digest, b"report-content", path.read_bytes())
    return digest.hexdigest()


def _artifacts_ready(db_file: Path, report_dir: Path) -> bool:
    if not _path_exists(db_file) or not _path_exists(report_dir):
        return False
    _validate_regular_file(db_file, "Database file")
    _validate_report_directory(report_dir)
    return True


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _record_state(
    state_file: Path, cache_fingerprint: str, report_fingerprint: str
) -> None:
    body = json.dumps(
        {
            "format_version": _FORMAT_VERSION,
            "fingerprint": cache_fingerprint,
            "report_fingerprint": report_fingerprint,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{state_file.name}.", suffix=".tmp", dir=state_file.parent
    )
    temporary_file = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_file, state_file)
    except BaseException:
        temporary_file.unlink(missing_ok=True)
        raise


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check or save a SlopO report cache.")
    parser.add_argument("action", choices=("check", "record"))
    parser.add_argument("--state-file", required=True, type=Path)
    parser.add_argument("--cache-key", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--input-file", required=True, action="append", type=Path)
    parser.add_argument("--db-file", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        _validate_state_parent(args.state_file)
        cache_fingerprint = fingerprint(args.cache_key, args.input_file)
        if args.action == "record":
            _validate_regular_file(args.db_file, "Database file")
            report_fingerprint = _report_fingerprint(args.report_dir)
            _record_state(args.state_file, cache_fingerprint, report_fingerprint)
            print(f"{args.label} cache saved.")
            return 0

        saved_state = _load_state(args.state_file)
        if saved_state is None or saved_state[0] != cache_fingerprint:
            print(f"{args.label} cache miss.")
            return 1
        if not _artifacts_ready(args.db_file, args.report_dir):
            print(f"{args.label} cache miss.")
            return 1
        if saved_state[1] != _report_fingerprint(args.report_dir):
            print(f"{args.label} cache miss.")
            return 1
        print(f"{args.label} cache hit.")
        return 0
    except CacheStateError as error:
        print(f"{args.label} cache error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
