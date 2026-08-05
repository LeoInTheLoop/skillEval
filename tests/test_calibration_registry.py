from __future__ import annotations

import json

import pytest

from workflows.calibration_registry import (
    assess_gate_calibration,
    calibrated_gate_outcome,
    deterministic_trajectory_metrics,
    load_registry,
)


QWENPLUS = {
    "id": "any-label",
    "model": "openai/qwen-plus",
    "api_base_env": "DASHSCOPE_BASE_URL",
    "params": {"temperature": 0},
    "system_prompt_hash": "sha256:4d0362f4a214079d",
    "dimensions": {
        "faithfulness": "v1",
        "completeness": "v1",
        "relevancy": "v1",
        "instruction_following": "v1",
    },
}


def test_tracked_registry_verifies_evidence_hash_and_qualifies_exact_assertion_gauge():
    registry, path, digest = load_registry("evals/calibration/registry.json")
    assert len(registry.entries) == 2
    assert path.name == "registry.json" and digest.startswith("sha256:")

    result = assess_gate_calibration(
        {"assertion_pass_rate": ">= 0.80"},
        registry_path="evals/calibration/registry.json",
        output_judge=QWENPLUS,
        trajectory_judge=None,
        deterministic_trajectory=set(),
    )
    assert result["unqualified"] == []
    assert result["metrics"]["assertion_pass_rate"]["entry_id"].startswith(
        "meeting-and-brief"
    )


def test_absolute_assertion_calibration_does_not_qualify_standard_dimensions():
    result = assess_gate_calibration(
        {"faithfulness": ">= 0.80", "assertion_pass_rate": ">= 0.80"},
        registry_path="evals/calibration/registry.json",
        output_judge=QWENPLUS,
        trajectory_judge=None,
        deterministic_trajectory=set(),
    )
    assert result["metrics"]["assertion_pass_rate"]["qualified"] is True
    assert result["metrics"]["faithfulness"]["qualified"] is False
    assert result["unqualified"] == ["faithfulness"]


def test_changed_params_or_prompt_hash_invalidates_calibration():
    changed = {**QWENPLUS, "params": {"temperature": 0.2}}
    result = assess_gate_calibration(
        {"assertion_pass_rate": ">= 0.80"},
        registry_path="evals/calibration/registry.json",
        output_judge=changed,
        trajectory_judge=None,
        deterministic_trajectory=set(),
    )
    assert result["unqualified"] == ["assertion_pass_rate"]
    assert "fingerprint" in result["metrics"]["assertion_pass_rate"]["reason"]


def test_deterministic_trajectory_metric_needs_no_judge_calibration():
    layer = {"structured": [{"metrics": {"tool_selection": 1.0,
                                            "argument_correctness": None}}]}
    deterministic = deterministic_trajectory_metrics(layer, "hybrid")
    result = assess_gate_calibration(
        {"tool_selection": ">= 0.80"},
        registry_path=None,
        output_judge=None,
        trajectory_judge={"model": "judge"},
        deterministic_trajectory=deterministic,
    )
    assert result["metrics"] == {} and result["unqualified"] == []
    assert deterministic_trajectory_metrics(layer, "judge") == set()


def test_unqualified_semantic_gate_is_indeterminate_but_hard_failure_still_fails():
    rows = [
        ("task_completion", ">= 0.8", 1.0, True),
        ("faithfulness", ">= 0.8", 1.0, True),
    ]
    assert calibrated_gate_outcome(rows, {"faithfulness"}) is None
    rows[0] = ("task_completion", ">= 0.8", 0.5, False)
    assert calibrated_gate_outcome(rows, {"faithfulness"}) is False


def test_registry_rejects_tampered_evidence_report(tmp_path):
    evidence = tmp_path / "report.json"
    evidence.write_text("{}", encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "schema_version": "1.0",
        "entries": [{
            "entry_id": "x",
            "calibration_id": "c",
            "scope": "absolute_assertions",
            "dimension": None,
            "judge": {
                "model": "m", "api_base_env": "BASE", "params": {},
                "system_prompt_hash": "sha256:p", "dimensions": {},
            },
            "agreement": 1.0,
            "invalid_output_rate": 0.0,
            "qualified": True,
            "evidence_report": str(evidence),
            "evidence_report_sha256": "sha256:" + "0" * 64,
            "evidence_judge_id": "j",
        }],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="hash 不匹配"):
        load_registry(registry)
