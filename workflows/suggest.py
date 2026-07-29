"""P3 最薄切片：失败 trajectory → 带原文证据的聚类改进建议。

沿用 OpenClaw-RL 的 next-state 思路，但不引入 RL 栈：这里的学习信号就是已经落盘的
RunResult 原始输出。脚本只生成 suggestions.json，绝不改写 SKILL.md。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Literal

import frontmatter
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from contracts import VERSION_DIR, RunResult, load_cases, version_order
from workflows.litellm_support import quiet_completion
from workflows.metrics import _tokens
from workflows.score_full import artifact_hit

ROOT = Path(__file__).parent.parent
SUGGESTER_VERSION = "p3-v0.2"
# 只在 source run 的快照里没有可继承模型时才用得上（OpenClaw run 的模型是 runtime 管的）。
# 硬编码的型号迟早会过期或额度用完 —— 报 quota exhausted 时用 --model 显式换一个。
DEFAULT_MODEL = "openai/qwen3.7-flash-2026-07-15"
DEFAULT_API_BASE_ENV = "DASHSCOPE_BASE_URL"
DEFAULT_API_KEY_ENV = "DASHSCOPE_API_KEY"
SYSTEM_PROMPT = """你是 Agent Skill 失败分析器。
基于失败 case 的模型原文证据，把多个失败按共同根因聚类，并提出对 SKILL.md 的具体修改建议。
只返回 JSON；不得编造证据，不得逐题各写一条，不得直接改写文件。"""


FailureMetric = Literal["exact_set_match", "task_completion", "assertion"]
StopReason = Literal["gate_pass", "budget_exceeded", "max_iterations"]
"""失败按哪个指标归因。

