"""Full eval 打分：产物 / tool / skill 加载的**确定性断言**（AGENTS.md §3.4、§21.2）。

和 score_routing.py 平级：一个 run 目录要么是路由的，要么是 full 的，各自算各自的。
写出的 scores.json 结构一致，所以 compare_runs.py 两边通吃。

**本文件自己一行 LLM 判定都没有**，全是代码能确定的事：文件在不在、非不非空、
后缀对不对、tool 调没调、skill 加载没加载。语义判定由 `grade.py` 用**独立的 judge
模型**单独跑，产物落在 `grading.<judge_id>.json`；这里只是把它**读进来并入维度分**。

这个分工是有意的：确定性维度换台机器重算必然一致，assertion 维度取决于 judge 模型，
两者混在一个函数里算，就再也说不清某次 delta 是 skill 变了还是尺子变了。

用法：
  python -m workflows.score_full --dir outputs/effect_deliverable-pack_v1.0__openclaw-default__v1
  python -m workflows.score_full --dir ... --judge-id glm5      # 用哪把尺子判的分并进来
"""
from __future__ import annotations

import argparse
import json
import mimetypes
from fnmatch import fnmatch
from pathlib import Path

import pandas as pd
import yaml

from workflows import metrics
from contracts import load_cases
from evaluators import EvaluationContext, evaluate_all
from evaluators.trajectory import merge_trajectory_metrics, write_trajectory_projection
from workflows.grade import load_grading, resolve_run_dataset
from workflows.score_routing import check_gate, describe_dimension, latest_run_dir, warn_uncalibrated

ROOT = Path(__file__).parent.parent
SCORE_VERSION = "score-full-v1"

# 不算在被测 skill 头上的失败：评测系统/网络/runtime 自己挂了（AGENTS.md ★★★ ⑥）。
# 把它们留在分母里，就等于「我们的 CLI 崩了」→「这个 skill 不行」。
_SYSTEM_FAILURES = ("runtime", "network", "harness")


def artifact_hit(pattern: str, artifacts: list[dict]) -> bool:
    """产物断言：存在 + 非空 + MIME 对得上（P1 的三条）。

    ponytail: MIME 是从**文件后缀**推的（adapter 那边也是），所以它抓的是
    「名字对但类型不对」，抓不到「叫 .csv 其实内容是 HTML」。真要嗅内容得上
    python-magic —— 等有一道题真的因此判错了再加。
    """
    want_mime = mimetypes.guess_type(pattern)[0]
    for a in artifacts:
        if not fnmatch(a["path"], pattern):
            continue
        if not a.get("size_bytes"):          # 空文件不算产出
            continue
        if want_mime and a.get("mime_type") and a["mime_type"] != want_mime:
            continue
        return True
    return False


def score_run(run: dict, case, catalog: set[str]) -> dict:
    """一条 run 的全部断言。expect_* 为空 = 该维度对本题 N/A（不是 0 分）。"""
    turn_index = int(run.get("turn_index") or 1)
    turn = case.resolved_turn(turn_index)
    arts = run.get("artifacts") or []
    tools = {t["name"] for t in (run.get("tool_calls") or [])}
    workspace_files = run.get("workspace_files") or []

    # ⚠️ `loaded_skills` 是 OpenClaw **注入进 system prompt** 的目录，不是
    # 「模型决定用它」。实测：拒答题里模型什么都没做（tools=[]、files=[]），
    # 但目标 skill 照样出现在 loaded_skills 里。
    # 所以这只能当**注入是否生效**的体检项（catch「extraDirs 静默失效」那类坑），
    # **不进 gate、不进任务完成度** —— 拿它当质量分就是个假数字。
    loaded_ours = set(run.get("loaded_skills") or []) & catalog
    skill_injected = set(case.expected_skills) <= loaded_ours

    art_hits = sum(artifact_hit(p, arts) for p in turn.expect_artifacts)
    tool_hits = sum(t in tools for t in turn.expect_tools)
    workspace_hits = sum(
        any(fnmatch(path, pattern) for path in workspace_files)
        for pattern in turn.expect_workspace_files
    )
    clean = not (turn.forbid_artifacts and arts)   # 拒答题：不该留文件却留了
    skipped = run.get("status") == "skipped"
    ok = bool(run.get("ok", True)) and not skipped
    return {
        "case_id": case.id,
        "type": case.case_type,
        "stage": case.case_stage,
        "severity": case.severity,
        "repeat": run.get("repeat_index"),
        "turn": turn_index,
        "session_id": run.get("session_id"),
        "skipped": skipped,
        "requires_context": turn.requires_context,
        "ok": ok,
        "error_kind": run.get("error_kind"),
        "skill_injected": skill_injected,
        "art_hits": art_hits, "art_total": len(turn.expect_artifacts),
        "tool_hits": tool_hits, "tool_total": len(turn.expect_tools),
        "workspace_hits": workspace_hits,
        "workspace_total": len(turn.expect_workspace_files),
        "n_artifacts": len(arts),
        # 任务完成度：跑成功 + 声明的产物和 tool 全中 + 该不留文件的没留
        "done": (ok and clean
                 and art_hits == len(turn.expect_artifacts)
                 and tool_hits == len(turn.expect_tools)
                 and workspace_hits == len(turn.expect_workspace_files)),
    }


