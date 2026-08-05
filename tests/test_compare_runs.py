"""跨 run 对比的差异分类（compare_runs.py）。

重点在最后一类：**judge 不同 ≠ 全表污染**。judge 只判 assertion_pass_rate 一个维度，
把它报成全局污染是过度警告，而过度警告的下场是所有警告都被忽略。
"""
from __future__ import annotations

from workflows.compare_runs import diff_configs, efficiency_means, load_run, same_execution_facts


def _run(**kw):
    base = dict(dir="d", suite_id="s", model="qwen", skillcfg="v1",
                dataset="a.jsonl", dataset_hash="h", runtime="litellm",
                judge=None, judge_model=None, judge_prompt_hash=None,
                judge_dimensions="{}",
                n_skills=6, gate_pass=True, scores={})
    return {**base, **kw}


def test_只差_skillcfg_是干净对照():
    axes, derived, polluted, judges = diff_configs([_run(skillcfg="none"), _run(skillcfg="v1")])
    assert axes and not polluted and not judges


def test_换模型是污染():
    *_, polluted, judges = diff_configs([_run(model="qwen"), _run(model="glm")])
    assert polluted and not judges


def test_judge_不同单独归类不算全表污染():
    _, _, polluted, judges = diff_configs([
        _run(skillcfg="none", judge="qwen", judge_model="openai/qwen3.7-max"),
        _run(skillcfg="v1", judge="glm5", judge_model="openai/glm-5.1"),
    ])
    assert not polluted, "judge 只影响 assertion 一行，不该报成全表污染"
    assert len(judges) == 2, "judge id 和 judge model 都该被列出来"


def test_同一个_judge_不报差异():
    *_, judges = diff_configs([_run(skillcfg="none", judge="glm5"),
                               _run(skillcfg="v1", judge="glm5")])
    assert not judges


def test_都没跑_grade_不报尺子差异():
    """judge 全是 None —— 那是「都没测 assertion」，不是「尺子不同」。"""
    *_, judges = diff_configs([_run(skillcfg="none"), _run(skillcfg="v1")])
    assert not judges


def test_judge_prompt_变了也算换了尺子():
    """模型没换但 system prompt 改了，判定标准照样变了。"""
    *_, judges = diff_configs([
        _run(judge="glm5", judge_prompt_hash="sha256:aaa"),
        _run(judge="glm5", judge_prompt_hash="sha256:bbb"),
    ])
    assert judges


def test_效率维度取_mean_且缺失不伪造零():
    rows = efficiency_means([
        _run(efficiency={"time_seconds": {"mean": 2.5}, "tokens": {"mean": 120}}),
        _run(efficiency={"time_seconds": {"mean": 3.0}}),
    ])
    assert rows["time_seconds"] == [2.5, 3.0]
    assert rows["tokens"] == [120, None]


def test_同一source_runs的不同grading_hash是换尺子不是skill_delta(tmp_path):
    run_dir = tmp_path / "run"
    scores_dir = run_dir / "scores"
    scores_dir.mkdir(parents=True)
    (run_dir / "config.snapshot.yaml").write_text(
        "suite:\n  suite_id: demo\n  dataset: cases.jsonl\n  skills:\n    cfg: v1\n",
        encoding="utf-8",
    )
    for name, grading_hash in (("r1", "sha256:g1"), ("r2", "sha256:g2")):
        (scores_dir / f"{name}.json").write_text(__import__("json").dumps({
            "model": "m",
            "config_hash": "sha256:c",
            "scores": {"task_completion": 1.0, "faithfulness": 0.5},
            "judge": {"id": name, "model": "judge"},
            "rescore": {
                "source_run_dir": str(run_dir),
                "source_runs_sha256": "sha256:same",
                "grading_hash": grading_hash,
            },
        }), encoding="utf-8")

    runs = [load_run(scores_dir / "r1.json"), load_run(scores_dir / "r2.json")]
    assert same_execution_facts(runs)
    _, _, polluted, judges = diff_configs(runs)
    assert not polluted
    assert any("grading_hash" in item for item in judges)
