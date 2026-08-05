"""P7 read-only run inspection."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from pipeline.viewer import filter_records, inspect_run, render_inspection


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
    assert view["records"][0]["artifacts"] == ["out/a.md"]
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
        status="failed",
        skill="alpha",
        model="MODEL-A",
    )

    assert len(selected) == 1
    assert selected[0]["error_subkind"] == "network_timeout"
    assert filter_records(records, status="skipped") == []


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
