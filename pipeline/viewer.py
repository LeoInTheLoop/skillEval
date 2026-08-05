"""Read-only run inspection for the P7 traceability path.

The scorer reports aggregate quality.  This module answers the adjacent user
question: "where are the facts for this exact case/repeat/turn?"  It only reads
immutable run inputs/results and projects them into one stable, filterable
shape; it never rewrites ``runs.jsonl`` or scoring artifacts.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import yaml

from contracts import load_cases


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _dataset_path(run_dir: Path, snapshot: dict[str, Any], root: Path) -> Path | None:
    frozen = run_dir / "inputs" / "dataset.jsonl"
    if frozen.is_file():
        return frozen
    configured = ((snapshot.get("suite") or {}).get("dataset"))
    if not configured:
        return None
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else root / candidate


def _record_status(run: dict[str, Any]) -> str:
    if run.get("status") == "skipped":
        return "skipped"
    return "ok" if run.get("ok", True) else "failed"


def _tool_names(value: Any) -> list[str]:
    return [
        str(item.get("name"))
        for item in (value or [])
        if isinstance(item, dict) and item.get("name")
    ]


def _artifact_paths(value: Any) -> list[str]:
    return [
        str(item.get("path"))
        for item in (value or [])
        if isinstance(item, dict) and item.get("path")
    ]


def inspect_run(run_dir: str | Path, *, root: str | Path) -> dict[str, Any]:
    """Load one exact run directory into a JSON-safe trace projection."""
    workspace = Path(root).resolve()
    directory = Path(run_dir).expanduser().resolve()
    runs_path = directory / "runs.jsonl"
    snapshot_path = directory / "config.snapshot.yaml"
    if not directory.is_dir() or not runs_path.is_file():
        raise FileNotFoundError(
            f"not a run directory: {directory} (expected runs.jsonl); "
            "pass the exact execution directory printed by `pipeline run`"
        )
    snapshot = (
        yaml.safe_load(snapshot_path.read_text(encoding="utf-8")) or {}
        if snapshot_path.is_file() else {}
    )
    suite = snapshot.get("suite") or {}
    dataset_path = _dataset_path(directory, snapshot, workspace)
    cases = {}
    if dataset_path is not None and dataset_path.is_file():
        cases = {case.id: case for case in load_cases(dataset_path)}

    records = []
    observed_models: set[str] = set()
    for line_number, line in enumerate(runs_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"{runs_path}:{line_number} must be a JSON object")
        case_id = str(raw.get("case_id") or "")
        case = cases.get(case_id)
        actual_model = raw.get("resolved_model") or raw.get("model")
        if actual_model:
            observed_models.add(str(actual_model))
        records.append({
            "case_id": case_id,
            "turn": int(raw.get("turn_index") or 1),
            "repeat": int(raw.get("repeat_index") or 0),
            "status": _record_status(raw),
            "model": actual_model,
            "session_id": raw.get("session_id"),
            "prompt": case.prompt if case is not None else None,
            "expected_skills": list(case.expected_skills) if case is not None else [],
            "selected_skills": list(raw.get("selected_skills") or []),
            "loaded_skills": list(raw.get("loaded_skills") or []),
            "tool_calls": _tool_names(raw.get("tool_calls")),
            "artifacts": _artifact_paths(raw.get("artifacts")),
            "final_answer": raw.get("final_answer"),
            "error_kind": raw.get("error_kind"),
            "error_subkind": raw.get("error_subkind"),
            "error": raw.get("error"),
        })

    scores = _read_json(directory / "scores.json")
    statuses = Counter(record["status"] for record in records)
    grading = sorted(
        _relative(path, workspace)
        for pattern in ("grading.*.json", "grading/**/*.json")
        for path in directory.glob(pattern)
    )
    reports = sorted(
        _relative(path, workspace)
        for pattern in ("report.html", "reports/**/*.html")
        for path in directory.glob(pattern)
    )
    suggestions = sorted(
        _relative(path, workspace)
        for path in directory.glob("improvements/**/suggestions.json")
    )
    return {
        "schema_version": "run-inspection-v1",
        "run_dir": _relative(directory, workspace),
        "suite_id": suite.get("suite_id"),
        "skill_mode": (suite.get("skills") or {}).get("mode"),
        "skill_cfg": (suite.get("skills") or {}).get("cfg"),
        "config_hash": snapshot.get("config_hash"),
        "observed_models": sorted(observed_models),
        "record_count": len(records),
        "matching_record_count": len(records),
        "available_case_ids": sorted({record["case_id"] for record in records}),
        "status_counts": dict(sorted(statuses.items())),
        "gate_pass": scores.get("gate_pass") if scores else None,
        "quality_verdict": scores.get("quality_verdict") if scores else None,
        "metrics": scores.get("scores", {}) if scores else {},
        "paths": {
            "snapshot": _relative(snapshot_path, workspace) if snapshot_path.is_file() else None,
            "runs": _relative(runs_path, workspace),
            "dataset": _relative(dataset_path, workspace) if dataset_path else None,
            "scores": _relative(directory / "scores.json", workspace) if scores else None,
            "reports": reports,
            "grading": grading,
            "suggestions": suggestions,
        },
        "records": records,
    }


def filter_records(
    records: Iterable[dict[str, Any]],
    *,
    case_id: str | None = None,
    status: str | None = None,
    skill: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Filter the stable projection without rereading or changing a run."""
    selected = []
    for record in records:
        if case_id and record["case_id"] != case_id:
            continue
        if status and record["status"] != status:
            continue
        if model and model.lower() not in str(record.get("model") or "").lower():
            continue
        skills = {
            *record.get("expected_skills", []),
            *record.get("selected_skills", []),
            *record.get("loaded_skills", []),
        }
        if skill and skill not in skills:
            continue
        selected.append(record)
    return selected


