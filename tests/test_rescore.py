"""正式 rescore：换尺子不重新执行 Agent，历史事实不可变。"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from contracts import RoutingCase, RunResult
from pipeline.rescore import ROOT, build_rescore_plan, execute_rescore, sha256_file


def _run_dir(tmp_path, *, trajectory: bool = False):
    run_dir = tmp_path / "outputs" / "demo-run"
    (run_dir / "inputs").mkdir(parents=True)
    case = RoutingCase(
        id="demo-pos-01",
        prompt="生成 out/result.md",
        expected_skills=["demo"],
        expect_artifacts=["out/result.md"],
        expect_tools=["write"],
        expect_assertions=["结果正文包含 done"],
        expect_trajectory={"required_tools": ["write"]} if trajectory else None,
    )
    (run_dir / "inputs" / "dataset.jsonl").write_text(
        case.model_dump_json() + "\n", encoding="utf-8"
    )
    # snapshot 故意指向另一份坏题；正式 rescore 必须优先使用 inputs/dataset.jsonl。
    live_dataset = tmp_path / "current-dataset.jsonl"
    live_dataset.write_text(
        RoutingCase(id="wrong-pos-01", prompt="wrong").model_dump_json() + "\n",
        encoding="utf-8",
    )
    run = RunResult(
        case_id=case.id,
        repeat_index=0,
        model="tested-model",
        selected_skills=["demo"],
        loaded_skills=["demo"],
        final_answer="done",
        tool_calls=[{"name": "write", "count": 1, "failures": 0}],
        trajectory=[{
            "step_index": 1,
            "event_type": "tool_call",
            "name": "write",
            "tool_name": "write",
            "call_id": "c1",
            "arguments": {"path": "out/result.md"},
            "evidence_level": "exact",
        }] if trajectory else [],
        artifacts=[{
            "path": "out/result.md",
            "sha256": "a" * 64,
            "size_bytes": 4,
            "mime_type": "text/markdown",
            "text_excerpt": "done",
        }],
        workspace_files=["out/result.md"],
        runtime_name="openclaw",
    )
    (run_dir / "runs.jsonl").write_text(run.model_dump_json() + "\n", encoding="utf-8")
    scoring = {
        "metrics": ["task_completion", "artifact_hit", "tool_hit"],
        "evaluators": ["outcome", "trajectory", "reliability", "efficiency"],
        "gate": {"task_completion": ">= 0.80"},
        "judge": {
            "id": "judge-v1",
            "model": "openai/judge-model",
            "api_base_env": "TEST_JUDGE_BASE",
            "api_key_env": "TEST_JUDGE_KEY",
            "params": {"temperature": 0},
            "dimensions": [],
        },
        "trajectory": {
            "enabled": trajectory,
            "mode": "hybrid" if trajectory else "judge",
            "judge_id": "judge-v1" if trajectory else None,
            "dimensions": ["tool_selection"],
            "version": "trajectory-v1",
            "argument_schema_version": "trajectory-argument-assertion-v1",
            "argument_rubric_version": "argument-correctness-v1",
        },
    }
    snapshot = {
        "suite": {
            "suite_id": "full-demo",
            "suite_version": "1.0",
            "dataset": str(live_dataset),
            "skills": {"mode": "full", "cfg": "v1"},
            "scoring": scoring,
            "repeats": 1,
        },
        "resolved_model": {"id": "tested", "model": "openai/tested-model"},
        "config_hash": "sha256:config",
        "dataset_hash": "sha256:archived",
        "skill_catalog": ["demo"],
        "mock": False,
    }
    (run_dir / "config.snapshot.yaml").write_text(
        yaml.safe_dump(snapshot, sort_keys=False), encoding="utf-8"
    )
    return run_dir


def test_score_only_rescore_never_calls_runtime_or_model_and_preserves_root_outputs(tmp_path):
    run_dir = _run_dir(tmp_path)
    original_runs_hash = sha256_file(run_dir / "runs.jsonl")
    for name in ("scores.json", "report.html", "trajectory.jsonl", "grading.judge-v1.json"):
        (run_dir / name).write_text("old-result", encoding="utf-8")

    plan = build_rescore_plan(
        run_dir, stages=("score",), grading_id="measure-01",
    )
    assert plan.runnable and not plan.egress_required
    seen: list[list[str]] = []

    def deterministic_only(command):
        seen.append(command)
        assert "workflows.run_routing" not in command
        assert "workflows.grade" not in command
        subprocess.run(command, cwd=ROOT, check=True)

    outputs = execute_rescore(
        plan,
        completion=lambda **_: (_ for _ in ()).throw(AssertionError("model called")),
        trajectory_completion=lambda **_: (_ for _ in ()).throw(AssertionError("model called")),
        command_runner=deterministic_only,
    )

    assert len(seen) == 1 and "workflows.score_full" in seen[0]
    assert sha256_file(run_dir / "runs.jsonl") == original_runs_hash
    assert (run_dir / "scores.json").read_text(encoding="utf-8") == "old-result"
    assert (run_dir / "grading.judge-v1.json").read_text(encoding="utf-8") == "old-result"
    scores = json.loads((run_dir / "scores" / "measure-01.json").read_text(encoding="utf-8"))
    assert scores["scores"]["task_completion"] == 1.0
    assert scores["rescore"]["source_runs_sha256"] == original_runs_hash
    assert scores["rescore"]["grading_hash"] == plan.provenance["grading_hash"]
    assert set(outputs) == {"scores", "report", "trajectory"}


def test_pipeline_rescore默认只打印计划不写文件(tmp_path, monkeypatch, capsys):
    from pipeline import __main__ as pipeline_main

    run_dir = _run_dir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "pipeline", "rescore", "--run-dir", str(run_dir),
        "--grading-id", "cli-plan", "--stages", "score",
    ])
    with pytest.raises(SystemExit) as exit_info:
        pipeline_main.main()
    assert exit_info.value.code == 0
    assert "RESCORE PLAN" in capsys.readouterr().out
    assert not (run_dir / "scores").exists()


def test_same_facts_and_gauge_have_same_hash_and_reproducible_deterministic_scores(tmp_path):
    run_dir = _run_dir(tmp_path)
    first = build_rescore_plan(run_dir, stages=("score",), grading_id="first")
    second = build_rescore_plan(run_dir, stages=("score",), grading_id="second")
    assert first.provenance["grading_hash"] == second.provenance["grading_hash"]

    execute_rescore(first)
    execute_rescore(second)
    a = json.loads(first.paths.scores.read_text(encoding="utf-8"))
    b = json.loads(second.paths.scores.read_text(encoding="utf-8"))
    assert a["scores"] == b["scores"]
    assert a["evaluation"] == b["evaluation"]

    collision = build_rescore_plan(run_dir, stages=("score",), grading_id="first")
    assert not collision.runnable
    assert "拒绝覆盖" in collision.blocked_reasons[-1]


def test_registered_exact_judge_gauge_can_enforce_assertion_gate(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path)
    snapshot_path = run_dir / "config.snapshot.yaml"
    snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
    scoring = snapshot["suite"]["scoring"]
    scoring["judge"] = {
        "id": "local-label-does-not-matter",
        "model": "openai/qwen-plus",
        "api_base_env": "DASHSCOPE_BASE_URL",
        "api_key_env": "DASHSCOPE_API_KEY",
        "params": {"temperature": 0},
        "dimensions": [
            "faithfulness", "completeness", "relevancy", "instruction_following",
        ],
    }
    scoring["calibration_registry"] = "evals/calibration/registry.json"
    scoring["gate"] = {
        "task_completion": ">= 0.80",
        "assertion_pass_rate": ">= 0.80",
    }
    snapshot_path.write_text(yaml.safe_dump(snapshot, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://judge.example.test/v1")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    plan = build_rescore_plan(
        run_dir, stages=("grade", "score"), grading_id="calibrated-assertions",
    )
    assert plan.runnable
    assert plan.provenance["gauge"]["calibration_registry"]["sha256"].startswith("sha256:")

    def calibrated_judge(**_):
        return json.dumps({
            "expectations": [{
                "text": "结果正文包含 done", "passed": True, "evidence": "done",
            }],
            "dimensions": [
                {"dimension": name, "score": 1.0, "evidence": "done"}
                for name in (
                    "faithfulness", "completeness", "relevancy", "instruction_following",
                )
            ],
        }, ensure_ascii=False)

    execute_rescore(plan, completion=calibrated_judge)
    scores = json.loads(plan.paths.scores.read_text(encoding="utf-8"))
    assert scores["gate_pass"] is True
    assert scores["calibration"]["unqualified"] == []
    assertion = scores["calibration"]["metrics"]["assertion_pass_rate"]
    assert assertion["qualified"] is True
    assert assertion["entry_id"] == "meeting-and-brief-assertions-qwenplus-v1"


def test_judge_and_trajectory_rescore_are_versioned_and_feed_only_the_new_score(tmp_path, monkeypatch):
    run_dir = _run_dir(tmp_path, trajectory=True)
    snapshot_path = run_dir / "config.snapshot.yaml"
    snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
    snapshot["suite"]["scoring"]["judge"]["dimensions"] = ["faithfulness"]
    snapshot["suite"]["scoring"]["gate"]["faithfulness"] = ">= 0.80"
    snapshot_path.write_text(yaml.safe_dump(snapshot, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("TEST_JUDGE_BASE", "https://judge.example.test/v1")
    monkeypatch.setenv("TEST_JUDGE_KEY", "test-key")
    (run_dir / "grading.judge-v1.json").write_text('{"old":true}\n', encoding="utf-8")
    runs_hash = sha256_file(run_dir / "runs.jsonl")

    plan = build_rescore_plan(
        run_dir,
        stages=("grade", "trajectory", "score"),
        grading_id="judge-round-02",
    )
    assert plan.runnable and plan.egress_required

    def output_judge(**_):
        return json.dumps({
            "expectations": [{
                "text": "结果正文包含 done", "passed": True, "evidence": "done",
            }],
            "dimensions": [{
                "dimension": "faithfulness", "score": 1.0, "evidence": "done",
            }],
        }, ensure_ascii=False)

    def trajectory_judge(**_):
        return json.dumps({
            "dimensions": [{
                "dimension": "tool_selection", "score": 0.75,
                "evidence": "write", "source": "judge",
            }]
        }, ensure_ascii=False)

    execute_rescore(
        plan,
        completion=output_judge,
        trajectory_completion=trajectory_judge,
    )

    grading = json.loads(plan.paths.grading.read_text(encoding="utf-8"))
    trajectory = json.loads(plan.paths.trajectory_grading.read_text(encoding="utf-8"))
    scores = json.loads(plan.paths.scores.read_text(encoding="utf-8"))
    assert grading["rescore"]["grading_id"] == "judge-round-02"
    assert trajectory["rescore"]["source_runs_sha256"] == runs_hash
    assert scores["scores"]["assertion_pass_rate"] == 1.0
    assert scores["scores"]["faithfulness"] == 1.0
    assert scores["gate_pass"] is None
    assert scores["quality_verdict"] == "indeterminate"
    assert scores["calibration"]["unqualified"] == ["faithfulness"]
    semantic_gate = next(item for item in scores["gate"] if item["metric"] == "faithfulness")
    assert semantic_gate["status"] == "judge-uncalibrated" and semantic_gate["pass"] is None
    # hybrid 中 deterministic tool selection=1.0 优先，不被 judge 的 0.75 覆盖。
    assert scores["scores"]["tool_selection"] == 1.0
    assert json.loads((run_dir / "grading.judge-v1.json").read_text())["old"] is True
    assert sha256_file(run_dir / "runs.jsonl") == runs_hash