`exact_set_match` 是 routing-only 的唯一失败形态（选错 skill）。full eval 里选对了
skill 也可能失败 —— 产物没落、tool 没调、内容写错，所以多两个：`task_completion`
（确定性断言没过）和 `assertion`（judge 判定的语义断言没过）。
"""

RAW_OUTPUT_LIMIT = 3000
DETAIL_EXCERPT_LIMIT = 1200


class FailureEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    repeat_index: int
    turn_index: int = 1
    metric: FailureMetric = "exact_set_match"
    expected_skills: list[str]
    selected_skills: list[str]
    raw_output: str
    # full eval 才填：确定性断言缺了什么、judge 判 failed 的断言与证据、产物内容摘录。
    # routing 只有「选错」一种形态，模型原文本身就是全部证据，不需要它。
    failure_detail: str | None = None


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    quote: str = Field(min_length=1)


class SuggestionCluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1)
    case_ids: list[str] = Field(min_length=1)
    metric: FailureMetric
    evidence: list[EvidenceQuote] = Field(min_length=1)
    change: str = Field(min_length=1)


class SuggestedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestions: list[SuggestionCluster]


class BudgetLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_total_tokens: int | None = Field(default=None, gt=0)
    max_total_seconds: float | None = Field(default=None, gt=0)


class BudgetUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_tokens: int = Field(default=0, ge=0)
    total_seconds: float = Field(default=0.0, ge=0)
    runs: int = Field(default=0, ge=0)
    runs_missing_tokens: int = Field(default=0, ge=0)
    runs_missing_duration: int = Field(default=0, ge=0)


class SuggestionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    source_run: str
    target_skill: str
    iteration: int
    max_iterations: int
    generated_at: str
    model: str | None
    prompt_hash: str | None
    failures: list[FailureEvidence]
    suggestions: list[SuggestionCluster]
    stop_reason: StopReason | None = None
    triggered_stop_reasons: list[StopReason] = Field(default_factory=list)
    run_lineage: list[str] = Field(default_factory=list)
    previous_report: str | None = None
    budget_limits: BudgetLimits = Field(default_factory=BudgetLimits)
    budget_usage: BudgetUsage = Field(default_factory=BudgetUsage)
    gate_pass: bool | None = None
    apply_status: Literal["not_requested", "applied"] = "not_requested"
    # --apply 落盘后才有：新版本目录、复验 suite、以及新旧正文的 hash（改没改得动，看它俩）
    applied_version: str | None = None
    applied_skill_path: str | None = None
    reeval_suite_path: str | None = None
    source_skill_hash: str | None = None
    applied_skill_hash: str | None = None


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _resolve_dataset(snapshot: dict) -> Path:
    path = Path(snapshot["suite"]["dataset"]).expanduser()
    return path if path.is_absolute() else ROOT / path


def load_snapshot(run_dir: str | Path) -> dict:
    return yaml.safe_load((Path(run_dir) / "config.snapshot.yaml").read_text(encoding="utf-8"))


def load_scores(run_dir: str | Path) -> dict:
    path = Path(run_dir) / "scores.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"停止条件需要评分结果，但找不到 {path}\n"
            "  → 先运行对应的 workflows.score_full / workflows.score_routing，"
            "再运行 workflows.suggest"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "gate_pass" not in payload:
        raise ValueError(f"{path} 缺 gate_pass，无法判断是否已达到发布门槛")
    return payload


def load_previous_report(path: str | Path) -> SuggestionReport:
    return SuggestionReport.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _run_usage(run_dir: Path) -> BudgetUsage:
    runs_path = run_dir / "runs.jsonl"
    if not runs_path.is_file():
        raise FileNotFoundError(f"迭代 lineage 缺 runs.jsonl：{runs_path}")
    total_tokens = 0.0
    total_seconds = 0.0
    runs = missing_tokens = missing_duration = 0
    for line in runs_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        runs += 1
        tokens = _tokens(payload.get("usage") or {})
        if tokens is None:
            missing_tokens += 1
        else:
            total_tokens += tokens
        duration = payload.get("duration_ms")
        if duration is None:
            missing_duration += 1
        else:
            total_seconds += float(duration) / 1000.0
    return BudgetUsage(
        total_tokens=int(total_tokens),
        total_seconds=round(total_seconds, 4),
        runs=runs,
        runs_missing_tokens=missing_tokens,
        runs_missing_duration=missing_duration,
    )


def cumulative_usage(
    run_lineage: list[str],
    limits: BudgetLimits,
) -> BudgetUsage:
    resolved = [str(Path(path).expanduser().resolve()) for path in run_lineage]
    if len(resolved) != len(set(resolved)):
        raise ValueError(f"迭代 lineage 含重复 run，拒绝重复计费：{resolved}")

    usage = BudgetUsage()
    for path in resolved:
        current = _run_usage(Path(path))
        usage.total_tokens += current.total_tokens
        usage.total_seconds += current.total_seconds
        usage.runs += current.runs
        usage.runs_missing_tokens += current.runs_missing_tokens
        usage.runs_missing_duration += current.runs_missing_duration
    usage.total_seconds = round(usage.total_seconds, 4)

    if limits.max_total_tokens is not None and usage.runs_missing_tokens:
        raise ValueError(
            f"配置了 token 预算，但 lineage 中 {usage.runs_missing_tokens} 个 run 没有 token usage；"
            "无法安全判断预算，拒绝继续"
        )
    if limits.max_total_seconds is not None and usage.runs_missing_duration:
        raise ValueError(
            f"配置了墙钟预算，但 lineage 中 {usage.runs_missing_duration} 个 run 没有 duration_ms；"
            "无法安全判断预算，拒绝继续"
        )
    return usage


def stop_decision(
    *,
    gate_pass: bool | None,
    iteration: int,
    max_iterations: int,
    limits: BudgetLimits,
    usage: BudgetUsage,
) -> tuple[StopReason | None, list[StopReason]]:
    """Return the primary stop reason and every triggered reason.

    Primary priority follows the product intent: reaching the quality goal is
    the most useful explanation, then resource exhaustion, then the safety cap.
    """
    triggered: list[StopReason] = []
    if gate_pass is True:
        triggered.append("gate_pass")
    budget_hit = (
        limits.max_total_tokens is not None
        and usage.total_tokens >= limits.max_total_tokens
    ) or (
        limits.max_total_seconds is not None
        and usage.total_seconds >= limits.max_total_seconds
    )
    if budget_hit:
        triggered.append("budget_exceeded")
    if iteration >= max_iterations:
        triggered.append("max_iterations")
    return (triggered[0] if triggered else None), triggered


def resolve_iteration_policy(
    *,
    run_dir: Path,
    snapshot: dict,
    previous_report_path: str | None,
    iteration_override: int | None,
    max_iterations_override: int | None,
    max_total_tokens: int | None,
    max_total_seconds: float | None,
) -> tuple[int, int, list[str], BudgetLimits, SuggestionReport | None]:
    previous = load_previous_report(previous_report_path) if previous_report_path else None
    suite_iteration = int(
        ((snapshot.get("suite") or {}).get("pipeline") or {}).get("iteration") or 1
    )
    iteration = iteration_override if iteration_override is not None else suite_iteration
    current = str(run_dir.resolve())

    if previous is None:
        max_iterations = max_iterations_override or 3
        return (
            iteration,
            max_iterations,
            [current],
            BudgetLimits(
                max_total_tokens=max_total_tokens,
                max_total_seconds=max_total_seconds,
            ),
            None,
        )

    if iteration != previous.iteration + 1:
        raise ValueError(
            f"当前 iteration={iteration}，但上一轮是 {previous.iteration}；"
            "跨轮必须严格递增 1"
        )
    max_iterations = (
        max_iterations_override
        if max_iterations_override is not None
        else previous.max_iterations
    )
    if max_iterations != previous.max_iterations:
        raise ValueError(
            f"不能在迭代中途修改 max_iterations："
            f"{previous.max_iterations} → {max_iterations}"
        )
    inherited = previous.budget_limits
    requested = BudgetLimits(
        max_total_tokens=max_total_tokens,
        max_total_seconds=max_total_seconds,
    )
    # 没传 CLI 值就继承；只要显式传了任一项，就必须与第一轮完全相同。
    if max_total_tokens is None and max_total_seconds is None:
        limits = inherited
    else:
        limits = requested
        if limits != inherited:
            raise ValueError(
                "不能在迭代中途修改预算上限："
                f"{inherited.model_dump()} → {limits.model_dump()}"
            )
    lineage = list(previous.run_lineage or [previous.source_run])
    if current in {str(Path(path).expanduser().resolve()) for path in lineage}:
        raise ValueError(f"当前 run 已在上一轮 lineage 中，拒绝重复计费：{current}")
    lineage.append(current)
    return iteration, max_iterations, lineage, limits, previous


def render_egress_manifest(
    *,
    target_skill: str,
    skill_text: str,
    failures: list[FailureEvidence],
    model: str,
    api_base_env: str,
    prompt: str,
    apply_requested: bool,
) -> str:
    case_ids = sorted({item.case_id for item in failures})
    endpoint = os.environ.get(api_base_env) or f"${api_base_env}（未设置）"
    calls = 2 if apply_requested else 1
    metadata = frontmatter.loads(skill_text).metadata
    lines = [
        "外发清单（尚未调用模型）：",
        f"  target: {target_skill}",
        f"  model: {model}",
        f"  endpoint: {endpoint}",
        f"  skill metadata: {json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)}",
        f"  full SKILL.md: {len(skill_text)} chars（建议 prompt 会发送全文）",
        f"  failure trajectories: {len(failures)} runs / {len(case_ids)} cases {case_ids}",
        f"  suggestion prompt: {len(prompt)} chars",
        f"  planned model calls: {calls}",
        "  trajectory preview:",
    ]
    lines.extend(
        f"    - {item.case_id} t{item.turn_index} r{item.repeat_index}: "
        f"{_truncate(item.raw_output, 240).replace(chr(10), ' ')}"
        for item in failures
    )
    return "\n".join(lines)


def resolve_source_model(snapshot: dict, args: argparse.Namespace) -> tuple[str, str, str, dict[str, object]]:
    resolved = snapshot.get("resolved_model") or {}
    params = dict(resolved.get("params") or {"temperature": 0})
    if not params:
        params = {"temperature": 0}
    return (
        args.model or resolved.get("model") or DEFAULT_MODEL,
        args.api_base_env or resolved.get("api_base_env") or DEFAULT_API_BASE_ENV,
        args.api_key_env or resolved.get("api_key_env") or DEFAULT_API_KEY_ENV,
        params,
    )


def resolve_skill_file(
    run_dir: str | Path,
    snapshot: dict,
    *,
    skill_file: str | None,
    skill_id: str | None,
) -> Path:
    if skill_file:
        return Path(skill_file).expanduser().resolve()

    inputs_root = Path(run_dir) / "inputs" / "skills"
    available = sorted(path.name for path in inputs_root.iterdir() if path.is_dir()) if inputs_root.is_dir() else []
    inferred_skill_id = skill_id
    versions = (snapshot.get("suite", {}).get("skills", {}) or {}).get("versions") or {}
    include = (snapshot.get("suite", {}).get("skills", {}) or {}).get("include") or []
    suite_id = str((snapshot.get("suite") or {}).get("suite_id") or "")
    skill_cfg = str(((snapshot.get("suite") or {}).get("skills") or {}).get("cfg") or "")
    if inferred_skill_id is None and len(versions) == 1:
        inferred_skill_id = next(iter(versions))
    if inferred_skill_id is None and len(available) == 1:
        inferred_skill_id = available[0]
    if inferred_skill_id is None and len(include) == 1:
        inferred_skill_id = include[0]
    if inferred_skill_id is None:
        hinted = [name for name in available or include if name and (name in suite_id or name in skill_cfg)]
        if len(set(hinted)) == 1:
            inferred_skill_id = hinted[0]
    if inferred_skill_id is None:
        raise ValueError(
            "无法仅凭 --run-dir 推断目标 skill：本次 run 含多个 skill 副本。\n"
            f"  → 可选副本：{available or '(none)'}\n"
            "  → 请补 --skill-id <skill> 或 --skill-file <path>"
        )
    candidate = inputs_root / inferred_skill_id / "SKILL.md"
    if candidate.is_file():
        return candidate.resolve()
    raise FileNotFoundError(
        f"run 快照里找不到 {inferred_skill_id!r} 的 SKILL.md：{candidate}\n"
        "  → 请补 --skill-file 指向真实文件，或确认本次 run 是否保留了 inputs/skills 副本"
    )


def collect_routing_failures(run_dir: str | Path) -> list[FailureEvidence]:
    """只收任务失败；runtime/network/harness 故障不拿来改 skill。"""
    directory = Path(run_dir)
    snapshot = yaml.safe_load(
        (directory / "config.snapshot.yaml").read_text(encoding="utf-8")
    )
    cases = {case.id: case for case in load_cases(_resolve_dataset(snapshot))}
    failures: list[FailureEvidence] = []
    for line in (directory / "runs.jsonl").read_text(encoding="utf-8").splitlines():
        result = RunResult.model_validate_json(line)
        if result.error_kind in {"runtime", "network", "harness"}:
            continue
        case = cases[result.case_id]
        if result.ok and result.selected_skills == case.expected_skills:
            continue
        raw = result.raw_output or result.reasoning or result.error or ""
        failures.append(
            FailureEvidence(
                case_id=result.case_id,
                repeat_index=result.repeat_index,
                expected_skills=case.expected_skills,
                selected_skills=result.selected_skills,
                raw_output=raw,
            )
        )
    return failures


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated at {limit} chars]"


def _load_grading_by_run(
    directory: Path, snapshot: dict
) -> dict[tuple[str, int, int], dict]:
    """judge 结果按 (case_id, repeat_index, turn_index) 索引。"""
    judge_id = ((snapshot.get("suite") or {}).get("scoring") or {}).get("judge") or {}
    candidates = [directory / f"grading.{judge_id['id']}.json"] if judge_id.get("id") else []
    candidates += sorted(directory.glob("grading.*.json"))
    for path in candidates:
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            (
                item["case_id"],
                item["repeat_index"],
                int(item.get("turn_index") or 1),
            ): item
            for item in payload.get("graded") or []
        }
    return {}


def _describe_full_failure(case, result: RunResult, grading: dict | None) -> tuple[FailureMetric, str] | None:
    """这次运行到底哪里没做到。全做到了返回 None。

    判定口径与 `score_full.score_run` 保持一致（产物存在且非空、tool 调没调、
    拒答题不许留文件），差别只是这里要把「差在哪」讲成人话喂给改进模型。
    """
    lines: list[str] = []
    turn = case.resolved_turn(result.turn_index)
    artifacts = [artifact.model_dump() for artifact in result.artifacts]
    called = {call.name for call in result.tool_calls}

    if not result.ok:
        lines.append(f"运行未成功：{result.error or '(无错误信息)'}")
    missing_artifacts = [p for p in turn.expect_artifacts if not artifact_hit(p, artifacts)]
    if missing_artifacts:
        lines.append(f"声明的产物没落下来（或为空/类型不符）：{missing_artifacts}")
    missing_tools = [t for t in turn.expect_tools if t not in called]
    if missing_tools:
        lines.append(f"声明必须调用的 tool 没调：{missing_tools}")
    if turn.forbid_artifacts and artifacts:
        lines.append(f"这是拒答题，不该留文件，却留下 {len(artifacts)} 个：{[a['path'] for a in artifacts]}")
    missing_workspace = [
        pattern
        for pattern in turn.expect_workspace_files
        if not any(fnmatch(path, pattern) for path in result.workspace_files)
    ]
    if missing_workspace:
        lines.append(f"本轮结束时 workspace 缺少应延续的文件：{missing_workspace}")

    failed_assertions = [
        item for item in ((grading or {}).get("expectations") or [])
        if not item.get("passed")
    ]
    if failed_assertions:
        lines.append("judge 判 failed 的断言：")
        lines += [f"  ✗ {item['text']}\n    judge 证据：{item.get('evidence', '')}"
                  for item in failed_assertions]
    low_dims = [d for d in ((grading or {}).get("dimensions") or []) if (d.get("score") or 0) < 0.6]
    if low_dims:
        lines.append("低分维度（< 0.6）：")
        lines += [f"  {d['dimension']}={d['score']} —— {d.get('evidence', '')}" for d in low_dims]

    if not lines:
        return None

    if artifacts:
        lines.append("本次产物内容：")
        for artifact in artifacts:
            excerpt = artifact.get("text_excerpt")
            body = _truncate(excerpt, DETAIL_EXCERPT_LIMIT) if excerpt else "(二进制或未采集，内容不可见)"
            lines.append(f"  --- {artifact['path']} ({artifact.get('mime_type')}, "
                         f"{artifact['size_bytes']} bytes) ---\n{body}")

    # done 失败优先归到 task_completion：产物/tool 都没做到时，语义断言过不了是必然结果，
    # 拿 assertion 当主因会让改进模型去改文风，而真正该改的是「必须写文件」这类硬要求。
    deterministic = bool(missing_artifacts or missing_tools or not result.ok
                         or missing_workspace
                         or (turn.forbid_artifacts and artifacts))
    return ("task_completion" if deterministic else "assertion"), "\n".join(lines)


def collect_full_failures(run_dir: str | Path) -> list[FailureEvidence]:
    """full eval 的失败证据：确定性断言 + judge 判定 + 产物内容。

    与 routing 一样，runtime/network/harness 故障不算 skill 的账（AGENTS.md ★★★ ⑥）。
    一条 run 最多产出一条证据 —— 同一道题被拆成多条，validate_suggestions 那边的
    「一个 case 只能归因到一个 cluster」就会跟自己打架。
    """
    directory = Path(run_dir)
    snapshot = yaml.safe_load((directory / "config.snapshot.yaml").read_text(encoding="utf-8"))
    cases = {case.id: case for case in load_cases(_resolve_dataset(snapshot))}
    grading = _load_grading_by_run(directory, snapshot)

    failures: list[FailureEvidence] = []
    for line in (directory / "runs.jsonl").read_text(encoding="utf-8").splitlines():
        result = RunResult.model_validate_json(line)
        if result.error_kind in {"runtime", "network", "harness"}:
            continue
        if result.status == "skipped":
            continue
        case = cases[result.case_id]
        described = _describe_full_failure(
            case,
            result,
            grading.get((result.case_id, result.repeat_index, result.turn_index)),
        )
        if described is None:
            continue
        metric, detail = described
        raw = result.final_answer or result.raw_output or result.reasoning or result.error or ""
        failures.append(
            FailureEvidence(
                case_id=result.case_id,
                repeat_index=result.repeat_index,
                turn_index=result.turn_index,
                metric=metric,
                expected_skills=case.expected_skills,
                selected_skills=result.loaded_skills or result.selected_skills,
                raw_output=_truncate(raw, RAW_OUTPUT_LIMIT),
                failure_detail=detail,
            )
        )
    return failures


def collect_failures(run_dir: str | Path, snapshot: dict) -> list[FailureEvidence]:
    """按 run 自己的 skills.mode 选收集口径，不靠调用方记得传对。"""
    mode = ((snapshot.get("suite") or {}).get("skills") or {}).get("mode", "routing_only")
    return collect_full_failures(run_dir) if mode == "full" else collect_routing_failures(run_dir)


def build_suggestion_prompt(
    *,
    target_skill: str,
    skill_text: str,
    failures: list[FailureEvidence],
) -> str:
    evidence = [item.model_dump(mode="json", exclude_none=True) for item in failures]
    metrics = sorted({item.metric for item in failures})
    case_ids = sorted({item.case_id for item in failures})
    return f"""[目标 skill]
{target_skill}