def render_inspection(view: dict[str, Any]) -> str:
    """Render a compact terminal view with every artifact path locatable."""
    def excerpt(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    verdict = view.get("quality_verdict") or (
        "PASS" if view.get("gate_pass") is True
        else "FAIL" if view.get("gate_pass") is False
        else "N/A"
    )
    lines = [
        f"Run: {view['run_dir']}",
        f"Suite: {view.get('suite_id') or 'unknown'} | mode={view.get('skill_mode') or 'unknown'} "
        f"| skillcfg={view.get('skill_cfg') or 'unknown'}",
        f"Verdict: {verdict} | records={view.get('matching_record_count', view['record_count'])}"
        f"/{view['record_count']} "
        f"| status={view['status_counts']}",
        f"Models: {', '.join(view.get('observed_models') or []) or 'unresolved'}",
        "Evidence paths:",
    ]
    paths = view["paths"]
    for key in ("snapshot", "dataset", "runs", "scores"):
        lines.append(f"  {key}: {paths.get(key) or 'N/A'}")
    for key in ("reports", "grading", "suggestions"):
        values = paths.get(key) or []
        lines.append(f"  {key}: {', '.join(values) if values else 'N/A'}")
    lines.append("Records:")
    if not view["records"]:
        lines.append("  (no records match the filters)")
        lines.append(
            f"  available case ids: {', '.join(view.get('available_case_ids') or []) or 'N/A'}"
        )
    for record in view["records"]:
        expected = record["expected_skills"] or ["∅"]
        selected = record["selected_skills"] or ["∅"]
        lines.append(
            f"  {record['case_id']} t{record['turn']} r{record['repeat']} "
            f"[{record['status']}] expected={expected} selected={selected}"
        )
        if record.get("prompt"):
            lines.append(f"    prompt: {excerpt(record['prompt'], 180)}")
        if record.get("final_answer"):
            lines.append(f"    answer: {excerpt(record['final_answer'], 300)}")
        if record["tool_calls"] or record["artifacts"]:
            lines.append(
                f"    tools={record['tool_calls'] or []} artifacts={record['artifacts'] or []}"
            )
        if record.get("error"):
            lines.append(
                f"    error={record.get('error_kind') or 'unclassified'}/"
                f"{record.get('error_subkind') or '-'}: {record['error']}"
            )
    return "\n".join(lines)
