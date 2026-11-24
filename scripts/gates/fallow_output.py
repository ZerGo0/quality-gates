"""Render fallow reports for the repository quality-gate runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from aislop_output import (
    StagePresentation,
    StageResult,
    diagnostic_line,
    display_path,
    load_json,
    present_text_stage,
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
