"""N10：跨 run 对比（No-Skill vs V1 vs V2、模型 A vs 模型 B）。

用法：
  python -m workflows.compare_runs outputs/a outputs/b [outputs/c ...]
  python -m workflows.compare_runs outputs/a/scores/rubric-v1.json outputs/a/scores/rubric-v2.json
  python -m workflows.compare_runs --glob 'outputs/ctxweave_*'

按 AGENTS.md §22.1 的比较维度出表。**先查配置差异**：只有目标那一项不同才是有效对照，
其余字段都相同才谈得上归因（§9.2）。
"""
from __future__ import annotations

import argparse
import glob as globlib
import json
from pathlib import Path

import pandas as pd
import yaml

from workflows import dimensions

ROOT = Path(__file__).parent.parent
# 路由的 + full 的都列出来：一个 run 只会有其中一组，另一组整列为 None 会被过滤掉。
_DETERMINISTIC = ["exact_set_match", "top1", "no_skill_rejection", "false_activation",
                  "type_pos", "type_amb", "type_multi", "type_rej",
                  "task_completion", "turn_completion", "artifact_hit", "tool_hit",
                  "session_continuity", "context_retention", "file_state_continuity",
                  "critical_miss", "skill_load", "artifact_correctness"]
# judge 打的分。这些跨 judge 不可比，所以下面要单独标出来。
# trajectory 第一版也是 judge 量具；即使未来换成 deterministic evaluator，
# 没有对应 judge 信息时仍能正常显示，旧 run 则得到 N/A。
_JUDGED = ["assertion_pass_rate", "final_answer_quality",
           "tool_selection", "argument_correctness", "order_correctness",
           "state_persistence", "verification_rate"] + sorted(dimensions.STANDARD_DIMENSIONS)
METRICS = _DETERMINISTIC + _JUDGED
EFFICIENCY = ("time_seconds", "tokens", "tool_calls", "errors")


def _compare_slug(columns) -> str:
    """把参与对比的 run 列名压成一个安全、稳定、可读的文件名片段。"""
    import re

    parts = [re.sub(r"[^A-Za-z0-9_.+-]", "-", str(col)) for col in columns]
    slug = "__vs__".join(parts)
    return slug[:120] if len(slug) > 120 else slug


def load_run(path: Path) -> dict:
    """读取普通 run 目录或 rescore 的版本化 scores JSON。"""
    score_path = path if path.is_file() else path / "scores.json"
    scores = json.loads(score_path.read_text(encoding="utf-8"))
    rescore = scores.get("rescore") or {}
    d = Path(rescore.get("source_run_dir") or path)
    snap_p = d / "config.snapshot.yaml"
    snap = yaml.safe_load(snap_p.read_text(encoding="utf-8")) if snap_p.exists() else {}
    suite = snap.get("suite", {})
    judge = scores.get("judge") or {}
    return {
        "dir": (d.name if score_path.name == "scores.json"
                else f"{d.name}/{score_path.parent.name}/{score_path.name}"),
        "suite_id": suite.get("suite_id"),
        "model": scores.get("model"),
        "skillcfg": suite.get("skills", {}).get("cfg"),
        "dataset": Path(suite.get("dataset", "?")).name,
        "dataset_hash": snap.get("dataset_hash"),
        "runtime": suite.get("runtime"),
        "repeats": suite.get("repeats", 3),
        "parallelism": suite.get("parallelism", 1),
        "config_hash": scores.get("config_hash"),
        # 判 assertion 的那把尺子。没跑 grade.py 就是 None ——
        # 那种情况下 assertion_pass_rate 整列为空，判定逻辑自己会跳过它。
        "judge": judge.get("id"),
        "judge_model": judge.get("model"),
        "judge_prompt_hash": judge.get("system_prompt_hash"),
        # 维度 id → rubric 版本。改了 rubric 就是换了尺子，哪怕模型没换
        "judge_dimensions": json.dumps(judge.get("dimensions") or {}, sort_keys=True),
        "trajectory_judge": (scores.get("trajectory_judge") or {}).get("id"),
        "trajectory_judge_model": (scores.get("trajectory_judge") or {}).get("model"),
        "trajectory_judge_prompt_hash": (
            scores.get("trajectory_judge") or {}
        ).get("system_prompt_hash"),
        "grading_hash": rescore.get("grading_hash"),
        "source_runs_sha256": rescore.get("source_runs_sha256"),
        "n_skills": len(snap.get("skill_catalog") or snap.get("skills") or {}),
        "gate_pass": scores.get("gate_pass"),
        "scores": scores.get("scores", {}),
        # score_* already records mean/stddev/min/max.  Pull the means into the
        # cross-run table; old results simply show N/A instead of fake zeroes.
        "efficiency": scores.get("efficiency", {}),
    }


