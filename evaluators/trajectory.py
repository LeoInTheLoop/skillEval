"""通用 trajectory evaluator。

第一版提供结构化字段的确定性体检，并把缺失事件粒度留给 trajectory judge。
它不假设 read/write；required_tools、顺序和 state_change 对所有 tool 类型通用。
"""
from __future__ import annotations

from statistics import fmean
import json
from pathlib import Path
import re
from typing import Any

from contracts.trajectory import (
    ARGUMENT_ASSERTION_SCHEMA_VERSION,
    ARGUMENT_CORRECTNESS_RUBRIC_VERSION,
    TrajectoryArgumentExpectation,
)
from .base import EvaluationContext, register

DIMENSIONS = (
    "tool_selection", "argument_correctness", "order_correctness",
    "state_persistence", "verification_rate",
)
EVALUATOR_VERSION = "trajectory-v2"


def merge_trajectory_metrics(
    deterministic: dict[str, float | None],
    judge: dict[str, float | None],
    mode: str,
) -> dict[str, float | None]:
    """按 suite mode 合并量具；hybrid 中确定性证据优先。"""
    if mode == "deterministic":
        return dict(deterministic)
    if mode == "judge":
        return dict(judge)
    if mode != "hybrid":
        raise ValueError(f"未知 trajectory mode：{mode}")
    return {
        name: deterministic.get(name)
        if deterministic.get(name) is not None else judge.get(name)
        for name in dict.fromkeys([*deterministic, *judge])
    }


def write_trajectory_projection(
    run_dir: str | Path,
    runs: list[dict[str, Any]],
    output_path: str | Path | None = None,
) -> Path:
    """把 runs.jsonl 中的 trajectory 投影成便于 Viewer/审计的独立文件。

    原始 runs.jsonl 仍是事实来源；这个文件只是稳定的 trajectory 视图，不回写
    或修改原始结果。
    """
    path = Path(output_path) if output_path is not None else Path(run_dir) / "trajectory.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _argument_value(arguments: dict[str, Any], path: str) -> tuple[bool, Any]:
    """读取点分 argument path；数字段可索引 list。返回 (是否存在, 值)。"""
    value: Any = arguments
    for segment in path.split("."):
        if isinstance(value, dict) and segment in value:
            value = value[segment]
            continue
        if isinstance(value, list) and segment.isdigit():
            index = int(segment)
            if index < len(value):
                value = value[index]
                continue
        return False, None
    return True, value


def _evidence_refs(events: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for event in events:
        if event.get("call_id"):
            refs.append(f"call_id={event['call_id']}")
        if event.get("step_index") is not None:
            refs.append(f"step_index={event['step_index']}")
    return list(dict.fromkeys(refs))


def _value_is_incomplete(value: Any) -> bool:
    """Adapter 脱敏/截断过的值不能被 equals/in/matches 当作完整反证。"""
    if isinstance(value, str):
        return value == "<redacted>" or "…[truncated at " in value
    if isinstance(value, dict):
        return any(_value_is_incomplete(child) for child in value.values())
    if isinstance(value, list):
        return any(_value_is_incomplete(child) for child in value)
    return False


def _json_equal(actual: Any, expected: Any) -> bool:
    # Python 会把 True == 1 判真；JSON gold 里它们是不同类型。
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def score_argument_assertions(
    run: dict[str, Any], assertions: list[dict[str, Any]] | None,
) -> tuple[float | None, list[dict[str, Any]]]:
    """用 exact tool-call arguments 判参数 gold，并保留逐断言证据。

    有 gold 但预期 tool 没调用或参数明确不匹配记 0；runtime 没交付 exact
    arguments、或相关值已被脱敏/截断时记 N/A，绝不拿未知冒充失败。
    """
    if not assertions:
        return None, []

    specs = [TrajectoryArgumentExpectation.model_validate(item) for item in assertions]
    exact = _exact_events(run)
    details: list[dict[str, Any]] = []
    for index, spec in enumerate(specs):
        relevant = [event for event in exact if event.get("tool_name") == spec.tool]
        refs = _evidence_refs(relevant or exact)
        score: float | None
        evidence: str

        if not exact:
            score = None
            evidence = "runtime 未交付 exact tool-call arguments"
        elif not relevant:
            score = 0.0
            evidence = "预期 tool 未出现在 exact trajectory 中"
        else:
            known = [event for event in relevant if isinstance(event.get("arguments"), dict)]
            unknown_arguments = len(known) != len(relevant)
            resolved = [
                _argument_value(event["arguments"], spec.path)
                for event in known
            ]
            present_values = [value for present, value in resolved if present]

            if spec.matcher == "required":
                if present_values:
                    score, evidence = 1.0, "至少一次相关调用包含必填参数路径"
                elif unknown_arguments:
                    score, evidence = None, "相关调用缺少可判定的 exact arguments"
                else:
                    score, evidence = 0.0, "所有相关调用都缺少必填参数路径"
            elif spec.matcher == "forbidden":
                if present_values:
                    score, evidence = 0.0, "至少一次相关调用包含禁用参数路径"
                elif unknown_arguments:
                    score, evidence = None, "相关调用缺少可判定的 exact arguments"
                else:
                    score, evidence = 1.0, "所有相关调用都未包含禁用参数路径"
            else:
                incomplete = unknown_arguments or any(
                    _value_is_incomplete(value) for value in present_values
                )
                complete_values = [
                    value for value in present_values if not _value_is_incomplete(value)
                ]
                if spec.matcher == "equals":
                    passed = any(_json_equal(value, spec.equals) for value in complete_values)
                elif spec.matcher == "in":
                    passed = any(
                        any(_json_equal(value, allowed) for allowed in (spec.in_values or []))
                        for value in complete_values
                    )
                else:
                    pattern = re.compile(spec.matches or "")
                    passed = any(isinstance(value, str) and pattern.search(value)
                                 for value in complete_values)
                if passed:
                    score, evidence = 1.0, "至少一次相关调用满足参数 matcher"
                elif incomplete:
                    score, evidence = None, "相关参数已脱敏、截断或缺失，证据不足"
                else:
                    score, evidence = 0.0, "相关调用均未满足参数 matcher"

        details.append({
            "assertion_index": index,
            "assertion": spec.model_dump(mode="json", by_alias=True, exclude_none=True),
            "score": score,
            "status": "passed" if score == 1.0 else ("failed" if score == 0.0
                                                        else "insufficient_evidence"),
            "evidence": evidence,
            "evidence_refs": refs,
        })

    scored = [item["score"] for item in details if item["score"] is not None]
    return (round(fmean(scored), 4) if scored else None), details


def _arg_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _arg_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _arg_strings(v)]
    return []


