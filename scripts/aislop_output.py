"""Render compact output for the repository quality-gate runner."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

MAX_INLINE_FINDINGS = 20
MAX_INLINE_LINE_LENGTH = 500
ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)
LOCATION = re.compile(r"(?:^|\s)([^\s:]+\.[A-Za-z0-9]+):(\d+)(?::(\d+))?")
SAFE_STAGE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
AISLOP_FINDINGS_EXIT_CODE = 1


@dataclass(frozen=True)
class Stage:
    name: str
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str] = field(default_factory=dict)
    report_filename: str | None = None
    report_source: Path | None = None
    parallel: bool = False

    def validate(self) -> None:
        if SAFE_STAGE_NAME.fullmatch(self.name) is None:
            raise ValueError(f"Unsafe quality-gate stage name: {self.name!r}")


@dataclass(frozen=True)
class StageResult:
    stage: Stage
    exit_code: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    log_path: Path
    report_path: Path | None

    @property
    def passed(self) -> bool:
        if self.exit_code == 0:
            return True
        return (
            self.stage.name == "aislop"
            and "--changes" in self.stage.command
            and self.exit_code == AISLOP_FINDINGS_EXIT_CODE
            and only_existing_context_errors(self.stdout)
        )


@dataclass(frozen=True)
class StagePresentation:
    summary: str
    findings: tuple[str, ...] = ()
    skipped: bool = False


def strip_terminal_control(text: str) -> str:
    return ANSI_ESCAPE.sub("", text).replace("\r", "")


def load_json(text: str) -> Any | None:
    try:
        return json.loads(strip_terminal_control(text))
    except (json.JSONDecodeError, TypeError):
        return None


def only_existing_context_errors(text: str) -> bool:
    payload = load_json(text)
    if not isinstance(payload, dict) or "error" in payload:
        return False
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list):
        return False
    return all(
        isinstance(item, dict)
        and (
            item.get("severity") != "error"
            or item.get("changeContext") == "existing-file-context"
        )
        for item in diagnostics
    )


def display_path(path: object, root: Path) -> str:
    value = str(path or "unknown")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return value


def diagnostic_line(diagnostic: dict[str, Any], root: Path) -> str:
    path = display_path(
        diagnostic.get("filePath")
        or diagnostic.get("file")
        or diagnostic.get("path")
        or diagnostic.get("filename"),
        root,
    )
    line = diagnostic.get("line") or diagnostic.get("lineNumber")
    column = diagnostic.get("column") or diagnostic.get("columnNumber")
    location = path
    if isinstance(line, int):
        location += f":{line}"
        if isinstance(column, int):
            location += f":{column}"
    severity = str(
        diagnostic.get("severity") or diagnostic.get("level") or "finding"
    )
    rule = diagnostic.get("rule") or diagnostic.get("ruleId") or diagnostic.get("code")
    message = diagnostic.get("message") or diagnostic.get("description") or "finding"
    context = diagnostic.get("changeContext")
    suffix = f" ({context})" if isinstance(context, str) else ""
    rule_text = f" [{rule}]" if rule else ""
    return f"{location} {severity}{rule_text}: {message}{suffix}"


def present_aislop(result: StageResult, root: Path) -> StagePresentation:
    payload = load_json(result.stdout)
    if not isinstance(payload, dict):
        return present_text_stage(result)
    if isinstance(payload.get("error"), str):
        return StagePresentation(summary=payload["error"])

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    diagnostics = payload.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, list) else []
    contexts = {
        context: sum(
            1
            for item in diagnostics
            if isinstance(item, dict) and item.get("changeContext") == context
        )
        for context in ("changed-line", "existing-file-context", "unknown")
    }
    score = payload.get("score")
    score_text = "not scored" if score is None else f"{score}/100"
    description = (
        f"score {score_text}; {summary.get('errors', 0)} errors, "
        f"{summary.get('warnings', 0)} warnings; "
        f"{contexts['changed-line']} changed-line, "
        f"{contexts['existing-file-context']} existing-context, "
        f"{contexts['unknown']} unclassified"
    )
    ordered = sorted(
        (item for item in diagnostics if isinstance(item, dict)),
        key=lambda item: (
            item.get("changeContext") != "changed-line",
            item.get("severity") != "error",
        ),
    )
    return StagePresentation(
        summary=description,
        findings=tuple(diagnostic_line(item, root) for item in ordered),
    )


def relevant_text_lines(text: str) -> list[str]:
    cleaned = strip_terminal_control(text)
    lines = [" ".join(line.split()) for line in cleaned.splitlines() if line.strip()]
    signals: list[str] = []
    for line in lines:
        lower = line.lower()
        if (
            LOCATION.search(line)
            or lower.startswith(("error", "warn", "report:"))
            or "found no new" in lower
            or "no changed" in lower
            or "skipped" in lower
            or re.search(r"\b\d+ (?:warnings?|errors?|findings?)\b", lower)
        ) and line not in signals:
            signals.append(line)
    return signals


def compact_line(line: str) -> str:
    if len(line) <= MAX_INLINE_LINE_LENGTH:
        return line
    return f"{line[: MAX_INLINE_LINE_LENGTH - 1]}…"


def present_text_stage(result: StageResult) -> StagePresentation:
    combined = "\n".join((result.stdout, result.stderr))
    lines = relevant_text_lines(combined)
    skipped = "slopo skipped:" in combined.lower()
    if skipped:
        summary = next(
            (line for line in lines if "skipped" in line.lower()),
            "runtime unavailable",
        )
    elif result.passed:
        summary = lines[-1] if lines else "completed without findings"
    else:
        summary = f"exit {result.exit_code}"
    if not lines and not result.passed:
        cleaned = strip_terminal_control(combined)
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()][-6:]
    findings = lines
    if result.passed:
        findings = [
            line
            for line in lines
            if "found no new" not in line.lower()
            and "no changed" not in line.lower()
            and "skipped" not in line.lower()
        ]
    return StagePresentation(
        summary=compact_line(summary),
        findings=tuple(compact_line(line) for line in findings),
        skipped=skipped,
    )


def present_stage(result: StageResult, root: Path) -> StagePresentation:
    if result.stage.name == "aislop":
        return present_aislop(result, root)
    if result.stage.name == "fallow":
        from fallow_output import present_fallow

        return present_fallow(result, root)
    return present_text_stage(result)


def relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def write_manifest(
    results: Sequence[StageResult], run_directory: Path, root: Path
) -> Path:
    manifest_path = run_directory / "summary.json"
    raw_output_path = run_directory / "raw-output.log"
    manifest = {
        "schemaVersion": 1,
        "passed": all(result.passed for result in results),
        "rawOutput": relative_path(raw_output_path, root),
        "stages": [
            {
                "name": result.stage.name,
                "command": list(result.stage.command),
                "exitCode": result.exit_code,
                "elapsedSeconds": round(result.elapsed_seconds, 3),
                "log": relative_path(result.log_path, root),
                "report": (
                    relative_path(result.report_path, root)
                    if result.report_path is not None and result.report_path.exists()
                    else None
                ),
            }
            for result in results
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def render_results(
    results: Sequence[StageResult], run_directory: Path, root: Path
) -> None:
    presentations = {
        result.stage.name: present_stage(result, root) for result in results
    }
    failed = [result for result in results if not result.passed]
    outcome = "PASS" if not failed else "FAIL"
    print(f"\nAISLOP {outcome} - {len(failed)} of {len(results)} stages failed")
    for result in results:
        presentation = presentations[result.stage.name]
        state = "SKIP" if presentation.skipped else "PASS" if result.passed else "FAIL"
        log_path = relative_path(result.log_path, root)
        print(
            f"{state:<4} {result.stage.name:<10} {presentation.summary} "
            f"({result.elapsed_seconds:.1f}s; log: {log_path})"
        )

    passed = [result for result in results if result.passed]
    ordered = [*failed, *passed]
    findings: list[tuple[str, str]] = []
    for result in failed:
        stage_findings = presentations[result.stage.name].findings
        if stage_findings:
            findings.append((result.stage.name, stage_findings[0]))
    for result in ordered:
        stage_findings = presentations[result.stage.name].findings
        start = 1 if result in failed and stage_findings else 0
        findings.extend(
            (result.stage.name, finding) for finding in stage_findings[start:]
        )
    if findings:
        print("\nFindings:")
        for stage_name, finding in findings[:MAX_INLINE_FINDINGS]:
            print(f"- [{stage_name}] {compact_line(finding)}")
        if len(findings) > MAX_INLINE_FINDINGS:
            remaining = len(findings) - MAX_INLINE_FINDINGS
            print(f"- … {remaining} more findings in the full logs")

    manifest_path = write_manifest(results, run_directory, root)
    print(f"\nFull reports: {relative_path(run_directory, root)}/")
    raw_output_path = run_directory / "raw-output.log"
    print(f"Raw output: {relative_path(raw_output_path, root)}")
    print(f"Machine summary: {relative_path(manifest_path, root)}")