[当前 SKILL.md]
{skill_text}

[失败 trajectory；raw_output 是模型原文，failure_detail 是这次差在哪
（缺哪个产物 / 没调哪个 tool / judge 判 failed 的断言与证据 / 产物内容）]
{json.dumps(evidence, ensure_ascii=False, indent=2)}

[输出约束]
- 输出对象格式：{{"suggestions":[...]}}
- 每条 suggestion 字段只有 pattern,case_ids,metric,evidence,change
- metric 只能取本批失败里出现过的值：{metrics}
- 本批失败 case 共 {len(case_ids)} 个：{case_ids}
- **每个 case_id 只能出现在一条 suggestion 里**；同一个 case 拆进多条会被判为重复归因而整批拒收
- 因此本批最多给 {len(case_ids)} 条 suggestion；有共同根因就合并成更少的条数
- quote 必须从上面 JSON 里**该 case 自己的** raw_output 或 failure_detail 中**逐字复制**
  一整段连续文本：不许跨段拼接、不许省略中间、不许加省略号、不许改标点、
  不许引用 [当前 SKILL.md] 里的话。拿不准就复制短一点的一句。
  （空白和 markdown 标记不计较，文字本身必须一模一样。）
- change 必须指出改 SKILL.md 的哪个 frontmatter 字段或哪句正文，以及具体怎么改
- 只给建议，不输出完整改写后的 SKILL.md