# 对比维度：这几项本来就该不同，那正是我们要比的东西
_AXES = ("suite_id", "skillcfg")
# 随对比维度联动的：No-Skill 基线目标 skill 缺席，gold 和目录规模必然跟着变，不算污染
_DERIVED = ("dataset", "dataset_hash", "n_skills")
# 必须钉死的：这几项一旦不同，delta 就混了两个原因，归因失效（AGENTS.md §9.2）
_FIXED = ("model", "runtime", "repeats", "parallelism")
# judge 是量具，不是实验对象。换了它，**只有 assertion_pass_rate 这一行**不可比，
# 其余确定性维度完全不受影响 —— 所以单独一类，不并进 _FIXED。
# 把它报成全局污染会让人以为整张表都不能信，那种过度警告的下场是所有警告都被忽略。
_JUDGE = ("judge", "judge_model", "judge_prompt_hash", "judge_dimensions",
          "trajectory_judge", "trajectory_judge_model", "trajectory_judge_prompt_hash",
          "grading_hash")
_LEGACY_DEFAULTS = {"repeats": 3, "parallelism": 1}


def _value(run: dict, field: str):
    """旧 run 没有后来新增的 suite 字段时，按当时的真实默认语义读取。"""
    return run.get(field, _LEGACY_DEFAULTS.get(field))


def _fmt(runs: list[dict], field: str) -> str:
    return f"{field}: " + " | ".join(f"{_short(r)}={_value(r, field)}" for r in runs)


def _short(r: dict) -> str:
    """Unique comparison column: model / skillcfg / execution archive.

    The old layout ended at skillcfg; the archive layout deliberately permits
    several executions with identical model + cfg.  Keeping the leaf avoids
    pandas silently producing duplicate columns for a stability/cost compare.
    """
    return f"{r.get('model') or '?'}/{r.get('skillcfg') or '?'}/{r.get('dir') or '?'}"


def diff_configs(runs: list[dict]) -> tuple[list[str], list[str], list[str], list[str]]:
    """返回 (对比维度, 联动变化, 污染项, 尺子差异)。

    污染项非空 = 整张表的 delta 都归因不了；
    尺子差异非空 = 只有 assertion_pass_rate 那一行不可比，其余照常看。
    """
    def changed(fields):
        return [
            _fmt(runs, field)
            for field in fields
            if len({_value(run, field) for run in runs}) > 1
        ]
    return changed(_AXES), changed(_DERIVED), changed(_FIXED), changed(_JUDGE)


def efficiency_means(runs: list[dict]) -> dict[str, list[float | None]]:
    """Extract comparable per-run efficiency means without treating missing as 0."""
    return {
        metric: [
            (run.get("efficiency", {}).get(metric) or {}).get("mean")
            for run in runs
        ]
        for metric in EFFICIENCY
    }


def same_execution_facts(runs: list[dict]) -> bool:
    """版本化 rescore 只有 source runs hash 全部相同才算「同一执行换尺子」。"""
    hashes = [run.get("source_runs_sha256") for run in runs]
    return bool(hashes and all(hashes) and len(set(hashes)) == 1)


def _format_efficiency(metric: str, value: float | None) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if metric == "time_seconds":
        return f"{value:.2f}s"
    if metric == "tokens":
        return f"{value:,.0f}"
    if metric == "errors":
        return f"{value:.1%}"
    return f"{value:.2f}"


