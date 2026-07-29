"""读一个 run 目录，算路由指标 + 判发布门槛，输出 scores.json + report.html。

指标：Exact Skill-Set Match / Top-1 / No-Skill 拒答率 / 误激活率 / 分题型准确率 /
每-skill PRF / 混淆矩阵。底层指标复用 scikit-learn，不手写。
再加三样跨 repeat 的东西（`metrics.py`）：稳定性方差、效率维度、跨 repeat 不一致的题。
可选并入 `grade.py` 用**独立 judge 模型**判的 assertion_pass_rate。
门槛从该 run 的 config.snapshot.yaml 读。

用法：
  python -m workflows.score_routing                     # 评 outputs/ 下最新的一次 run
  python -m workflows.score_routing --dir outputs/xxx   # 评指定 run
  python -m workflows.score_routing --judge-id glm5     # 并入哪把尺子判的 assertion 分
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import yaml
from sklearn.metrics import classification_report, confusion_matrix

from workflows import metrics
from contracts import load_cases
from workflows.diagnostics import failure_summary
from workflows.grade import load_grading

ROOT = Path(__file__).parent.parent
NONE = "∅"  # No-Skill 标签


def top1(sel: list[str]) -> str:
    return sel[0] if sel else NONE


def case_type(case_id: str) -> str:
    """从 id 取题型段：pdf-pos-01 → pos（命名规范见 evals/AUTHORING.md §1.2）。"""
    parts = case_id.split("-")
    return parts[-2] if len(parts) >= 3 else "?"


def latest_run_dir() -> Path:
    """outputs/ 下 mtime 最新的 run 目录（见 evals/AUTHORING.md §1.4）。"""
    # v2 archive layout is group/execution_id; retain the v1 direct layout so
    # historical results remain scoreable after the migration.
    dirs = [d for d in (ROOT / "outputs").rglob("*") if (d / "runs.jsonl").exists()]
    if not dirs:
        raise SystemExit("outputs/ 下没有 run 目录，先跑 run_routing.py")
    # 按 runs.jsonl 的 mtime，不是目录的 —— 覆盖写文件不会更新目录 mtime
    return max(dirs, key=lambda d: (d / "runs.jsonl").stat().st_mtime)


def check_gate(gate: dict, scores: dict) -> list[tuple[str, str, float, bool]]:
    """gate 形如 {"top1": ">= 0.90"}；返回 [(指标, 条件, 实际值, 是否通过)]。"""
    out = []
    for metric, cond in (gate or {}).items():
        op, thr = cond.split()
        got = scores.get(metric, float("nan"))
        ok = got >= float(thr) if op == ">=" else got <= float(thr)
        out.append((metric, cond, got, ok))
    return out


def describe_dimension(name: str) -> str:
    """维度 id → 人读的标签。不认识就原样返回（别为了打印崩掉整个评分）。"""
    from workflows import dimensions

    d = dimensions.STANDARD_DIMENSIONS.get(name)
    return d.label if d else name


def warn_uncalibrated(gate_cfg: dict) -> list[str]:
    """gate 里出现语义维度时提醒 —— **不拦，只提醒**。

    AGENTS.md §③/§22.6：未校准的 judge 分「只是另一个随机数」，本不该决定发版。
    但强行拦住会让人以为功能坏了，所以这里只把话说清楚：想让它决定发版，
    先做校准（judge vs 人工标注一致率 ≥ 80%）。默认不往 gate 里写维度就没这回事。
    """
    from workflows import dimensions

    return sorted(set(gate_cfg) & set(dimensions.STANDARD_DIMENSIONS))


def print_failure_summary(summary: dict) -> None:
    """Show system outcomes before quality metrics, with an actionable next step."""
    if not summary["failed_runs"]:
        return
    by_kind = " ".join(f"{key}={value}" for key, value in summary["by_error_kind"].items())
    subkind = " ".join(
        f"{key}={value}" for key, value in summary["by_error_subkind"].items()
    )
    print(f"\n失败运行（不计入路由质量分）：{summary['failed_runs']}  {by_kind}")
    if subkind:
        print(f"  细分：{subkind}")
    for item in summary["remediation"]:
        print(f"  下一步 [{item['category']}]：{item['action']}")


def write_no_evaluable_report(d: Path, *, suite: dict, snap: dict, summary: dict,
                              attempted: int, n_cases: int, html_path: str) -> None:
    """Persist a useful report even when every request failed externally."""
    gate_cfg = suite.get("scoring", {}).get("gate", {}) or {}
    gate = [
        {"metric": metric, "condition": condition, "actual": None, "pass": None}
        for metric, condition in gate_cfg.items()
    ]
    payload = {
        "run_dir": d.name,
        "suite_id": suite.get("suite_id"),
        "run_mode": "synthetic_mock" if snap.get("mock") else "real",
        "quality_verdict": "not_evaluated" if snap.get("mock") else "indeterminate",
        "config_hash": snap.get("config_hash"),
        "model": snap.get("resolved_model", {}).get("id"),
        "n_cases": n_cases,
        "n_runs": attempted,
        "n_evaluable_runs": 0,
        "scores": {},
        "failure_summary": summary,
        "gate": gate,
        "gate_enforced": not bool(snap.get("mock")),
        "gate_pass": None,
    }
    (d / "scores.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    actions = "".join(
        f"<li><b>{item['category']}</b>: {item['action']}</li>" for item in summary["remediation"]
    ) or "<li>runs.jsonl is empty; inspect the runner output.</li>"
    by_kind = " ".join(
        f"{key}={value}" for key, value in summary["by_error_kind"].items()
    ) or "no recorded runs"
    Path(html_path).parent.mkdir(parents=True, exist_ok=True)
    Path(html_path).write_text(
        "\n".join([
            "<h2>Skill Routing Report — no evaluable runs</h2>",
            f"<p>attempted runs: {attempted}; failures: {summary['failed_runs']} ({by_kind})</p>",
            "<p>Routing quality and gate are <b>N/A</b>: infrastructure/provider failures are not task failures.</p>",
            "<h3>Next steps</h3><ul>", actions, "</ul>",
        ]), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", help="run 目录，默认取 outputs/ 下最新的一个")
    ap.add_argument("--cases", help="默认从该 run 的 config.snapshot.yaml 里读")
    ap.add_argument("--runs", help="默认 {dir}/runs.jsonl")
    ap.add_argument("--html", help="默认 {dir}/report.html")
    ap.add_argument("--judge-id", help="并入哪个 judge 的 assertion 判定；"
                                       "默认取 suite 的 scoring.judge.id")
    args = ap.parse_args()

    d = Path(args.dir) if args.dir else latest_run_dir()
    args.runs = args.runs or str(d / "runs.jsonl")
    args.html = args.html or str(d / "report.html")

    # 配置快照是这次 run 的唯一真相：数据集、门槛都从它读，不靠人记 CLI 参数
    snap_path = d / "config.snapshot.yaml"
    snap = yaml.safe_load(snap_path.read_text(encoding="utf-8")) if snap_path.exists() else {}
    suite = snap.get("suite", {})
    cases_path = args.cases or (ROOT / suite["dataset"] if suite.get("dataset") else None)
    if cases_path is None:
        raise SystemExit(f"{d} 缺 config.snapshot.yaml，请显式给 --cases")
    print(f"run dir: {d.name}")
    if suite:
        print(f"suite: {suite.get('suite_id')} v{suite.get('suite_version')} "
              f"| model: {snap.get('resolved_model', {}).get('id')} "
              f"| config_hash: {snap.get('config_hash')}")

    cases = {c.id: c for c in load_cases(cases_path)}

    rows = []
    all_runs = []                            # 含失败的：效率维度要看全部，耗时/报错都算数
    for line in Path(args.runs).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        all_runs.append(r)
        if not r.get("ok", True):
            continue
        c = cases[r["case_id"]]
        exp, sel = set(c.expected_skills), set(r["selected_skills"])
        rows.append({
            "case_id": c.id,
            "type": case_type(c.id),
            "severity": c.severity,
            "repeat": r.get("repeat_index", 0),
            "n_expected": len(exp),
            "exact": exp == sel,            # Exact Skill-Set Match：严格，多选/少选都算错
            "y_true": top1(c.expected_skills),
            "y_pred": top1(r["selected_skills"]),
        })
    df = pd.DataFrame(rows)
    failures = failure_summary(all_runs)
    if df.empty:
        print("没有可评的 run（runs.jsonl 为空或全部失败）；未把它误记成 skill 失败。")
        print_failure_summary(failures)
        write_no_evaluable_report(
            d, suite=suite, snap=snap, summary=failures, attempted=len(all_runs),
            n_cases=len(cases), html_path=args.html,
        )
        print(f"\nscores.json + report.html → {d}")
        return

    total = len(df)
    n_cases = df.case_id.nunique()
    single = df[df.n_expected == 1]     # 单 skill 正向题
    multi = df[df.n_expected > 1]       # 多 skill 题
    neg = df[df.n_expected == 0]        # 应拒答题

    exact_match = df.exact.mean()
    top1_acc = (single.y_true == single.y_pred).mean() if len(single) else float("nan")
    multi_acc = multi.exact.mean() if len(multi) else float("nan")
    reject_acc = neg.exact.mean() if len(neg) else float("nan")
    false_act = 1 - reject_acc if len(neg) else float("nan")
    # Critical Skill Miss Rate（AGENTS.md §21.3 / §22.4，默认门槛 = 0）。
    # 这是 case.severity 唯一的落地用途：以前它只在契约里被校验，评分和 gate
    # 一次都没读过 —— 写题的人以为标了 critical 就多一层保护，其实跟 medium 一样。
    crit = df[df.severity == "critical"]
    critical_miss = 1 - crit.exact.mean() if len(crit) else float("nan")

    # 混淆矩阵只覆盖单标签题（multi 题的多选无法放进单标签矩阵，已单列 multi_exact）
    sdf = df[df.n_expected <= 1]
    labels = sorted(set(sdf.y_true) | set(sdf.y_pred), key=lambda x: (x == NONE, x))

    attempted = len(all_runs)
    print(f"\n路由结果 — {n_cases} cases × {total // n_cases} runs = {total} 次可评判定"
          + (f"（另有 {attempted - total} 次失败）" if attempted != total else "") + " "
          f"| skills={len([l for l in labels if l != NONE])}")
    print_failure_summary(failures)
    print(f"  Exact skill-set match   {exact_match:6.1%}   ← 最严格（全部题）")
    print(f"  Top-1 accuracy (单skill) {top1_acc:6.1%}")
    print(f"  Multi-skill exact       {multi_acc:6.1%}")
    print(f"  No-Skill rejection      {reject_acc:6.1%}")
    print(f"  False activation        {false_act:6.1%}")
    if len(crit):
        print(f"  Critical skill miss     {critical_miss:6.1%}   "
              f"← {crit.case_id.nunique()} 道 severity=critical 的题")

    # 分题型：区分度看这张表 —— pos 高而 amb 低，说明 description 的边界没写清
    by_type = df.groupby("type").agg(runs=("exact", "size"), accuracy=("exact", "mean"))
    by_type = by_type.reindex([t for t in ("pos", "amb", "multi", "rej") if t in by_type.index])
    print("\n分题型准确率（Exact match）：")
    for t, row in by_type.iterrows():
        print(f"  {t:<6} {int(row.runs):>4} runs   {row.accuracy:6.1%}")

    rep = classification_report(
        sdf.y_true, sdf.y_pred, labels=labels, output_dict=True, zero_division=0
    )
    per = pd.DataFrame(rep).T.loc[labels, ["precision", "recall", "f1-score", "support"]]
    per.index = [i if i != NONE else "∅(No-Skill)" for i in per.index]
    print("\n每-skill 指标（单标签题）：")
    print(per.round(2).to_string())

    cm = confusion_matrix(sdf.y_true, sdf.y_pred, labels=labels)
    cmdf = pd.DataFrame(cm, index=[f"真:{l}" for l in labels],
                        columns=[f"预:{l}" for l in labels])
    print(f"\n混淆矩阵（行=期望 top-1，列=预测 top-1；{len(sdf)}/{total} 次单标签判定）：")
    print(cmdf.to_string())

    conf = [(cm[i][j], t, p)
            for i, t in enumerate(labels)
            for j, p in enumerate(labels)
            if i != j and cm[i][j] > 0]
    conf.sort(reverse=True)
    if conf:
        print("\n最易混淆（次数）：")
        for n, t, p in conf[:5]:
            print(f"  {t} → {p}: {n}")

    # --- 稳定性 / 效率 / 题目质量诊断（metrics.py，全部确定性计算）---
    per_repeat = df.groupby("repeat").exact.mean().tolist()
    stability = metrics.stats(per_repeat)
    efficiency = metrics.efficiency(all_runs)
    flaky = metrics.flaky_cases(rows, key="exact")
    print(f"\n稳定性：exact_set_match 跨 {len(per_repeat)} 次 repeat "
          f"{metrics.format_stats(stability, percent=True)}")
    print("\n效率维度（不进 gate；换 skill 让准确率涨一点却让 token 翻倍时要看得见）：")
    for key, unit in (("time_seconds", "s"), ("tokens", ""), ("tool_calls", ""), ("errors", "")):
        print(f"  {key:<14} {metrics.format_stats(efficiency[key], unit=unit)}")
    if flaky:
        print("\n⚠️ 同题跨 repeat 判定不一致（delta 会被这些题的随机性污染）：")
        for f in flaky:
            print(f"  {f['case_id']:<24} {f['passed']}/{f['runs']} 判对")

    # --- 维度评分 + 发布门槛 ---
    grading = load_grading(d, snap, args.judge_id)
    judge = (grading or {}).get("judge")
    scores = {
        "exact_set_match": float(exact_match),
        "top1": float(top1_acc), "multi_exact": float(multi_acc),
        "no_skill_rejection": float(reject_acc), "false_activation": float(false_act),
        "critical_miss": float(critical_miss),
        # 没跑 grade.py 就是 None（N/A）。不能按 0，那等于「一条断言都没过」。
        "assertion_pass_rate": (grading or {}).get("pass_rate"),
    }
    scores.update({f"type_{t}": float(r.accuracy) for t, r in by_type.iterrows()})

    # 语义维度：0–1 连续分，judge 模型打的。和上面的路由指标分开印 ——
    # 上面的换台机器重算必然一致，这里的换把尺子就会变。
    semantic = dict((grading or {}).get("dimension_means") or {})
    scores.update(semantic)
    if semantic:
        print(f"\n语义维度（judge {judge['id']} / {judge['model']}，0–1 连续分）：")
        for name, value in semantic.items():
            print(f"  {name:<22} {value:.2f}   {describe_dimension(name)}")
        print("  ⚠️ 换 judge 或改 rubric 这些数就会变，不同尺子之间不可比")
    if judge and grading.get("total"):
        print(f"\nassertion_pass_rate {scores['assertion_pass_rate']:.1%} "
              f"— judge {judge['id']}，{grading['total']} 条断言")
    # gate 里配了但值为 None 的维度要 SKIP，不能按 0 判 FAIL（AGENTS.md ★★★ ③）。
    # 典型场景：gate 写了 assertion_pass_rate 但这次没跑 grade.py。
    gate_cfg = suite.get("scoring", {}).get("gate", {}) or {}
    na = [m for m in gate_cfg if scores.get(m) is None]
    gate_rows = check_gate({m: c for m, c in gate_cfg.items() if scores.get(m) is not None},
                           scores)
    # 一条都判不了时不许报 PASS：那是「没测」，不是「通过」
    observed_passed = all(ok for *_, ok in gate_rows) if gate_rows else None
    is_mock = bool(snap.get("mock"))
    passed = None if is_mock else observed_passed
    verdict = (
        "NOT EVALUATED（synthetic mock 只验证管道）"
        if is_mock else
        {True: "PASS", False: "FAIL", None: "无法判定（可判维度全为 N/A）"}[passed]
    )
    if gate_rows or na:
        print("\n发布门槛：")
        for metric, cond, got, ok in gate_rows:
            status = "OBSERVED" if is_mock else ("PASS" if ok else "FAIL")
            print(f"  {status:<8} {metric:<20} {cond:<8} 实际 {got:.1%}")
        for m in na:
            print(f"  SKIP  {m:<20} {gate_cfg[m]:<8} N/A —— 不按 0 分处理")
        uncal = warn_uncalibrated(gate_cfg)
        if uncal:
            print(f"  ⚠️ gate 里有语义维度 {uncal}：这是 judge 模型打的分，"
                  f"未做校准（§22.6）就让它决定发版，等于让一个没对过表的尺子判生死")
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

    (d / "scores.json").write_text(json.dumps({
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
        "n_cases": int(n_cases), "n_runs": int(attempted), "n_evaluable_runs": int(total),
        "scores": scores,
        "failure_summary": failures,
        "judge": judge,          # 这批 assertion 分是谁判的；没跑 grade.py 就是 None
        "stability": {"exact_set_match": stability, "per_repeat": per_repeat},
        "efficiency": efficiency,
        "flaky_cases": flaky,
        "by_type": by_type.assign(accuracy=by_type.accuracy.round(4)).to_dict(orient="index"),
        "per_skill": per.round(4).to_dict(orient="index"),
        "confusion_matrix": {"labels": labels, "matrix": cm.tolist(),
                             "covers_runs": int(len(sdf))},
        "gate": gate_records,
        "gate_enforced": not is_mock,
        "gate_pass": passed,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    gate_html = "".join(
        f"<tr><td>{m}</td><td>{c}</td><td>{g:.1%}</td>"
        f"<td>{'OBSERVED ONLY' if is_mock else ('✅ PASS' if ok else '❌ FAIL')}</td></tr>"
        for m, c, g, ok in gate_rows
    ) + "".join(f"<tr><td>{m}</td><td>{gate_cfg[m]}</td><td>N/A</td><td>⏭ SKIP</td></tr>"
                for m in na)
    html = [
        (
            "<div style='border:3px solid #b45309;padding:12px;background:#fff7ed'>"
            "<b>SYNTHETIC MOCK — PIPELINE SMOKE ONLY.</b> "
            "Metrics below are generated test data and are not a skill-quality verdict.</div>"
            if is_mock else ""
        ),
        "<h2>Skill Routing Report</h2>",
        f"<p><b>{suite.get('suite_id', '?')}</b> v{suite.get('suite_version', '?')} | "
        f"model {snap.get('resolved_model', {}).get('id', '?')} | "
        f"skills cfg {suite.get('skills', {}).get('cfg', '?')} | "
        f"{n_cases} cases × {total // n_cases} runs | "
        f"config_hash <code>{snap.get('config_hash', '?')}</code></p>",
        f"<p><b>Exact skill-set match {exact_match:.1%}</b> | Top-1 {top1_acc:.1%} | "
        f"Multi exact {multi_acc:.1%} | No-Skill rejection {reject_acc:.1%} | "
        f"False activation {false_act:.1%}</p>",
    ]
    if gate_rows or na:
        html += [f"<h3>发布门槛 — {'QUALITY VERDICT' if is_mock else 'GATE'} {verdict}</h3>",
                 "<table border=1 cellpadding=4><tr><th>指标</th><th>门槛</th>"
                 f"<th>实际</th><th>结果</th></tr>{gate_html}</table>"]
    if semantic:
        sem_html = "".join(
            f"<tr><td>{k}</td><td>{v:.2f}</td><td>{describe_dimension(k)}</td></tr>"
            for k, v in semantic.items())
        html += [f"<h3>语义维度（judge {judge['id']} / {judge['model']}，0–1 连续分）</h3>",
                 "<table border=1 cellpadding=4><tr><th>维度</th><th>分</th><th>说明</th></tr>"
                 f"{sem_html}</table>",
                 f"<p><b>⚠️ 换 judge 或改 rubric 这些数就会变，不同尺子之间不可比。</b>"
                 f"rubric 版本：{judge.get('dimensions')}</p>"]
    if judge and grading.get("total"):
        html += [f"<p>assertion_pass_rate <b>{scores['assertion_pass_rate']:.1%}</b> — judge "
                 f"<b>{judge['id']}</b>（{judge['model']}，env {judge['api_base_env']}），"
                 f"{grading['total']} 条断言。</p>"]
    eff_html = "".join(f"<tr><td>{k}</td><td>{metrics.format_stats(efficiency[k])}</td></tr>"
                       for k in metrics.EFFICIENCY_KEYS)
    html += [f"<h3>稳定性</h3><p>exact_set_match 跨 {len(per_repeat)} 次 repeat "
             f"{metrics.format_stats(stability, percent=True)}</p>",
             "<h3>效率维度（不进 gate）</h3>"
             f"<table border=1 cellpadding=4><tr><th>维度</th><th>mean ± stddev</th></tr>"
             f"{eff_html}</table>",
             "<h3>跨 repeat 不一致的题</h3><ul>"
             + ("".join(f"<li>{f['case_id']} — {f['passed']}/{f['runs']} 判对</li>" for f in flaky)
                or "<li>无</li>") + "</ul>",
             "<h3>分题型准确率</h3>",
             by_type.assign(accuracy=by_type.accuracy.map("{:.1%}".format)).to_html(),
             "<h3>每-skill 指标（单标签题）</h3>", per.round(3).to_html(),
             f"<h3>混淆矩阵（{len(sdf)}/{total} 次单标签判定）</h3>", cmdf.to_html()]
    Path(args.html).parent.mkdir(parents=True, exist_ok=True)
    Path(args.html).write_text("\n".join(html), encoding="utf-8")
    print(f"\nscores.json + report.html → {d}")


if __name__ == "__main__":
    main()