[输出示例——照这个结构，evidence 永远是数组，不是字符串]
{{"suggestions": [
  {{"pattern": "一句话说清这组失败的共同根因",
    "case_ids": ["{case_ids[0]}"],
    "metric": "{metrics[0]}",
    "evidence": [{{"case_id": "{case_ids[0]}", "quote": "逐字复制的一段原文"}}],
    "change": "改哪里、怎么改"}}
]}}
"""


def _extract_json(text: str) -> str:
    value = text.strip().strip("`")
    if value.lower().startswith("json"):
        value = value[4:].lstrip()
    left, right = value.find("{"), value.rfind("}")
    if left == -1 or right == -1:
        raise ValueError("模型输出里没有 JSON object")
    return value[left : right + 1]


def call_litellm(
    *,
    model: str,
    api_base_env: str,
    api_key_env: str,
    params: dict[str, object],
    prompt: str,
    system_prompt: str = SYSTEM_PROMPT,
    json_mode: bool = True,
) -> str:
    """建议阶段要 JSON，改写阶段要 Markdown 全文 —— 同一个调用点，两种 response_format。"""
    import litellm

    response = quiet_completion(
        litellm,
        model=model,
        api_base=os.environ.get(api_base_env) or None,
        api_key=os.environ.get(api_key_env) or None,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        timeout=300,
        **({"response_format": {"type": "json_object"}} if json_mode else {}),
        **params,
    )
    return response.choices[0].message.content or ""


_QUOTE_NOISE = re.compile(r"[\s`*_~]+")


def _quotable(text: str) -> str:
    """比对引用时忽略空白与 markdown 强调符号。

    模型复制证据时会顺手把 `反引号` 和 **星号** 剥掉、把换行改成空格 —— 实测下来这是
    最常见的"quote 对不上"原因，而它并没有编造任何内容。要防的是**捏造事实**，
    不是要求它连 markdown 标记都一个不差；文字内容本身仍然必须逐字来自原文。
    """
    return _QUOTE_NOISE.sub("", text)


def validate_suggestions(
    suggestions: list[SuggestionCluster],
    failures: list[FailureEvidence],
) -> list[SuggestionCluster]:
    by_case: dict[str, list[str]] = {}
    for failure in failures:
        quotable = by_case.setdefault(failure.case_id, [])
        quotable.append(failure.raw_output)
        if failure.failure_detail:
            quotable.append(failure.failure_detail)
    failure_ids = set(by_case)
    referenced: list[str] = []
    issues: list[str] = []

    for index, suggestion in enumerate(suggestions, start=1):
        unknown = sorted(set(suggestion.case_ids) - failure_ids)
        if unknown:
            issues.append(f"suggestion {index} 引用了非失败 case：{unknown}")
        referenced.extend(suggestion.case_ids)
        for evidence in suggestion.evidence:
            if evidence.case_id not in suggestion.case_ids:
                issues.append(
                    f"suggestion {index} 的 evidence {evidence.case_id} 不在本 cluster"
                )
                continue
            outputs = by_case.get(evidence.case_id, [])
            if not any(_quotable(evidence.quote) in _quotable(output) for output in outputs):
                issues.append(
                    f"suggestion {index} 的 quote 不是 {evidence.case_id} 的"
                    "原文（raw_output / failure_detail）子串"
                )

    missing = sorted(failure_ids - set(referenced))
    if missing:
        issues.append(f"这些失败 case 未被任何建议覆盖：{missing}")
    duplicate_refs = sorted(
        case_id for case_id in set(referenced) if referenced.count(case_id) > 1
    )
    if duplicate_refs:
        issues.append(f"失败 case 被多个 cluster 重复归因：{duplicate_refs}")
    if len(failure_ids) > 1 and len(suggestions) >= len(failure_ids):
        issues.append("建议未聚类：suggestion 数不能等于或超过失败 case 数")
    if issues:
        raise ValueError("建议校验失败：\n- " + "\n- ".join(issues))
    return suggestions


def generate_suggestions(
    *,
    target_skill: str,
    skill_text: str,
    failures: list[FailureEvidence],
    model: str,
    api_base_env: str,
    api_key_env: str,
    params: dict[str, object],
    completion: Callable[..., str] = call_litellm,
) -> tuple[list[SuggestionCluster], str]:
    prompt = build_suggestion_prompt(
        target_skill=target_skill,
        skill_text=skill_text,
        failures=failures,
    )
    raw = completion(
        model=model,
        api_base_env=api_base_env,
        api_key_env=api_key_env,
        params=params,
        prompt=prompt,
    )
    batch = SuggestedBatch.model_validate_json(_extract_json(raw))
    return validate_suggestions(batch.suggestions, failures), prompt


APPLY_SYSTEM_PROMPT = """你是 Agent Skill 的改写器。
按给定的改进建议改写 SKILL.md，只做建议里点名的改动。
只返回改写后的完整 SKILL.md 全文（含 YAML frontmatter），不要解释、不要代码块围栏。"""


def source_skill_dir(snapshot: dict, skill_id: str) -> Path:
    """被测 skill 在仓库里的**源目录**（`subjects/<skill-id>/`）。

    run 归档里的 `inputs/skills/<id>/SKILL.md` 是当次输入的只读副本，改它没有任何意义
    —— 下一次 run 读的还是源目录。
    """
    root = (snapshot.get("suite", {}).get("skills", {}) or {}).get("dir") or "subjects"
    directory = Path(root)
    if not directory.is_absolute():
        directory = ROOT / directory
    return directory / skill_id


def next_version(skill_dir: Path) -> str:
    """现有版本里最大的那个 +1。没有任何版本目录时从 v2 起（v1 是源版本的默认名）。"""
    existing = [
        version_order(path.name) for path in skill_dir.iterdir()
        if path.is_dir() and VERSION_DIR.fullmatch(path.name)
    ] if skill_dir.is_dir() else []
    return f"v{max(existing)[0] + 1}" if existing else "v2"


def build_apply_prompt(*, skill_text: str, suggestions: list[SuggestionCluster]) -> str:
    payload = [item.model_dump(mode="json") for item in suggestions]
    return f"""[当前 SKILL.md]
{skill_text}

