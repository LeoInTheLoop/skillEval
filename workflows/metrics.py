"""跨 repeat 的统计、效率维度与稳定性诊断。

结构搬自 Anthropic skill-creator 的 `scripts/aggregate_benchmark.py`（AGENTS.md §4.2
允许把它当参考实现，但它不得成为唯一 Evaluator —— 所以只搬**算法**，不搬它的目录
布局和 CLI）。`score_routing.py` 与 `score_full.py` 共用本模块。

**这里一行 LLM 判定都没有。** 语义判定在 `grade.py`，两者分开的理由：本模块的数字
换台机器重算必须一模一样；grade.py 的数字取决于 judge 模型，天然不可复现到位。

三件事：
  1. `stats()`   —— mean ± stddev / min / max。原来只报 mean，repeat 之间抖多厉害看不见。
  2. `efficiency()` —— time / tokens / tool_calls / errors。数据 runs.jsonl 里早就有了，
                       只是从没进过 scores。
  3. `flaky_cases()` / `non_discriminating()` —— 题本身的质量诊断（对应 skill-creator
     的 analyzer：同题跨 repeat 结果不一致 = 可能是 flaky 题；有无 skill 都同样通过
     = 这道题不判别 skill，留着只会稀释指标）。
"""
from __future__ import annotations

import statistics
from typing import Any, Iterable, Mapping, Sequence

# 效率维度的键。固定住是为了让 scores.json 的形状稳定 —— 少一个键，
# compare_runs 那边就整列消失，看起来像「这次没测」。
EFFICIENCY_KEYS = ("time_seconds", "tokens", "tool_calls", "errors")


def stats(values: Sequence[float]) -> dict[str, float] | None:
    """mean / stddev / min / max。空序列返回 None（N/A，不是 0）。

    stddev 用样本标准差（n-1）；单点样本没有离散度，返回 0.0 而不是报错。
    """
    numbers = [float(v) for v in values if v is not None]
    if not numbers:
        return None
    return {
        "mean": round(statistics.fmean(numbers), 4),
        "stddev": round(statistics.stdev(numbers), 4) if len(numbers) > 1 else 0.0,
        "min": round(min(numbers), 4),
        "max": round(max(numbers), 4),
    }


def _tokens(usage: Mapping[str, Any]) -> float | None:
    """两个 adapter 的 usage 形状不同，这里收敛成一个数。

    openclaw 直接给 total_tokens；litellm 只给 input/output，得自己加。
    一个都没有就返回 None —— 别拿 0 冒充「这次没花 token」。
    """
    total = usage.get("total_tokens")
    if total is not None:
        return float(total)
    parts = [usage.get("input_tokens"), usage.get("output_tokens")]
    present = [float(p) for p in parts if p is not None]
    return sum(present) if present else None


def _tool_calls(run: Mapping[str, Any]) -> float | None:
    """本次运行的 tool 调用总次数。

    ⚠️ 不能用 `len(tool_calls)` —— 那是「用过几种 tool」，不是「调了几次」。
    OpenClaw 在 usage.tool_calls_total 里给整轮总数，逐次粒度的 runtime 则把次数
    放在每个 ToolCall.count 上；count 为 None 表示该 runtime 只报告用过、没报次数，
    此时按 1 次下限计（宁可低估，不要把 None 当 0 让整列失真）。
    """
    usage = run.get("usage") or {}
    total = usage.get("tool_calls_total")
    if total is not None:
        return float(total)
    calls = run.get("tool_calls")
    if not calls:
        return 0.0 if calls == [] else None
    return float(sum(c.get("count") or 1 for c in calls))


def efficiency(runs: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float] | None]:
    """成本/效率维度的逐 run 取值 → 统计量。

    和质量维度分开报：一个 skill 把准确率从 80% 拉到 100% 却让耗时翻三倍，
    只看 pass rate 是看不出来的。
    """
    rows = list(runs)
    collected: dict[str, list[float]] = {k: [] for k in EFFICIENCY_KEYS}
    for run in rows:
        duration = run.get("duration_ms")
        if duration is not None:
            collected["time_seconds"].append(float(duration) / 1000.0)
        tokens = _tokens(run.get("usage") or {})
        if tokens is not None:
            collected["tokens"].append(tokens)
        calls = _tool_calls(run)
        if calls is not None:
            collected["tool_calls"].append(calls)
        collected["errors"].append(1.0 if run.get("error_kind") else 0.0)
    return {key: stats(values) for key, values in collected.items()}