def _targets(event: dict[str, Any], path: str) -> bool:
    """这次调用是不是**指向** path。

    只认「某个参数值以该路径结尾」，不认「参数里出现过这串字符」——否则一篇正文里
    提到了文件名的 write 会被算成读回它自己。
    """
    return any(s == path or s.endswith(path) for s in _arg_strings(event.get("arguments")))


def _succeeded(run: dict[str, Any]) -> set[str]:
    return {e.get("call_id") for e in _events(run)
            if e.get("event_type") == "tool_result" and e.get("status") == "success"
            and e.get("call_id")}


def _readback_rate(run: dict[str, Any], probes: set[str]) -> float | None:
    """「改过的东西，事后有没有被题目声明的 probe 工具成功读过」。

    两处分类都不靠猜 tool 语义：probe 由题目声明（`verification_tools`），
    「状态确实变了」由 workspace diff 的 artifact 证明。probe 之外的调用一律
    当作产生方，所以判定是「先产生、后观察」的顺序事实，不是对工具的语义假设。

    ponytail: 只覆盖「产物有路径」这一类环境。数据库/API 类副作用要验证时，
    得由那类 skill 的专用 evaluator（T3）自己给出 state key，别在这儿加分支。
    """
    exact = _exact_events(run)
    paths = [a.get("path") for a in (run.get("artifacts") or []) if a.get("path")]
    if not probes or not exact or not paths:
        return None
    ok = _succeeded(run)
    produced, verified = 0, 0
    for path in paths:
        writes = [e for e in exact
                  if e.get("tool_name") not in probes and _targets(e, path)]
        if not writes:
            continue  # 不是 agent 显式写出来的，不要求它验证
        produced += 1
        first_write = min(int(e.get("step_index") or 0) for e in writes)
        if any(e.get("tool_name") in probes and _targets(e, path)
               and e.get("call_id") in ok
               and int(e.get("step_index") or 0) > first_write
               for e in exact):
            verified += 1
    return round(verified / produced, 4) if produced else None


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

    out["argument_correctness"], _ = score_argument_assertions(
        run, expectation.get("argument_assertions") or []
    )

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
        # runtime 给了明确 verification 事件就直接用；没有就退回「先产生、后观察」的
        # read-back 事实。两者都没有（旧 coarse 轨迹）才是真的 N/A，不能把未知当失败。
        out["verification_rate"] = (
            1.0 if verified
            else _readback_rate(run, set(expectation.get("verification_tools") or []))
        )
    return out


@register("trajectory")
class TrajectoryEvaluator:
    def evaluate(self, context: EvaluationContext) -> dict[str, Any]:
        cases = context.cases
        per_run = []
        for run in context.runs:
            case = cases.get(run.get("case_id"))
            turn = case.resolved_turn(int(run.get("turn_index") or 1)) if case else None
            expectation = (
                turn.expect_trajectory.model_dump(
                    mode="json", by_alias=True, exclude_none=True, exclude_defaults=True,
                )
                if turn and turn.expect_trajectory else None
            )
            metrics = score_structured(run, expectation)
            _, argument_details = score_argument_assertions(
                run, (expectation or {}).get("argument_assertions") or []
            )
            per_run.append({
                "case_id": run.get("case_id"),
                "repeat": run.get("repeat_index"),
                "turn": run.get("turn_index", 1),
                "metrics": metrics,
                "argument_assertions": argument_details,
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
        trajectory_config = context.suite.get("scoring", {}).get("trajectory", {}) or {}
        return {
            "mode": trajectory_config.get("mode", "judge"),
            "versions": {
                "trajectory": trajectory_config.get("version", "trajectory-v1"),
                "argument_assertion_schema": trajectory_config.get(
                    "argument_schema_version", ARGUMENT_ASSERTION_SCHEMA_VERSION,
                ),
                "argument_correctness_rubric": trajectory_config.get(
                    "argument_rubric_version", ARGUMENT_CORRECTNESS_RUBRIC_VERSION,
                ),
            },
            "metrics": means,
            "structured": per_run,
            "note": "缺少事件级 runtime 证据的维度保留 N/A；LLM judge 结果另存并在评分阶段合并。",
        }