def aggregate(df: pd.DataFrame, assertion_pass_rate: float | None = None
              ) -> dict[str, float | None]:
    """维度分。分母里剔掉系统故障；某维度没有任何题声明 → None（N/A，不是 0）。

    `assertion_pass_rate` 由 grade.py 判完后传进来；没跑 grade.py 就是 None ——
    **不能按 0 处理**，那等于「一条断言都没过」，会把 gate 判成假 FAIL。
    """
    skipped = df.skipped if "skipped" in df else pd.Series(False, index=df.index)
    turns = df[~skipped]

    def ratio(hits: str, total: str, source: pd.DataFrame = turns) -> float | None:
        if hits not in source or total not in source:
            return None
        total_count = int(source[total].sum())
        return float(source[hits].sum() / total_count) if total_count else None

    grouped = df.copy()
    # 正常 runs.jsonl 中 (case, repeat, turn) 唯一。保留 occurrence 只是兼容旧调用方
    # 直接把两条同 key 的 synthetic row 交给 aggregate() 的用法。
    turn_key = "turn" if "turn" in grouped else "case_id"
    grouped["_occurrence"] = grouped.groupby(
        ["case_id", "repeat", turn_key], dropna=False
    ).cumcount()
    group_columns = ["case_id", "repeat", "_occurrence"]
    conversations = (
        grouped.groupby(group_columns, dropna=False)
        .agg(
            done=("done", "all"),
            severity=("severity", "first"),
            max_turn=("turn", "max") if "turn" in df else ("done", "size"),
            sessions=("session_id", lambda values: len({
                value for value in values if isinstance(value, str) and value
            })) if "session_id" in df else ("done", lambda _values: 0),
            turn_count=("turn", "nunique") if "turn" in df else ("done", "size"),
        )
        .reset_index()
    )
    session_rows = grouped[
        ~grouped["skipped"]
        if "skipped" in grouped
        else pd.Series(True, index=grouped.index)
    ]
    session_conversations = (
        session_rows.groupby(group_columns, dropna=False)
        .agg(
            max_turn=("turn", "max") if "turn" in session_rows else ("done", "size"),
            sessions=("session_id", lambda values: len({
                value for value in values if isinstance(value, str) and value
            })) if "session_id" in session_rows else ("done", lambda _values: 0),
            turn_count=("turn", "nunique") if "turn" in session_rows else ("done", "size"),
        )
        .reset_index()
    )
    multi = session_conversations[session_conversations.turn_count > 1]
    session_continuity = (
        float(((multi.sessions == 1) & (multi.turn_count == multi.max_turn)).mean())
        if len(multi) else None
    )
    context_turns = (
        turns[turns.requires_context]
        if "requires_context" in turns else turns.iloc[0:0]
    )
    file_turns = (
        turns[turns.turn > 1]
        if "turn" in turns else turns.iloc[0:0]
    )

    # Critical Skill Miss Rate（AGENTS.md §21.3，默认门槛 = 0）。full eval 里
    # 「判错」= 这道题没完成。没有 critical 题时是 None（N/A），不是 0 ——
    # 按 0 会让「这批题里根本没标 critical」看起来像「critical 题全过了」。
    crit = conversations[conversations.severity == "critical"]
    return {
        # case/repeat 是一个 conversation，只计一次；多轮题不会因轮数多而加权。
        "task_completion": (
            float(conversations.done.mean()) if len(conversations) else None
        ),
        "turn_completion": float(turns.done.mean()) if len(turns) else None,
        "artifact_hit": ratio("art_hits", "art_total"),
        "tool_hit": ratio("tool_hits", "tool_total"),
        "session_continuity": session_continuity,
        "context_retention": (
            float(context_turns.done.mean()) if len(context_turns) else None
        ),
        "file_state_continuity": ratio(
            "workspace_hits", "workspace_total", file_turns
        ),
        "critical_miss": float(1 - crit.done.mean()) if len(crit) else None,
        "assertion_pass_rate": assertion_pass_rate,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="run 目录，默认取 outputs/ 下最新的一个")
    ap.add_argument("--judge-id", help="并入哪个 judge 的 assertion 判定；"
                                       "默认取 suite 的 scoring.judge.id")
    ap.add_argument("--grading-file", help="显式读取一份版本化 grading JSON")
    ap.add_argument("--no-grading", action="store_true", help="不并入任何输出 Judge 结果")
    ap.add_argument("--trajectory-grading-file", help="显式读取一份 trajectory grading JSON")
    ap.add_argument("--no-trajectory-grading", action="store_true",
                    help="不并入任何 trajectory Judge 结果")
    ap.add_argument("--scores-output", help="默认 {dir}/scores.json")
    ap.add_argument("--html", help="默认 {dir}/report.html")
    ap.add_argument("--trajectory-output", help="默认 {dir}/trajectory.jsonl")
    args = ap.parse_args()
    if args.no_grading and args.grading_file:
        ap.error("--no-grading 与 --grading-file 不能同时使用")
    if args.no_trajectory_grading and args.trajectory_grading_file:
        ap.error("--no-trajectory-grading 与 --trajectory-grading-file 不能同时使用")

    d = Path(args.dir) if args.dir else latest_run_dir()
    scores_output = Path(args.scores_output) if args.scores_output else d / "scores.json"
    html_output = Path(args.html) if args.html else d / "report.html"
    trajectory_output = (
        Path(args.trajectory_output) if args.trajectory_output else d / "trajectory.jsonl"
    )
    snap = yaml.safe_load((d / "config.snapshot.yaml").read_text(encoding="utf-8"))
    suite = snap.get("suite", {})
    catalog = set(snap.get("skill_catalog") or [])
    cases = {c.id: c for c in load_cases(resolve_run_dataset(d, snap))}

    print(f"run dir: {d.name}")
    print(f"suite: {suite.get('suite_id')} v{suite.get('suite_version')} "
          f"| skillcfg: {suite.get('skills', {}).get('cfg')} "
          f"| catalog: {sorted(catalog) or '（空 = No-Skill 基线）'} "
          f"| config_hash: {snap.get('config_hash')}")

    runs = [json.loads(l) for l in
            (d / "runs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    write_trajectory_projection(d, runs, trajectory_output)
    rows = [score_run(r, cases[r["case_id"]], catalog) for r in runs]
    all_df = pd.DataFrame(rows)
    if all_df.empty:
        raise SystemExit("runs.jsonl 为空")

    # 系统故障单列，不进维度分的分母。多轮时要剔除整段 conversation：
    # turn 1 runtime 挂了，turn 2 会是 skipped；只删 turn 1 会把 skipped 留成质量失败。
    sysfail = all_df[all_df.error_kind.isin(_SYSTEM_FAILURES)]
    system_conversations = set(zip(sysfail.case_id, sysfail.repeat))
    df = all_df[
        ~all_df.apply(
            lambda row: (row.case_id, row.repeat) in system_conversations,
            axis=1,
        )
    ]
    n_conversations = all_df[["case_id", "repeat"]].drop_duplicates().shape[0]
    print(
        f"\n{all_df.case_id.nunique()} cases / {n_conversations} conversations / "
        f"{len(all_df)} turns"
    )
    if len(sysfail):
        by_kind = sysfail.error_kind.value_counts().to_dict()
        print(f"  ⚠️ 系统故障 {len(sysfail)} 次已从分母剔除：{by_kind}"
              f"  ← 这些不是 skill 的问题，别算进它头上")
    if df.empty:
        raise SystemExit("全部运行都是系统故障，没有可评的结果")

    grading = None if args.no_grading else load_grading(
        d, snap, args.judge_id, args.grading_file
    )
    judge = (grading or {}).get("judge")
    scores = aggregate(df, (grading or {}).get("pass_rate"))
    print("\n确定性维度（分母 = 非系统故障的运行）：")
    for k, v in scores.items():
        print(f"  {k:<18} {'N/A（没有题声明这个维度）' if v is None else f'{v:6.1%}'}")

    # 语义维度：0–1 连续分，由 judge 模型打。和上面分开印 ——
    # 上面的数换台机器重算必然一致，这里的换把尺子就会变。
    semantic = dict((grading or {}).get("dimension_means") or {})
    scores.update(semantic)
    if semantic:
        print(f"\n语义维度（judge {judge['id']} / {judge['model']}，0–1 连续分）：")
        for name, value in semantic.items():
            label = describe_dimension(name)
            print(f"  {name:<22} {value:.2f}   {label}")
        print("  ⚠️ 换 judge 或改 rubric 这些数就会变，不同尺子之间不可比")
    if judge and grading.get("total"):
        print(f"\n  assertion_pass_rate 由 judge {judge['id']} 判定，"
                  f"{grading['total']} 条断言")

    # 四层报告视图：旧 scores 字段保持兼容，新增 evaluation 由注册表组装。
    layer_context = EvaluationContext(
        suite=suite, snapshot=snap, cases=cases, runs=runs,
        # reliability 不把 runtime/network/harness failure 算成 skill 失败；
        # efficiency 仍消费完整 runs，保留系统耗时和错误成本。
        scores=scores, rows=df.to_dict(orient="records"),
    )
    evaluator_names = (suite.get("scoring", {}).get("evaluators")
                       or ["outcome", "trajectory", "reliability", "efficiency"])
    layers = evaluate_all(evaluator_names, layer_context)
    trajectory_cfg = (suite.get("scoring", {}).get("trajectory") or {})
    trajectory_judge = None
    if (trajectory_cfg.get("enabled") and "trajectory" in layers
            and not args.no_trajectory_grading):
        trajectory_judge_id = trajectory_cfg.get("judge_id") or (judge or {}).get("id")
        if trajectory_judge_id:
            trajectory_path = (
                Path(args.trajectory_grading_file)
                if args.trajectory_grading_file else
                d / f"trajectory_grading.{trajectory_judge_id}.json"
            )
            if trajectory_path.exists():
                trajectory_judge = json.loads(trajectory_path.read_text(encoding="utf-8"))
                layers["trajectory"]["judge"] = trajectory_judge.get("judge")
                judge_metrics = trajectory_judge.get("dimension_means", {})
                layers["trajectory"]["judge_metrics"] = judge_metrics
                layers["trajectory"]["metrics"] = merge_trajectory_metrics(
                    layers["trajectory"].get("metrics") or {},
                    judge_metrics,
                    trajectory_cfg.get("mode", "judge"),
                )
                layers["trajectory"]["graded"] = len(trajectory_judge.get("graded") or [])
                layers["trajectory"]["judge_failures"] = len(
                    trajectory_judge.get("judge_failures") or []
                )
    for layer in ("outcome", "trajectory"):
        if layer in layers:
            scores.update(layers[layer].get("metrics") or {})
    print("\n四层评估视图：")
    for layer_name, layer in layers.items():
        print(f"  {layer_name}: {layer.get('metrics', {})}")

    executed_turns = df[~df.skipped]
    conversation_df = (
        df.groupby(["case_id", "repeat"], dropna=False)
        .agg(
            done=("done", "all"),
            type=("type", "first"),
            stage=("stage", "first"),
            severity=("severity", "first"),
            turns=("turn", "max"),
        )
        .reset_index()
    )

    # 稳定性：把每个 repeat 当成一次独立测量，看 conversation 完成度抖多少。
    # 只有 mean 的时候，「三次都 80%」和「100/100/40」长得一模一样。
    per_repeat = conversation_df.groupby("repeat").done.mean().tolist()
    stability = metrics.stats(per_repeat)
    k = int(suite.get("repeats") or 1)
    pass_at_k = metrics.pass_at_k(conversation_df.to_dict("records"), key="done", k=k)
    pass_all_k = metrics.pass_all_k(conversation_df.to_dict("records"), key="done", k=k)
    pass_at_stats = (
        {"mean": pass_at_k, "stddev": 0.0, "min": pass_at_k, "max": pass_at_k}
        if pass_at_k is not None else None
    )
    pass_all_stats = (
        {"mean": pass_all_k, "stddev": 0.0, "min": pass_all_k, "max": pass_all_k}
        if pass_all_k is not None else None
    )
    scores.update({
        "pass_at_k": pass_at_k,
        "pass_all_k": pass_all_k,
    })
    efficiency = metrics.efficiency(
        run for run in runs if run.get("status") != "skipped"
    )
    print(f"\n稳定性：task_completion 跨 {len(per_repeat)} 次 repeat "
          f"{metrics.format_stats(stability, percent=True)}")
    print(f"  pass@{k:<2} {metrics.format_stats(pass_at_stats, percent=True)}   "
          f"pass^{k} {metrics.format_stats(pass_all_stats, percent=True)}")
    print("\n效率维度（这些不进 gate，但 skill 让准确率涨 5% 却让耗时翻倍时要看得见）：")
    for key, unit in (("time_seconds", "s"), ("tokens", ""), ("tool_calls", ""), ("errors", "")):
        print(f"  {key:<14} {metrics.format_stats(efficiency[key], unit=unit)}")

    flaky = metrics.flaky_cases(conversation_df.to_dict(orient="records"), key="done")
    if flaky:
        print("\n⚠️ 同题跨 repeat 结果不一致（delta 会被这些题的随机性污染）：")
        for f in flaky:
            print(f"  {f['case_id']:<24} {f['passed']}/{f['runs']} 通过")
    # 体检项，不是质量分：它只回答「skill 注入生效了吗」（见 score_run 的说明）
    print(f"\n  [体检] skill 注入生效 {executed_turns.skill_injected.mean():.1%} "
          f"—— 只说明目录可见，不代表模型用了它；不进 gate")

    by_type = conversation_df.groupby("type").agg(
        runs=("done", "size"), completion=("done", "mean")
    )
    print("\n分题型任务完成度：")
    for t, r in by_type.iterrows():
        print(f"  {t:<6} {int(r.runs):>3} runs   {r.completion:6.1%}")

    by_stage = conversation_df.groupby("stage").agg(
        runs=("done", "size"), completion=("done", "mean")
    )
    by_stage = by_stage.reindex(
        [stage for stage in ("trigger", "logic", "artifact", "failure") if stage in by_stage.index]
    )
    print("\n分层任务完成度（四层 taxonomy）：")
    for stage, r in by_stage.iterrows():
        print(f"  {stage:<9} {int(r.runs):>3} runs   {r.completion:6.1%}")

    print("\n逐题：")
    per_case_completion = conversation_df.groupby("case_id").agg(
        conversations=("done", "size"),
        done=("done", "mean"),
        turns=("turns", "max"),
    )
    per_case_turns = executed_turns.groupby("case_id").agg(
        turn_runs=("done", "size"),
        turn_completion=("done", "mean"),
        injected=("skill_injected", "mean"),
        art=("art_hits", "sum"), art_max=("art_total", "sum"),
        tool=("tool_hits", "sum"), tool_max=("tool_total", "sum"),
        workspace=("workspace_hits", "sum"), workspace_max=("workspace_total", "sum"),
        files=("n_artifacts", "mean"))
    per_case = per_case_completion.join(per_case_turns, how="left")
    per_turn = executed_turns.groupby(["case_id", "turn"]).agg(
        runs=("done", "size"),
        completion=("done", "mean"),
        art=("art_hits", "sum"),
        art_max=("art_total", "sum"),
        tool=("tool_hits", "sum"),
        tool_max=("tool_total", "sum"),
        workspace=("workspace_hits", "sum"),
        workspace_max=("workspace_total", "sum"),
    )
    print(per_case.to_string(float_format=lambda v: f"{v:.2f}"))
    if int(all_df.turn.max()) > 1:
        print("\n逐轮：")
        print(per_turn.to_string(float_format=lambda v: f"{v:.2f}"))

    # --- 发布门槛：N/A 的维度跳过，不按 0 处理（AGENTS.md ★★★ ③）---
    gate_cfg = suite.get("scoring", {}).get("gate", {}) or {}
    na = [m for m in gate_cfg if scores.get(m) is None]
    gate_rows = check_gate({m: c for m, c in gate_cfg.items() if scores.get(m) is not None},
                           scores)
    is_mock = bool(snap.get("mock"))
    if gate_rows or na:
        print("\n发布门槛：")
        for metric, cond, got, ok in gate_rows:
            status = "OBSERVED" if is_mock else ("PASS" if ok else "FAIL")
            print(f"  {status:<8} {metric:<18} {cond:<8} 实际 {got:.1%}")
        for m in na:
            print(f"  SKIP  {m:<18} {gate_cfg[m]:<8} N/A —— 不按 0 分处理")
        uncal = warn_uncalibrated(gate_cfg)
        if uncal:
            print(f"  ⚠️ gate 里有语义维度 {uncal}：这是 judge 模型打的分，"
                  f"未做校准（§22.6）就让它决定发版，等于让一个没对过表的尺子判生死")
    # 一条都判不了时不许报 PASS：那是「没测」，不是「通过」
    observed_passed = all(ok for *_, ok in gate_rows) if gate_rows else None
    passed = None if is_mock else observed_passed
    verdict = (
        "NOT EVALUATED（synthetic mock 只验证管道）"
        if is_mock else
        {True: "PASS", False: "FAIL", None: "无法判定（可判维度全为 N/A）"}[passed]
    )
    print(f"  → QUALITY VERDICT {verdict}" if is_mock else f"  → GATE {verdict}")

    gate_records = [
        {
            "metric": m,
            "condition": c,
            "actual": g,
            "pass": None if is_mock else ok,
            **({"observed_pass": ok} if is_mock else {}),
        }
        for m, c, g, ok in gate_rows
    ] + [
        {"metric": m, "condition": gate_cfg[m], "actual": None, "pass": None}
        for m in na
    ]

    out = {
        "run_dir": d.name,
        "suite_id": suite.get("suite_id"),
        "run_mode": "synthetic_mock" if is_mock else "real",
        "quality_verdict": (
            "not_evaluated"
            if is_mock else
            {True: "pass", False: "fail", None: "indeterminate"}[passed]
        ),
        "config_hash": snap.get("config_hash"),
        "model": snap.get("resolved_model", {}).get("id"),
        "n_cases": int(all_df.case_id.nunique()),
        "n_runs": int(len(all_df)),
        "n_conversations": int(n_conversations),
        "n_turns": int(len(all_df)),
        "n_skipped_turns": int(all_df.skipped.sum()),
        "n_system_failures": int(len(sysfail)),
        "system_failures_by_kind": sysfail.error_kind.value_counts().to_dict(),
        "scores": scores,
        "evaluation": layers,
        # 这批分是谁判的。没有它，两个 run 的 assertion_pass_rate 就没法判断可不可比。
        "judge": judge,
        "trajectory_judge": layers.get("trajectory", {}).get("judge"),
        "skill_injected": float(executed_turns.skill_injected.mean()),  # 体检项
        "stability": {"task_completion": stability, "per_repeat": per_repeat},
        "reliability": {
            "k": k,
            "pass_at_k": pass_at_k,
            "pass_all_k": pass_all_k,
            "pass_all_label": f"pass^{k}",
        },
        "efficiency": efficiency,
        "flaky_cases": flaky,
        "by_type": by_type.round(4).to_dict(orient="index"),
        "by_stage": by_stage.round(4).to_dict(orient="index"),
        "per_case": per_case.round(4).to_dict(orient="index"),
        "per_turn": {
            f"{case_id}.t{turn}": values
            for (case_id, turn), values in
            per_turn.round(4).to_dict(orient="index").items()
        },
        "gate": gate_records,
        "gate_enforced": not is_mock,
        "gate_pass": passed,
    }
    scores_output.parent.mkdir(parents=True, exist_ok=True)
    scores_output.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 确定性维度是百分比，语义维度是 0–1 连续分 —— 两种含义不同，不能混在一张表里
    dim_html = "".join(
        f"<tr><td>{k}</td><td>{'N/A' if v is None else f'{v:.1%}'}</td></tr>"
        for k, v in scores.items() if k not in semantic)
    sem_html = "".join(
        f"<tr><td>{k}</td><td>{v:.2f}</td><td>{describe_dimension(k)}</td></tr>"
        for k, v in semantic.items())
    gate_html = "".join(
        f"<tr><td>{m}</td><td>{c}</td><td>{g:.1%}</td>"
        f"<td>{'OBSERVED ONLY' if is_mock else ('✅ PASS' if ok else '❌ FAIL')}</td></tr>"
        for m, c, g, ok in gate_rows
    ) + "".join(f"<tr><td>{m}</td><td>{gate_cfg[m]}</td><td>N/A</td><td>⏭ SKIP</td></tr>"
                for m in na)
    eff_html = "".join(
        f"<tr><td>{k}</td><td>{metrics.format_stats(efficiency[k])}</td></tr>"
        for k in metrics.EFFICIENCY_KEYS)
    flaky_html = ("".join(f"<li>{f['case_id']} — {f['passed']}/{f['runs']} 通过</li>"
                          for f in flaky) or "<li>无</li>")
    judge_html = (
        f"<p>assertion 维度由 judge <b>{judge['id']}</b>（{judge['model']}，"
        f"env {judge['api_base_env']}）判定，共 {grading['total']} 条断言。"
        f"<b>换 judge 这个数就会变，不同 judge 之间不可比。</b></p>"
        if judge else
        "<p>未并入 assertion 判定（没跑 grade.py，或该 judge 无结果）——"
        " assertion_pass_rate 记 N/A。</p>")
    layer_html = "".join(
        f"<h3>{name.title()} 层</h3><pre>{json.dumps(layer.get('metrics', {}), ensure_ascii=False, indent=2)}</pre>"
        for name, layer in layers.items()
    )
    html_output.parent.mkdir(parents=True, exist_ok=True)
    html_output.write_text("\n".join([
        (
            "<div style='border:3px solid #b45309;padding:12px;background:#fff7ed'>"
            "<b>SYNTHETIC MOCK — PIPELINE SMOKE ONLY.</b> "
            "Metrics below are generated test data and are not a skill-quality verdict.</div>"
            if is_mock else ""
        ),
        "<h2>Full Skill Eval Report</h2>",
        f"<p><b>{suite.get('suite_id')}</b> v{suite.get('suite_version')} | "
        f"skills cfg {suite.get('skills', {}).get('cfg')} | "
        f"{out['n_cases']} cases / {n_conversations} conversations / "
        f"{len(all_df)} turns | "
        f"系统故障 {out['n_system_failures']} 次（已剔除） | "
        f"config_hash <code>{snap.get('config_hash')}</code></p>",
        f"<h3>确定性维度 — {'QUALITY VERDICT' if is_mock else 'GATE'} {verdict}</h3>",
        f"<table border=1 cellpadding=4><tr><th>维度</th><th>值</th></tr>{dim_html}</table>",
        (f"<h3>语义维度（judge {judge['id']} / {judge['model']}，0–1 连续分）</h3>"
         f"<table border=1 cellpadding=4><tr><th>维度</th><th>分</th><th>说明</th></tr>"
         f"{sem_html}</table>"
         f"<p><b>⚠️ 换 judge 或改 rubric 这些数就会变，不同尺子之间不可比。</b>"
         f"rubric 版本：{judge.get('dimensions')}</p>") if semantic else "",
        judge_html,
        "<p>trajectory.jsonl 保存本次运行的可观察事件投影；原始事实仍在 runs.jsonl。</p>",
        layer_html,
        f"<h3>发布门槛</h3><table border=1 cellpadding=4><tr><th>指标</th><th>门槛</th>"
        f"<th>实际</th><th>结果</th></tr>{gate_html}</table>",
        f"<h3>稳定性</h3><p>task_completion 跨 {len(per_repeat)} 次 repeat "
        f"{metrics.format_stats(stability, percent=True)}</p>",
        f"<h3>效率维度（不进 gate）</h3>"
        f"<table border=1 cellpadding=4><tr><th>维度</th><th>mean ± stddev</th></tr>"
        f"{eff_html}</table>",
        f"<h3>跨 repeat 不一致的题</h3><ul>{flaky_html}</ul>",
        "<h3>分题型</h3>", by_type.to_html(),
        "<h3>逐题</h3>", per_case.to_html(),
        "<h3>逐轮</h3>", per_turn.to_html(),
    ]), encoding="utf-8")
    print(f"\nscores.json + report.html → {d}")


if __name__ == "__main__":
    main()
