"""通用 trajectory 的第一版 LLM-as-judge。

它只判断可观察的动作、工具结果和状态证据，不评价隐藏 CoT。输出和普通
``grading.<judge>.json`` 分开，后续 deterministic / skill-specific evaluator
可以替换或叠加而不污染原始 runs.jsonl。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from contracts import TRAJECTORY_DIMENSIONS, SuiteJudgeSpec, TrajectoryDimensionScore, load_cases
from judges.llm import LLMJudge, call_litellm

ROOT = Path(__file__).parent.parent
GRADER_VERSION = "trajectory-judge-v1"

SYSTEM_PROMPT = """你是 Agent 执行轨迹评测器。
你评估的是可观察的 agent 行为，不是隐藏思维过程，也不是最终回答本身。

规则：
- 只根据用户任务、轨迹事件、工具摘要、状态变化和产物证据评分。
- 工具摘要只有 coarse 粒度时，不能推断具体参数或调用顺序。
- 没有事件或状态证据时必须返回 score=null（N/A），不能把缺数据当成 0。
- score 为 0 到 1；evidence 必须引用输入中可见的证据，不能编造。
- 最终回答声称“完成”不能证明工具真的执行成功；优先看 tool result 和环境状态。
"""


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class _Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimensions: list[TrajectoryDimensionScore] = Field(default_factory=list)


def _resolve_dataset(snapshot: dict[str, Any]) -> Path:
    path = Path(snapshot["suite"]["dataset"]).expanduser()
    return path if path.is_absolute() else ROOT / path


def _judge(snapshot: dict[str, Any]) -> SuiteJudgeSpec:
    configured = (snapshot.get("suite", {}).get("scoring") or {}).get("judge")
    if not configured:
        raise ValueError("trajectory judge 需要 suite.scoring.judge")
    return SuiteJudgeSpec.model_validate(configured)


def trajectory_path(run_dir: Path, judge_id: str) -> Path:
    return run_dir / f"trajectory_grading.{judge_id}.json"


def build_prompt(case, run: dict[str, Any], dimensions: list[str]) -> str:
    turn = case.resolved_turn(int(run.get("turn_index") or 1))
    expectation = (
        turn.expect_trajectory.model_dump(mode="json")
        if turn.expect_trajectory else None
    )
    events = run.get("trajectory") or []
    tools = run.get("tool_calls") or []
    artifacts = [
        {key: value for key, value in artifact.items()
         if key in {"path", "sha256", "size_bytes", "mime_type", "change", "text_excerpt"}}
        for artifact in (run.get("artifacts") or [])
    ]
    return "\n\n".join([
        f"[用户任务]\n{turn.prompt}",
        "[本题轨迹期望]\n" + json.dumps(expectation, ensure_ascii=False, indent=2),
        "[事件级轨迹；如果为空表示 runtime 没有提供事件级记录]\n"
        + json.dumps(events, ensure_ascii=False, indent=2),
        "[工具聚合摘要；只能证明使用过哪些工具，不能证明顺序和参数]\n"
        + json.dumps(tools, ensure_ascii=False, indent=2),
        "[环境产物/状态证据]\n" + json.dumps(artifacts, ensure_ascii=False, indent=2),
        "[最终回答]\n" + (run.get("final_answer") or run.get("raw_output") or run.get("error") or ""),
        "[需要评分的维度]\n" + json.dumps(dimensions, ensure_ascii=False),
        "[输出约束]\n只返回 JSON：{\"dimensions\":[{\"dimension\":string,\"score\":0到1或null,\"evidence\":string,\"source\":\"judge\"}]}；"
        "dimensions 必须逐项按输入顺序返回，不得漏项或增加。",
    ])


def grade_one(case, run: dict[str, Any], judge: SuiteJudgeSpec,
              completion: Callable[..., str] = call_litellm) -> dict[str, Any]:
    configured = (run.get("trajectory_dimensions") or list(TRAJECTORY_DIMENSIONS))
    batch = LLMJudge(
        judge,
        system_prompt=SYSTEM_PROMPT,
        completion=completion,
    ).judge(build_prompt(case, run, configured), _Batch)
    got = [item.dimension for item in batch.dimensions]
    if got != configured:
        raise ValueError(f"{case.id}: trajectory judge 维度不匹配；期望 {configured}，实际 {got}")
    return {
        "case_id": run["case_id"],
        "repeat_index": run.get("repeat_index", 0),
        "turn_index": run.get("turn_index", 1),
        "dimensions": [item.model_dump(mode="json") for item in batch.dimensions],
    }


def grade_run_dir(run_dir: Path, *, completion: Callable[..., str] = call_litellm) -> dict[str, Any]:
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    trajectory = ((snapshot.get("suite", {}).get("scoring") or {}).get("trajectory") or {})
    if not trajectory.get("enabled"):
        raise ValueError("suite.scoring.trajectory.enabled=false")
    judge = _judge(snapshot)
    dimensions = list(trajectory.get("dimensions") or TRAJECTORY_DIMENSIONS)
    cases = {c.id: c for c in load_cases(_resolve_dataset(snapshot))}
    graded: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for line in (run_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        run = json.loads(line)
        if run.get("status") in {"skipped"} or run.get("error_kind") in {"runtime", "network", "harness"}:
            continue
        run["trajectory_dimensions"] = dimensions
        try:
            graded.append(grade_one(cases[run["case_id"]], run, judge, completion))
        except Exception as error:  # noqa: BLE001 — one judge failure cannot kill the batch
            failures.append({"case_id": run["case_id"], "repeat_index": run.get("repeat_index", 0),
                             "turn_index": run.get("turn_index", 1), "error": repr(error)})

    by_dim: dict[str, list[float]] = {name: [] for name in dimensions}
    for item in graded:
        for score in item["dimensions"]:
            if score["score"] is not None:
                by_dim[score["dimension"]].append(score["score"])
    means = {name: round(sum(values) / len(values), 4) if values else None
             for name, values in by_dim.items()}
    return {
        "grader_version": GRADER_VERSION,
        "run_dir": run_dir.name,
        "config_hash": snapshot.get("config_hash"),
        "judge": {
            "id": judge.id, "model": judge.model,
            "api_base_env": judge.api_base_env, "api_key_env": judge.api_key_env,
            "params": judge.params,
            "system_prompt_hash": _sha(SYSTEM_PROMPT),
        },
        "trajectory": {"version": trajectory.get("version", "trajectory-v1"),
                       "dimensions": dimensions},
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "graded": graded,
        "judge_failures": failures,
        "dimension_means": means,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="用 judge 模型评估通用 Agent trajectory")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    run_dir = Path(args.dir).resolve()
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    judge = _judge(snapshot)
    out = trajectory_path(run_dir, judge.id)
    if out.exists() and not args.force:
        raise SystemExit(f"已存在 {out.name}；加 --force 才覆盖")
    if args.dry_run:
        print(f"trajectory judge={judge.id}/{judge.model}; output={out}; no model call")
        return
    report = grade_run_dir(run_dir)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"trajectory grading → {out}")
    print(json.dumps(report["dimension_means"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