def _format_efficiency_delta(metric: str, value: float | None) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    if metric == "time_seconds":
        return f"{value:+.2f}s"
    if metric == "tokens":
        return f"{value:+,.0f}"
    if metric == "errors":
        return f"{value:+.1%}"
    return f"{value:+.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*", help="run 目录，按你想要的顺序给（基线放第一个）")
    ap.add_argument("--glob", help="用通配符选目录，如 'outputs/ctxweave_*'")
    args = ap.parse_args()

    paths = [Path(p) for p in (sorted(globlib.glob(args.glob)) if args.glob else args.dirs)]
    paths = [p for p in paths if p.is_file() or (p / "scores.json").exists()]
    if len(paths) < 2:
        raise SystemExit("至少要两个已评分的 run 目录（缺 scores.json 就先跑 score_routing.py）")

    runs = [load_run(p) for p in paths]
    same_facts = same_execution_facts(runs)

    print("对比的 run：")
    for r in runs:
        gate = {True: "PASS", False: "FAIL", None: "—"}[r["gate_pass"]]
        print(f"  {r['dir']:<52} skills={r['n_skills']} gate={gate}")

    axes, derived, polluted, judges = diff_configs(runs)
    print("\n配置差异：")
    if same_facts:
        print("  [同一执行] source_runs_sha256 完全相同；这里只是在比较量具，不是 skill delta")
    for d in axes:
        print(f"  [对比维度] {d}")
    for d in derived:
        print(f"  [随之联动] {d}")
    for d in polluted:
        print(f"  [⚠️ 污染]  {d}")
    for d in judges:
        print(f"  [⚠️ 尺子不同] {d}")
    if not (axes or derived or polluted or judges):
        print(
            "  （同一执行 + 同一量具 —— 这是 deterministic 可复现性核对）"
            if same_facts else
            "  （完全相同 —— 这几个 run 只差重复运行带来的随机性）"
        )
    if polluted:
        print("  ⚠️ 上述项本应钉死；它们变了，delta 就混了多个原因，无法归因到单一因素")
    if judges:
        print(f"  ⚠️ 这几个 run 的语义分不是同一把尺子判的 —— **只有 judge 打分的那几行**"
              f"跨 run 不可比（确定性维度是代码算的，不受影响）。"
              f"\n     要比那几行，先用同一个 --judge-id 把它们各自重判一遍。")
    if same_facts and judges:
        print("  ⚠️ 同一执行上的 grading_hash/ judge 差异表示换了尺子；"
              "确定性分可直接核对，语义分只展示、不解释为 skill 变化。")

    # 指标表 + 相对第一个 run 的 delta（每列一个独立的 Δ 列，别共用列名互相覆盖）
    rows = {m: [r["scores"].get(m) for r in runs] for m in METRICS}
    rows = {m: v for m, v in rows.items() if not all(x is None for x in v)}
    df = pd.DataFrame(rows, index=[_short(r) for r in runs]).T

    base = df.columns[0]
    disp = pd.DataFrame(index=df.index)
    disp[base] = df[base].map(lambda v: "—" if pd.isna(v) else f"{v:.1%}")
    for col in df.columns[1:]:
        disp[col] = df[col].map(lambda v: "—" if pd.isna(v) else f"{v:.1%}")
        disp[f"Δ({col})"] = (df[col] - df[base]).map(
            lambda v: "—" if pd.isna(v) else f"{v * 100:+.1f}pp")
    # 尺子不同就在**每一个 judge 打分的行**上打标记：光在上面印一句话，
    # 看表的人照样会径直去读那个 delta
    if judges:
        disp = disp.rename(index={m: f"{m} ⚠️尺子不同，不可比"
                                  for m in _JUDGED if m in disp.index})
    print(f"\n指标（基线 = {base}）：")
    print(disp.to_string())

    # Cost/latency use absolute deltas, not percentage-point deltas.  A skill
    # that improves accuracy but doubles tokens must be visible in the same
    # compare output, while missing telemetry remains N/A.
    eff_raw = pd.DataFrame(efficiency_means(runs), index=[_short(r) for r in runs]).T
    eff_raw = eff_raw.dropna(how="all")
    eff_disp = pd.DataFrame(index=eff_raw.index)
    if not eff_raw.empty:
        eff_base = eff_raw.columns[0]
        eff_disp[eff_base] = [
            _format_efficiency(metric, value) for metric, value in eff_raw[eff_base].items()
        ]
        for col in eff_raw.columns[1:]:
            eff_disp[col] = [
                _format_efficiency(metric, value) for metric, value in eff_raw[col].items()
            ]
            eff_disp[f"Δ({col})"] = [
                _format_efficiency_delta(metric, value)
                for metric, value in (eff_raw[col] - eff_raw[eff_base]).items()
            ]
        print("\n效率与成本（mean，基线差值）：")
        print(eff_disp.to_string())

    # 文件名带上被比较的两个 run —— 固定叫 compare.html 时，连着比两个模型，
    # 第二次会静默覆盖第一次，用户以为还留着。
    out = ROOT / "outputs" / f"compare__{_compare_slug(df.columns)}.html"
    judged_here = [m for m in _JUDGED if m in df.index]
    out.write_text(
        "<h2>Run 对比</h2><h3>配置差异</h3><ul>"
        + ("<li><b>[同一执行] 仅比较量具，不是 skill delta</b></li>" if same_facts else "")
        + "".join(f"<li>[对比维度] {d}</li>" for d in axes)
        + "".join(f"<li>[随之联动] {d}</li>" for d in derived)
        + "".join(f"<li><b>[⚠️ 污染] {d}</b></li>" for d in polluted)
        + "".join(f"<li><b>[⚠️ 尺子不同] {d}</b></li>" for d in judges)
        + "</ul>"
        + (f"<p><b>⚠️ 语义分不是同一把尺子判的 —— 只有 {judged_here} "
           f"这几行不可比，确定性维度不受影响。</b></p>" if judges else "")
        + "<h3>指标</h3>" + disp.to_html()
        + ("<h3>效率与成本（mean，基线差值）</h3>" + eff_disp.to_html()
           if not eff_disp.empty else ""),
        encoding="utf-8")
    print(f"\nHTML → {out}")


if __name__ == "__main__":
    main()
