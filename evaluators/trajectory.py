"""通用 trajectory evaluator。

第一版提供结构化字段的确定性体检，并把缺失事件粒度留给 trajectory judge。
它不假设 read/write；required_tools、顺序和 state_change 对所有 tool 类型通用。
"""
from __future__ import annotations

from statistics import fmean
import json
from pathlib import Path
from typing import Any

from .base import EvaluationContext, register

DIMENSIONS = (
    "tool_selection", "argument_correctness", "order_correctness",
    "state_persistence", "verification_rate",
)


def write_trajectory_projection(run_dir: str | Path, runs: list[dict[str, Any]]) -> Path:
    """把 runs.jsonl 中的 trajectory 投影成便于 Viewer/审计的独立文件。

    原始 runs.jsonl 仍是事实来源；这个文件只是稳定的 trajectory 视图，不回写
    或修改原始结果。
    """
    path = Path(run_dir) / "trajectory.jsonl"
    lines = []
    for run in runs:
        lines.append(json.dumps({
            "case_id": run.get("case_id"),
            "repeat_index": run.get("repeat_index"),
            "turn_index": run.get("turn_index", 1),
            "request_id": run.get("request_id"),
            "session_id": run.get("session_id"),
            "status": run.get("status"),
            "error_kind": run.get("error_kind"),
            "events": run.get("trajectory") or [],
        }, ensure_ascii=False))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def _events(run: dict[str, Any]) -> list[dict[str, Any]]:
    return list(run.get("trajectory") or [])


def _tools(run: dict[str, Any]) -> set[str]:
    event_tools = {
        e.get("tool_name") for e in _events(run)
        if e.get("event_type") in {"tool_call", "tool_result"} and e.get("tool_name")
    }
    summary_tools = {t.get("name") for t in (run.get("tool_calls") or []) if t.get("name")}
    return event_tools | summary_tools


def _exact_events(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in _events(run) if e.get("event_type") == "tool_call"
            and e.get("evidence_level") == "exact"]


def score_structured(run: dict[str, Any], expectation: dict[str, Any] | None) -> dict[str, float | None]:
    """只判有明确结构化 gold 且证据足够的部分；其余返回 N/A。"""
    out: dict[str, float | None] = {name: None for name in DIMENSIONS}
    if not expectation:
        return out

    tools = _tools(run)
    required = set(expectation.get("required_tools") or [])
    forbidden = set(expectation.get("forbidden_tools") or [])
    if required or forbidden:
        hits = sum(tool in tools for tool in required)
        misses = sum(tool in tools for tool in forbidden)
        denominator = len(required) + len(forbidden)
        out["tool_selection"] = round((hits + len(forbidden) - misses) / denominator, 4)

    order = list(expectation.get("required_order") or [])
    exact = _exact_events(run)
    if order and exact:
        observed = [e.get("tool_name") for e in exact]
        positions = []
        cursor = 0
        for tool in order:
            try:
                cursor = observed.index(tool, cursor) + 1
            except ValueError:
                break
            positions.append(tool)
        out["order_correctness"] = round(len(positions) / len(order), 4)

    if expectation.get("required_state_change"):
        artifacts = run.get("artifacts") or []
        state_events = [e for e in _events(run) if e.get("event_type") == "state_change"
                        and e.get("status") == "success"]
        out["state_persistence"] = 1.0 if artifacts or state_events else 0.0

    if expectation.get("required_verification"):
        verified = [e for e in _events(run) if e.get("event_type") == "verification"
                    and e.get("status") == "success"]
        # 有明确轨迹时判；旧 runtime 没有 verification 事件，不能把未知当失败。
        out["verification_rate"] = 1.0 if _events(run) and verified else None
    return out


@register("trajectory")
class TrajectoryEvaluator:
    def evaluate(self, context: EvaluationContext) -> dict[str, Any]:
        cases = context.cases
        per_run = []
        for run in context.runs:
            case = cases.get(run.get("case_id"))
            turn = case.resolved_turn(int(run.get("turn_index") or 1)) if case else None
            expectation = turn.expect_trajectory.model_dump(mode="json") if turn and turn.expect_trajectory else None
            per_run.append({
                "case_id": run.get("case_id"),
                "repeat": run.get("repeat_index"),
                "turn": run.get("turn_index", 1),
                "metrics": score_structured(run, expectation),
                "evidence_level": (
                    "exact" if any(e.get("evidence_level") == "exact" for e in _events(run))
                    else ("coarse" if _events(run) else "missing")
                ),
            })
        means: dict[str, float | None] = {}
        for name in DIMENSIONS:
            values = [row["metrics"][name] for row in per_run
                      if row["metrics"][name] is not None]
            means[name] = round(fmean(values), 4) if values else None
        return {
            "mode": (context.suite.get("scoring", {}).get("trajectory", {}) or {}).get("mode", "judge"),
            "metrics": means,
            "structured": per_run,
            "note": "缺少事件级 runtime 证据的维度保留 N/A；LLM judge 结果另存并在评分阶段合并。",
        }