def flaky_cases(
    rows: Iterable[Mapping[str, Any]],
    *,
    key: str,
    case_key: str = "case_id",
) -> list[dict[str, Any]]:
    """同一道题跨 repeat 结果不一致的，列出来。

    这类题会让 delta 变得没法解释：分数动了，你分不清是 skill 改好了还是这题本来就飘。
    只有 repeat ≥ 2 的题才可能被判定，单次运行不下结论。
    """
    grouped: dict[str, list[bool]] = {}
    for row in rows:
        grouped.setdefault(str(row[case_key]), []).append(bool(row[key]))
    out = []
    for case_id, values in sorted(grouped.items()):
        if len(values) > 1 and len(set(values)) > 1:
            out.append({
                "case_id": case_id,
                "passed": sum(values),
                "runs": len(values),
                "pass_rate": round(sum(values) / len(values), 4),
            })
    return out


def _complete_repeat_groups(
    rows: Iterable[Mapping[str, Any]],
    *,
    key: str,
    k: int,
    case_key: str = "case_id",
    repeat_key: str = "repeat",
) -> list[list[bool]]:
    """收集恰好完成 k 个独立 repeat 的 case。

    系统失败或缺失 repeat 不应被当成 task failure，也不能悄悄当成成功；
    因此只有完整的 case 才进入 pass@k / pass^k 分母。
    """
    if k < 1:
        raise ValueError("k 必须 >= 1")
    grouped: dict[str, dict[Any, bool]] = {}
    for row in rows:
        case_id = str(row.get(case_key))
        repeat = row.get(repeat_key)
        grouped.setdefault(case_id, {})[repeat] = bool(row.get(key))
    return [list(repeats.values()) for repeats in grouped.values() if len(repeats) == k]


def pass_at_k(
    rows: Iterable[Mapping[str, Any]],
    *,
    key: str,
    k: int,
    case_key: str = "case_id",
    repeat_key: str = "repeat",
) -> float | None:
    """k 次运行中至少成功一次的 case 比例（pass@k）。"""
    groups = _complete_repeat_groups(
        rows, key=key, k=k, case_key=case_key, repeat_key=repeat_key
    )
    return round(sum(any(values) for values in groups) / len(groups), 4) if groups else None


def pass_all_k(
    rows: Iterable[Mapping[str, Any]],
    *,
    key: str,
    k: int,
    case_key: str = "case_id",
    repeat_key: str = "repeat",
) -> float | None:
    """k 次运行全部成功的 case 比例（pass^k，稳定性指标）。"""
    groups = _complete_repeat_groups(
        rows, key=key, k=k, case_key=case_key, repeat_key=repeat_key
    )
    return round(sum(all(values) for values in groups) / len(groups), 4) if groups else None


def non_discriminating(
    baseline: Mapping[str, float],
    treatment: Mapping[str, float],
    *,
    threshold: float = 1.0,
) -> list[str]:
    """两个 skillcfg 下都满分通过的题 —— 它们不判别 skill 有没有用。

    输入是 {case_id: 通过率}。留着这种题不算错，但它们只会把总分往上顶，
    掩盖真正有区分度的那几题（skill-creator 的 analyzer 把这个列为头号检查项）。
    只在**两边都有**这道题时才判，避免拿 none 基线换掉的数据集来误判。
    """
    return sorted(
        case_id
        for case_id, base in baseline.items()
        if case_id in treatment
        and base >= threshold
        and treatment[case_id] >= threshold
    )


def format_stats(value: dict[str, float] | None, *, percent: bool = False, unit: str = "") -> str:
    """统一的 `mean ± stddev` 展示。None 一律显示成 N/A，不显示成 0。"""
    if value is None:
        return "N/A"
    if percent:
        return f"{value['mean']:.1%} ± {value['stddev']:.1%}"
    return f"{value['mean']:.1f}{unit} ± {value['stddev']:.1f}{unit}"
