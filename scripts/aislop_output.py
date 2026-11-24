"""Render compact output for the repository quality-gate runner."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

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


def walk_json_objects(value: Any) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if (
            isinstance(value.get("message") or value.get("description"), str)
            and any(
                key in value for key in ("path", "file", "filePath", "filename")
            )
        ):
            objects.append(value)
        for child in value.values():
            objects.extend(walk_json_objects(child))
    elif isinstance(value, list):
        for child in value:
            objects.extend(walk_json_objects(child))
    return objects


def walk_matching_objects(
    value: Any, matches: Callable[[dict[str, Any]], bool]
) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if matches(value):
            objects.append(value)
        for child in value.values():
            objects.extend(walk_matching_objects(child, matches))
    elif isinstance(value, list):
        for child in value:
            objects.extend(walk_matching_objects(child, matches))
    return objects


def fallow_finding_lines(
    items: list[dict[str, Any]], root: Path, label: str
) -> list[str]:
    findings: list[str] = []
    for item in items:
        if "instances" in item:
            continue
        if isinstance(item.get("message") or item.get("description"), str):
            findings.append(diagnostic_line(item, root))
            continue
        path = display_path(
            item.get("path") or item.get("file") or item.get("filePath"), root
        )
        line = item.get("line")
        location = f"{path}:{line}" if isinstance(line, int) else path
        actions = item.get("actions")
        actions = actions if isinstance(actions, list) else []
        first_action = actions[0] if actions and isinstance(actions[0], dict) else {}
        description = first_action.get("description") or label
        findings.append(f"{location} {label}: {description}")
    return findings


def fallow_clone_groups(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    section = payload.get(key)
    groups = section.get("clone_groups") if isinstance(section, dict) else None
    if not isinstance(groups, list):
        return []
    return [group for group in groups if isinstance(group, dict)]


def fallow_duplication_findings(
    groups: list[dict[str, Any]], root: Path
) -> list[str]:
    findings: list[str] = []
    for group in groups:
        instances = group.get("instances")
        instances = instances if isinstance(instances, list) else []
        first = instances[0] if instances and isinstance(instances[0], dict) else {}
        path = display_path(first.get("file"), root)
        line = first.get("start_line")
        location = f"{path}:{line}" if isinstance(line, int) else path
        fingerprint = group.get("fingerprint") or "duplication"
        line_count = group.get("line_count") or "unknown"
        findings.append(
            f"{location} duplication [{fingerprint}]: "
            f"{len(instances)} instances, {line_count} lines"
        )
    return findings


def present_fallow_repository(payload: dict[str, Any], root: Path) -> StagePresentation:
    check = payload.get("check")
    check = check if isinstance(check, dict) else {}
    health = payload.get("health")
    health_findings = health.get("findings") if isinstance(health, dict) else None
    health_findings = [
        item for item in health_findings or [] if isinstance(item, dict)
    ]
    groups = fallow_clone_groups(payload, "dupes")
    summary = (
        f"{check.get('total_issues', 0)} dead-code issues, "
        f"{len(groups)} clone groups, {len(health_findings)} health findings"
    )
    findings = fallow_finding_lines(
        [*walk_matching_objects(check, lambda item: "finding_id" in item), *health_findings],
        root,
        "finding",
    )
    findings.extend(fallow_duplication_findings(groups, root))
    return StagePresentation(summary=summary, findings=tuple(dict.fromkeys(findings)))


def present_fallow(result: StageResult, root: Path) -> StagePresentation:
    report_text = ""
    if result.report_path is not None and result.report_path.is_file():
        report_text = result.report_path.read_text(encoding="utf-8", errors="replace")
    payload = load_json(report_text)
    if not isinstance(payload, dict):
        return present_text_stage(result)
    if payload.get("kind") == "combined":
        return present_fallow_repository(payload, root)

    attribution = payload.get("attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    introduced = sum(
        value
        for key, value in attribution.items()
        if key.endswith("_introduced") and isinstance(value, int)
    )
    inherited = sum(
        value
        for key, value in attribution.items()
        if key.endswith("_inherited") and isinstance(value, int)
    )
    verdict = payload.get("verdict", "unknown")
    gate = attribution.get("gate", "unknown")
    summary = (
        f"verdict {verdict}; {introduced} introduced, {inherited} inherited; "
        f"gate {gate}"
    )

    findings = fallow_finding_lines(
        walk_matching_objects(payload, lambda item: item.get("introduced") is True),
        root,
        "introduced finding",
    )
    findings.extend(
        fallow_duplication_findings(
            [
                group
                for group in fallow_clone_groups(payload, "duplication")
                if group.get("introduced") is True
            ],
            root,
        )
    )
    if not findings and not result.passed:
        findings.extend(diagnostic_line(item, root) for item in walk_json_objects(payload))
    return StagePresentation(summary=summary, findings=tuple(dict.fromkeys(findings)))


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