[要落实的改进建议]
{json.dumps(payload, ensure_ascii=False, indent=2)}

[改写约束]
- 只做上面建议里点名的改动，不要顺手重写其他部分
- 保留 frontmatter 的 name 字段原样不变（它是 skill_id，改了就换了一个 skill）
- 输出改写后的**完整** SKILL.md 全文，从 `---` 开始
- 不要输出解释、不要用 ``` 围栏包裹
"""


def validate_applied_skill(new_text: str, old_text: str) -> str:
    """改写结果必须仍是一个合法 skill，且还是同一个 skill。"""
    text = new_text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    text = text.strip() + "\n"

    old, new = frontmatter.loads(old_text), frontmatter.loads(text)
    issues = []
    if not text.startswith("---"):
        issues.append("输出不是以 YAML frontmatter 开头")
    if new.metadata.get("name") != old.metadata.get("name"):
        issues.append(
            f"name 被改了：{old.metadata.get('name')!r} → {new.metadata.get('name')!r}；"
            "改 name 等于换了一个 skill，对照实验就不成立了"
        )
    if not new.metadata.get("description"):
        issues.append("frontmatter 缺 description")
    if not new.content.strip():
        issues.append("正文是空的")
    if text.strip() == old_text.strip():
        issues.append("改写结果与原文逐字相同，等于什么都没改")
    if issues:
        raise ValueError("改写结果校验失败：\n- " + "\n- ".join(issues))
    return text


def apply_suggestions(
    *,
    skill_text: str,
    suggestions: list[SuggestionCluster],
    model: str,
    api_base_env: str,
    api_key_env: str,
    params: dict[str, object],
    completion: Callable[..., str] = call_litellm,
) -> str:
    raw = completion(
        model=model,
        api_base_env=api_base_env,
        api_key_env=api_key_env,
        params=params,
        prompt=build_apply_prompt(skill_text=skill_text, suggestions=suggestions),
        system_prompt=APPLY_SYSTEM_PROMPT,
        json_mode=False,
    )
    return validate_applied_skill(raw, skill_text)


def write_new_version(
    *,
    skill_dir: Path,
    source_version: str,
    version: str,
    skill_text: str,
    provenance: str,
) -> Path:
    """把新正文落成 `subjects/<skill-id>/<version>/`，其余附件从源版本整目录复制。

    只写 SKILL.md 是不够的：skill 常带 references/ 和 scripts/，漏掉它们新版本会
    在运行时缺文件，而这种失败看起来像「改坏了」，其实是没复制全。
    """
    target = skill_dir / version
    if target.exists():
        raise FileExistsError(f"版本目录已存在，拒绝覆盖：{target}")
    source = skill_dir / source_version
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        target.mkdir(parents=True)
    (target / "SKILL.md").write_text(skill_text, encoding="utf-8")
    (target / "PROVENANCE.md").write_text(provenance, encoding="utf-8")
    return target


def build_reeval_suite(
    *,
    snapshot: dict,
    skill_id: str,
    version: str,
) -> dict:
    """同题复验 suite：题集、模型、runtime、judge 全部照抄，**只换被测版本**。

    唯一变量原则（AGENTS.md §9.2）：复验和上一轮之间只能差 skill 版本，差第二个
    东西这条 delta 就归因不到改动上了。
    """
    suite = json.loads(json.dumps(snapshot["suite"]))  # 深拷贝，别改到调用方的快照
    skills = suite.setdefault("skills", {})
    skills.setdefault("versions", {})[skill_id] = version
    skills.pop("overlay", None)     # 早期快照留下的废弃字段，现在的 suite 契约不认
    # 旧快照可能没有 target；复验本来就只改这一个 skill，顺手把显式归属补齐。
    # target 是 provenance，不影响模型输入，也不进运行 config_hash。
    skills["target"] = [skill_id]
    base_cfg = str(skills.get("cfg") or skill_id)
    skills["cfg"] = f"{base_cfg}-{version}" if not base_cfg.endswith(version) else base_cfg
    suite["description"] = (
        f"{suite.get('description', '')}｜同题复验：{skill_id} {version}"
    ).strip("｜")
    suite.setdefault("pipeline", {})["iteration"] = int(
        (snapshot["suite"].get("pipeline") or {}).get("iteration") or 1
    ) + 1
    return suite


def main() -> None:
    parser = argparse.ArgumentParser(
        description="runs.jsonl 失败原文 → 聚类改进建议；--apply 才会写出新版本 skill"
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--skill-file", help="默认从 run 快照推断；多 skill run 可用 --skill-id 缩小范围")
    parser.add_argument("--skill-id", help="当 run 含多个 skill 副本时，指定要分析哪个")
    parser.add_argument(
        "--iteration",
        type=int,
        help="默认读取 suite.pipeline.iteration；显式值会写入报告",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        help="首轮默认 3；后续轮从 --previous-report 继承且不得中途修改",
    )
    parser.add_argument(
        "--previous-report",
        help="上一轮 suggestions.json；用于校验 iteration 并累计 lineage/预算",
    )
    parser.add_argument("--max-total-tokens", type=int, help="整条迭代 lineage 的 token 上限")
    parser.add_argument("--max-total-seconds", type=float, help="整条迭代 lineage 的墙钟秒数上限")
    parser.add_argument("--output")
    parser.add_argument("--model", help="默认继承 source run 的 resolved_model.model")
    parser.add_argument("--api-base-env", help="默认继承 source run 的 resolved_model.api_base_env")
    parser.add_argument("--api-key-env", help="默认继承 source run 的 resolved_model.api_key_env")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="把建议落成 subjects/<skill-id>/v<N+1>/ 并生成同题复验 suite；"
             "只新增版本目录，绝不改写源版本",
    )
    parser.add_argument(
        "--confirm-egress",
        action="store_true",
        help="确认把打印出的 skill 文本与失败 trajectory 发送到建议模型",
    )
    args = parser.parse_args()
    if args.iteration is not None and args.iteration < 1:
        parser.error("--iteration 必须 >= 1")
    if args.max_iterations is not None and args.max_iterations < 1:
        parser.error("--max-iterations 必须 >= 1")
    if args.max_total_tokens is not None and args.max_total_tokens < 1:
        parser.error("--max-total-tokens 必须 >= 1")
    if args.max_total_seconds is not None and args.max_total_seconds <= 0:
        parser.error("--max-total-seconds 必须 > 0")

    load_dotenv(ROOT / ".env")
    run_dir = Path(args.run_dir).resolve()
    snapshot = load_snapshot(run_dir)
    scores = load_scores(run_dir)
    skill_file = resolve_skill_file(
        run_dir,
        snapshot,
        skill_file=args.skill_file,
        skill_id=args.skill_id,
    )
    skill_text = skill_file.read_text(encoding="utf-8")
    target_skill = frontmatter.loads(skill_text).metadata.get("name") or skill_file.parent.name
    failures = collect_failures(run_dir, snapshot)
    model_name, api_base_env, api_key_env, params = resolve_source_model(snapshot, args)
    iteration, max_iterations, run_lineage, budget_limits, previous_report = (
        resolve_iteration_policy(
            run_dir=run_dir,
            snapshot=snapshot,
            previous_report_path=args.previous_report,
            iteration_override=args.iteration,
            max_iterations_override=args.max_iterations,
            max_total_tokens=args.max_total_tokens,
            max_total_seconds=args.max_total_seconds,
        )
    )
    if previous_report is not None and previous_report.target_skill != target_skill:
        raise ValueError(
            f"上一轮 target_skill={previous_report.target_skill!r}，"
            f"当前是 {target_skill!r}；拒绝串联不同 skill 的预算与停止条件"
        )
    budget_usage = cumulative_usage(run_lineage, budget_limits)
    stop_reason, triggered_stop_reasons = stop_decision(
        gate_pass=scores.get("gate_pass"),
        iteration=iteration,
        max_iterations=max_iterations,
        limits=budget_limits,
        usage=budget_usage,
    )

    output = (
        Path(args.output).resolve()
        if args.output
        else run_dir / "improvements" / f"round-{iteration:02d}" / "suggestions.json"
    )
    if output.exists():
        raise FileExistsError(f"拒绝覆盖已有建议轮次：{output}")

    prompt = None
    suggestions: list[SuggestionCluster] = []
    model: str | None = model_name
    if stop_reason is not None:
        model = None
    elif not failures:
        # gate 明确没过、但也没有任何可归因给 skill 的失败时，不能凭空生成建议。
        model = None
    else:
        prompt = build_suggestion_prompt(
            target_skill=target_skill,
            skill_text=skill_text,
            failures=failures,
        )
        print(render_egress_manifest(
            target_skill=target_skill,
            skill_text=skill_text,
            failures=failures,
            model=model_name,
            api_base_env=api_base_env,
            prompt=prompt,
            apply_requested=args.apply,
        ))
        if not args.confirm_egress:
            print(
                "\n未提供 --confirm-egress：没有调用任何模型，没有写任何文件。"
                "\n审核清单后，用同一命令加 --confirm-egress 才会继续。"
            )
            return
        suggestions, _ = generate_suggestions(
            target_skill=target_skill,
            skill_text=skill_text,
            failures=failures,
            model=model_name,
            api_base_env=api_base_env,
            api_key_env=api_key_env,
            params=params,
            # 显式传，不吃默认参数：默认值在 def 那一刻就绑死了模块函数对象，
            # 测试 monkeypatch 模块属性根本盖不住它，冒烟测试会真的打出去（§29.26）。
            completion=call_litellm,
        )

    applied: dict[str, str] = {}
    if args.apply and suggestions:
        skill_id = args.skill_id or skill_file.parent.name
        skill_dir = source_skill_dir(snapshot, skill_id)
        source_version = (
            (snapshot.get("suite", {}).get("skills", {}) or {}).get("versions", {}) or {}
        ).get(skill_id) or "v1"
        version = next_version(skill_dir)
        new_text = apply_suggestions(
            skill_text=skill_text,
            suggestions=suggestions,
            model=model_name,
            api_base_env=api_base_env,
            api_key_env=api_key_env,
            params=params,
            completion=call_litellm,
        )
        target = write_new_version(
            skill_dir=skill_dir,
            source_version=source_version,
            version=version,
            skill_text=new_text,
            provenance=(
                f"# {skill_id} {version}\n\n"
                f"由 `workflows.suggest --apply` 生成，不是人手写的。\n\n"
                f"- 源版本：{source_version}\n"
                f"- 依据的 run：{run_dir}\n"
                f"- 依据的建议：{output}\n"
                f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            ),
        )
        reeval_suite = build_reeval_suite(snapshot=snapshot, skill_id=skill_id, version=version)
        suite_path = output.parent / "reeval.suite.yaml"
        suite_path.parent.mkdir(parents=True, exist_ok=True)
        suite_path.write_text(
            yaml.safe_dump(reeval_suite, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        applied = {
            "applied_version": version,
            "applied_skill_path": str(target / "SKILL.md"),
            "reeval_suite_path": str(suite_path),
            "source_skill_hash": _sha(skill_text),
            "applied_skill_hash": _sha(new_text),
        }

    report = SuggestionReport(
        version=SUGGESTER_VERSION,
        source_run=str(run_dir),
        target_skill=target_skill,
        iteration=iteration,
        max_iterations=max_iterations,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        model=model,
        prompt_hash=_sha(SYSTEM_PROMPT + "\n" + prompt) if prompt else None,
        failures=failures,
        suggestions=suggestions,
        stop_reason=stop_reason,
        triggered_stop_reasons=triggered_stop_reasons,
        run_lineage=run_lineage,
        previous_report=(
            str(Path(args.previous_report).expanduser().resolve())
            if args.previous_report else None
        ),
        budget_limits=budget_limits,
        budget_usage=budget_usage,
        gate_pass=scores.get("gate_pass"),
        apply_status="applied" if applied else "not_requested",
        **applied,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"建议报告 → {output}")
    if applied:
        print(f"新版本 skill → {applied['applied_skill_path']}（源版本原样保留）")
        print(f"同题复验 suite → {applied['reeval_suite_path']}")
        print("下一步（只换了 skill 版本，题集/模型/judge 全部照抄上一轮）：")
        print(f"  .venv/bin/python -m pipeline run --suite {applied['reeval_suite_path']} "
              f"--stages run,grade,score --confirm --confirm-egress")
        print("复验评分完成后，判断停止条件并累计预算：")
        print(
            "  .venv/bin/python -m workflows.suggest --run-dir <复验run目录> "
            f"--previous-report {output}"
        )
    elif args.apply:
        print("没有可落实的建议（已触发停止条件或没有 skill 失败），未写任何版本目录。")
    else:
        print("apply_status=not_requested：没有修改任何 SKILL.md；确认后才进入 --apply 阶段。")


if __name__ == "__main__":
    main()
