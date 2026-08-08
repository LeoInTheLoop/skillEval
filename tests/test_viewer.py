"""P7 read-only run inspection."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import yaml

from pipeline.viewer import (
    filter_records,
    inspect_run,
    render_html_view,
    render_inspection,
    write_html_view,
)
from workflows.score_full import write_no_evaluable_report


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "outputs" / "group" / "trial-01"
    inputs = run_dir / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "dataset.jsonl").write_text(
        '\n'.join([
            json.dumps({
                "id": "alpha-pos-01",
                "prompt": "make alpha",
                "expected_skills": ["alpha"],
            }),
            json.dumps({
                "id": "none-rej-01",
                "prompt": "answer normally",
                "expected_skills": [],
            }),
        ]) + '\n',
        encoding="utf-8",
    )
    snapshot = {
        "config_hash": "sha256:cfg",
        "suite": {
            "suite_id": "routing-alpha",
            "dataset": "missing/original.jsonl",
            "skills": {"mode": "routing_only", "cfg": "alpha-v1"},
        },
    }
    (run_dir / "config.snapshot.yaml").write_text(
        yaml.safe_dump(snapshot), encoding="utf-8"
    )
    runs = [
        {
            "case_id": "alpha-pos-01",
            "turn_index": 1,
            "repeat_index": 0,
            "model": "matrix-label",
            "resolved_model": "provider/model-a",
            "selected_skills": ["alpha"],
            "loaded_skills": ["alpha"],
            "tool_calls": [{"name": "write", "arguments": {}}],
            "artifacts": [{"path": "out/a.md", "size_bytes": 3}],
            "final_answer": "done",
            "reasoning": "picked alpha because the prompt asked to generate a file",
        },
        {
            "case_id": "none-rej-01",
            "turn_index": 1,
            "repeat_index": 0,
            "model": "matrix-label",
            "resolved_model": "provider/model-a",
            "selected_skills": ["alpha"],
            "ok": False,
            "error_kind": "network",
            "error_subkind": "network_timeout",
            "error": "timed out",
        },
    ]
    (run_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps(run) for run in runs) + "\n", encoding="utf-8"
    )
    (run_dir / "scores.json").write_text(json.dumps({
        "scores": {"exact_set_match": 1.0},
        "gate_pass": True,
    }), encoding="utf-8")
    (run_dir / "report.html").write_text("<h1>report</h1>", encoding="utf-8")
    suggestion = run_dir / "improvements" / "round-01" / "suggestions.json"
    suggestion.parent.mkdir(parents=True)
    suggestion.write_text("{}", encoding="utf-8")
    return run_dir


def test_inspection把完整证据链投影到一个只读结构(tmp_path):
    run_dir = _run_dir(tmp_path)
    before = (run_dir / "runs.jsonl").read_bytes()

    view = inspect_run(run_dir, root=tmp_path)

    assert view["run_dir"] == "outputs/group/trial-01"
    assert view["status_counts"] == {"failed": 1, "ok": 1}
    assert view["matching_record_count"] == 2
    assert view["available_case_ids"] == ["alpha-pos-01", "none-rej-01"]
    assert view["gate_pass"] is True
    assert view["observed_models"] == ["provider/model-a"]
    assert view["records"][0]["prompt"] == "make alpha"
    assert view["records"][0]["tool_calls"] == ["write"]
    assert view["records"][0]["tool_call_details"][0]["arguments"] == {}
    assert view["records"][0]["artifacts"] == ["out/a.md"]
    assert view["records"][0]["artifact_details"][0]["size_bytes"] == 3
    assert view["records"][0]["reasoning"] == (
        "picked alpha because the prompt asked to generate a file"
    )
    assert view["paths"]["reports"] == ["outputs/group/trial-01/report.html"]
    assert view["paths"]["suggestions"] == [
        "outputs/group/trial-01/improvements/round-01/suggestions.json"
    ]
    assert (run_dir / "runs.jsonl").read_bytes() == before


def test_inspection可按case_status_skill_model组合过滤(tmp_path):
    records = inspect_run(_run_dir(tmp_path), root=tmp_path)["records"]

    selected = filter_records(
        records,
        case_id="none-rej-01",
        turn=1,
        repeat=0,
        status="failed",
        skill="alpha",
        model="MODEL-A",
    )

    assert len(selected) == 1
    assert selected[0]["error_subkind"] == "network_timeout"
    assert filter_records(records, status="skipped") == []
    assert filter_records(records, turn=2) == []


def test_terminal_view先显示verdict与证据路径再列record(tmp_path):
    rendered = render_inspection(inspect_run(_run_dir(tmp_path), root=tmp_path))

    assert "Verdict: PASS" in rendered
    assert "records=2/2" in rendered
    assert "Evidence paths:" in rendered
    assert "runs: outputs/group/trial-01/runs.jsonl" in rendered
    assert "none-rej-01 t1 r0 [failed]" in rendered
    assert "prompt: make alpha" in rendered
    assert "answer: done" in rendered
    assert "network/network_timeout: timed out" in rendered


def test_html_viewer自包含且具备六类过滤与证据链接(tmp_path):
    run_dir = _run_dir(tmp_path)
    view = inspect_run(run_dir, root=tmp_path)
    output = run_dir / "viewer.html"

    rendered = render_html_view(view, root=tmp_path, output=output)

    assert "https://" not in rendered
    assert "<script src=" not in rendered
    for field in ("case", "turn", "repeat", "status", "skill", "model"):
        assert f'id="{field}"' in rendered
    assert "href='runs.jsonl'" in rendered
    assert "href='report.html'" in rendered
    assert "provider/model-a" in rendered
    assert "network/network_timeout: timed out" in rendered
    assert "Loaded skills" in rendered
    assert "Reasoning" in rendered
    assert "picked alpha because the prompt asked to generate a file" in rendered
    assert "size_bytes" in rendered


def test_html_viewer转义模型输出而不是把它当markup执行(tmp_path):
    run_dir = _run_dir(tmp_path)
    view = inspect_run(run_dir, root=tmp_path)
    view["records"][0]["final_answer"] = "</script><img src=x onerror=alert(1)>"

    rendered = render_html_view(view, root=tmp_path, output=run_dir / "viewer.html")

    assert "</script><img" not in rendered
    assert "&lt;/script&gt;&lt;img" in rendered


def test_html_viewer幂等复用且不同内容不静默覆盖(tmp_path):
    run_dir = _run_dir(tmp_path)
    view = inspect_run(run_dir, root=tmp_path)
    output = run_dir / "viewer.html"

    first, action = write_html_view(view, root=tmp_path, output=output)
    assert (first, action) == (output, "written")
    assert write_html_view(view, root=tmp_path, output=output)[1] == "reused"
    output.write_text("user content", encoding="utf-8")

    import pytest
    with pytest.raises(FileExistsError, match="--force"):
        write_html_view(view, root=tmp_path, output=output)
    assert write_html_view(view, root=tmp_path, output=output, force=True)[1] == "written"


# --- HANDOFF ★ 更新 16 强制回归：镜像真实历史 run docker-t1 的形状 ---
# （4/4 openclaw exit=1、error_kind=network、阿里云百炼欠费文案，从没存过 error_subkind）。
_ARREARAGE_ERROR = (
    "openclaw exit=1: ack/decision] model fallback decision: decision=candidate_failed "
    "requested=qwen/qwen3.5-plus candidate=qwen/qwen3.5-plus reason=auth next=none "
    "detail=400 Access denied, please make sure your account is in good standing. "
    "For details, see: https://www.alibabacloud.com/help/en/model-studio/error-code#overdue-payment"
)


def _all_system_failure_runs() -> list[dict]:
    return [
        {"case_id": case_id, "turn_index": 1, "repeat_index": repeat,
         "model": "qwen/qwen3.5-plus", "ok": False, "status": "failed",
         "error_kind": "network", "error": _ARREARAGE_ERROR}
        for case_id in ("deliverable-pack-pos-01", "deliverable-pack-rej-01")
        for repeat in (0, 1)
    ]


def test_全系统故障的三个出口给出一致的indeterminate和quota子类(tmp_path):
    runs = _all_system_failure_runs()

    # 出口 1：score_full 的「全部运行都是系统故障」路径（df.empty 之后走这条）。
    all_df = pd.DataFrame([
        {"case_id": r["case_id"], "repeat": r["repeat_index"], "error_kind": r["error_kind"]}
        for r in runs
    ])
    score_full_dir = tmp_path / "score_full_run"
    write_no_evaluable_report(
        score_full_dir, suite={}, snap={}, all_df=all_df, sysfail=all_df,
        html_output=score_full_dir / "report.html",
        scores_output=score_full_dir / "scores.json",
    )
    from_score_full = json.loads((score_full_dir / "scores.json").read_text(encoding="utf-8"))

    # 出口 2 + 3：pipeline inspect 与 pipeline view 共用同一个 inspect_run()。
    run_dir = tmp_path / "outputs" / "group" / "docker-t1-shape"
    run_dir.mkdir(parents=True)
    (run_dir / "config.snapshot.yaml").write_text(
        yaml.safe_dump({"suite": {"suite_id": "s", "skills": {"mode": "full", "cfg": "v1"}}}),
        encoding="utf-8",
    )
    (run_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps(r) for r in runs) + "\n", encoding="utf-8"
    )
    before = (run_dir / "runs.jsonl").read_bytes()
    view = inspect_run(run_dir, root=tmp_path)
    after = (run_dir / "runs.jsonl").read_bytes()

    assert before == after, "runs.jsonl 是不可变的历史事实，不能被读取/展示改写"
    assert from_score_full["quality_verdict"] == "indeterminate"
    assert view["quality_verdict"] == "indeterminate"
    assert from_score_full["gate_pass"] is None
    assert view["gate_pass"] is None
    # 理由一致：同一个 derive_verdict()，同样的结构化 counts。
    assert from_score_full["verdict_reason"] == view["verdict_reason"]
    assert "network=4" in view["verdict_reason"]
    # subkind 从原始错误文本重分类出 quota，且没有改写 runs.jsonl 里缺失的 error_subkind。
    assert all(r["error_subkind"] == "provider_quota_exhausted" for r in view["records"])
    assert all(json.loads(line).get("error_subkind") is None
               for line in after.decode("utf-8").splitlines())

    rendered = render_inspection(view)
    assert "Verdict: INDETERMINATE" in rendered
    assert "the Skill never executed" in rendered
