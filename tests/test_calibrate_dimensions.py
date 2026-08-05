from __future__ import annotations

import json
import sys

import pytest

from workflows.calibrate_dimensions import calibrate_one, load_gold


def _gold(tmp_path, *, count=10):
    path = tmp_path / "dimension-gold.json"
    path.write_text(json.dumps({
        "schema_version": "1.0",
        "calibration_id": "dimensions-v1",
        "source_run": "outputs/run-1",
        "annotated_by": "human-reviewer",
        "annotated_at": "2026-08-05",
        "dimension_versions": {"faithfulness": "v1"},
        "annotations": [
            {
                "case_id": f"c{i}",
                "repeat_index": 0,
                "turn_index": 1,
                "dimension": "faithfulness",
                "score": 1.0 if i % 2 == 0 else 0.5,
                "rationale": f"人工证据 {i}",
            }
            for i in range(count)
        ],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _grading(tmp_path, *, count=10, score_delta=0.0, failed=()):
    path = tmp_path / "grading.j.json"
    failed = set(failed)
    graded = []
    for i in range(count):
        if i in failed:
            continue
        human = 1.0 if i % 2 == 0 else 0.5
        graded.append({
            "case_id": f"c{i}",
            "repeat_index": 0,
            "turn_index": 1,
            "expectations": [],
            "dimensions": [{
                "dimension": "faithfulness",
                "score": max(0.0, min(1.0, human + score_delta)),
                "evidence": f"judge 证据 {i}",
            }],
        })
    path.write_text(json.dumps({
        "judge": {
            "id": "judge-a",
            "model": "openai/model-a",
            "api_base_env": "JUDGE_BASE_URL",
            "api_key_env": "JUDGE_API_KEY",
            "params": {"temperature": 0},
            "system_prompt_hash": "sha256:prompt",
            "dimensions": {"faithfulness": "v1"},
        },
        "graded": graded,
        "judge_failures": [
            {"case_id": f"c{i}", "repeat_index": 0, "turn_index": 1,
             "error": "invalid json"}
            for i in failed
        ],
    }), encoding="utf-8")
    return path


def test_continuous_dimension_calibration_uses_explicit_tolerance_and_mae(tmp_path):
    result = calibrate_one(
        load_gold(_gold(tmp_path)),
        _grading(tmp_path, score_delta=-0.2),
    )[0]

    assert result.within_tolerance_agreement == 1.0
    assert result.mean_absolute_error == 0.2
    assert result.agreement_tolerance == 0.25
    assert result.qualified is True
    assert result.ab_swap_consistency is None


def test_continuous_dimension_calibration_records_disagreement(tmp_path):
    result = calibrate_one(
        load_gold(_gold(tmp_path)),
        _grading(tmp_path, score_delta=-0.4),
    )[0]

    assert result.within_tolerance_agreement == 0.0
    assert result.qualified is False
    assert result.disagreements[0]["absolute_error"] == 0.4


def test_invalid_output_is_not_silently_treated_as_a_score(tmp_path):
    result = calibrate_one(
        load_gold(_gold(tmp_path)),
        _grading(tmp_path, failed={0}),
    )[0]

    assert result.n_valid_scores == 9
    assert result.invalid_judge_outputs == 1
    assert result.invalid_judge_output_rate == 0.1
    assert result.qualified is False


def test_dimension_calibration_requires_minimum_sample_and_same_rubric(tmp_path):
    result = calibrate_one(
        load_gold(_gold(tmp_path, count=2)),
        _grading(tmp_path, count=2),
    )[0]
    assert result.within_tolerance_agreement == 1.0
    assert result.qualified is False

    payload = json.loads(_grading(tmp_path).read_text(encoding="utf-8"))
    payload["judge"]["dimensions"]["faithfulness"] = "v2"
    grading = tmp_path / "grading.wrong-rubric.json"
    grading.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="rubric version"):
        calibrate_one(load_gold(_gold(tmp_path)), grading)


def test_dimension_gold_rejects_duplicate_locator(tmp_path):
    path = _gold(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["annotations"].append(payload["annotations"][0])
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="重复定位键"):
        load_gold(path)


def test_dimension_calibration_cli_writes_tamper_evident_registry(
    tmp_path, monkeypatch, capsys
):
    from workflows import calibrate_dimensions
    from workflows.calibration_registry import assess_gate_calibration, load_registry

    output = tmp_path / "dimension-calibration.json"
    registry = tmp_path / "registry.json"
    grading = _grading(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "workflows.calibrate_dimensions",
        "--gold", str(_gold(tmp_path)),
        "--grading", str(grading),
        "--output", str(output),
        "--registry-output", str(registry),
    ])
    calibrate_dimensions.main()

    loaded, _, _ = load_registry(registry)
    entry = loaded.entries[0]
    assert entry.scope == "standard_dimension"
    assert entry.dimension == "faithfulness"
    assert entry.mean_absolute_error == 0.0
    assert entry.agreement_tolerance == 0.25

    judge = json.loads(grading.read_text(encoding="utf-8"))["judge"]
    result = assess_gate_calibration(
        {"faithfulness": ">= 0.8"},
        registry_path=registry,
        output_judge=judge,
        trajectory_judge=None,
        deterministic_trajectory=set(),
    )
    assert result["unqualified"] == []
    assert "QUALIFIED" in capsys.readouterr().out


def test_registry_rejects_dimension_report_that_self_qualifies_with_lax_policy(tmp_path):
    from workflows.calibrate_dimensions import calibrate
    from workflows.calibration_registry import registry_from_report

    report = calibrate(
        _gold(tmp_path, count=2),
        [_grading(tmp_path, count=2)],
        agreement_threshold=0.0,
        invalid_threshold=1.0,
        tolerance=1.0,
        min_annotations=1,
    )
    report_path = tmp_path / "lax-report.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="低于 registry policy|过松 policy"):
        registry_from_report(report_path)
