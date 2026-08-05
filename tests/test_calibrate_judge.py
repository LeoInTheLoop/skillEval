from __future__ import annotations

import json
import sys

import pytest

from workflows.calibrate_judge import calibrate_one, load_gold


def _gold(tmp_path):
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps({
            "schema_version": "1.0",
            "calibration_id": "cal-v1",
            "source_run": "outputs/run-1",
            "annotated_by": "human",
            "annotated_at": "2026-07-29",
            "annotations": [
                {"case_id": "c1", "assertion": "a1", "passed": True, "rationale": "证据 1"},
                {"case_id": "c1", "assertion": "a2", "passed": False, "rationale": "证据 2"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _grading(tmp_path, *, second=False, failures=0, omit_second=False):
    expectations = [{"text": "a1", "passed": True, "evidence": "e1"}]
    if not omit_second:
        expectations.append({"text": "a2", "passed": second, "evidence": "e2"})
    path = tmp_path / "grading.j.json"
    path.write_text(
        json.dumps({
            "judge": {
                "id": "j", "model": "m", "api_base_env": "BASE",
                "params": {}, "system_prompt_hash": "sha256:prompt", "dimensions": {},
            },
            "graded": [{"case_id": "c1", "repeat_index": 0, "expectations": expectations}],
            "judge_failures": [
                {"case_id": f"failed-{i}", "repeat_index": 0, "error": "bad json"}
                for i in range(failures)
            ],
        }),
        encoding="utf-8",
    )
    return path


def test_calibration计算agreement与invalid_rate(tmp_path):
    result = calibrate_one(load_gold(_gold(tmp_path)), _grading(tmp_path, failures=1))

    assert result.judge_vs_human_agreement == 1.0
    assert result.invalid_judge_output_rate == 0.5
    assert result.ab_swap_consistency is None
    assert not result.qualified_for_absolute_assertions


def test_calibration记录逐条分歧(tmp_path):
    result = calibrate_one(load_gold(_gold(tmp_path)), _grading(tmp_path, second=True))

    assert result.judge_vs_human_agreement == 0.5
    assert result.disagreements[0]["assertion"] == "a2"
    assert result.disagreements[0]["human_passed"] is False


def test_calibration拒绝grading覆盖不完整(tmp_path):
    with pytest.raises(ValueError, match="覆盖不一致"):
        calibrate_one(
            load_gold(_gold(tmp_path)),
            _grading(tmp_path, omit_second=True),
        )


def test_human_gold拒绝重复断言(tmp_path):
    path = _gold(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["annotations"].append(payload["annotations"][0])
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="重复"):
        load_gold(path)


def test_calibrate_main从CLI写报告(tmp_path, monkeypatch, capsys):
    from workflows import calibrate_judge

    output = tmp_path / "calibration.json"
    monkeypatch.setattr(sys, "argv", [
        "workflows.calibrate_judge",
        "--gold", str(_gold(tmp_path)),
        "--grading", str(_grading(tmp_path)),
        "--output", str(output),
    ])
    calibrate_judge.main()

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["calibrations"][0]["qualified_for_absolute_assertions"] is True
    assert "QUALIFIED" in capsys.readouterr().out


def test_calibrate_main可同时生成有证据绑定的registry(tmp_path, monkeypatch):
    from workflows import calibrate_judge
    from workflows.calibration_registry import load_registry

    output = tmp_path / "calibration.json"
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(sys, "argv", [
        "workflows.calibrate_judge",
        "--gold", str(_gold(tmp_path)),
        "--grading", str(_grading(tmp_path)),
        "--output", str(output),
        "--registry-output", str(registry),
    ])
    calibrate_judge.main()

    loaded, _, _ = load_registry(registry)
    assert loaded.entries[0].qualified is True
    assert loaded.entries[0].evidence_report_sha256.startswith("sha256:")
