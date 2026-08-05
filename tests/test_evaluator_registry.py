from __future__ import annotations

import pytest

from evaluators import (
    EvaluationContext,
    evaluate_all,
    evaluator_manifest,
    scalar_metrics,
)
from contracts import RoutingSuite


def _plugin(tmp_path, name="custom_eval_plugin"):
    path = tmp_path / f"{name}.py"
    registered = f"custom-quality-{name}"
    path.write_text(
        "\n".join([
            "from evaluators import register",
            f"@register('{registered}', version='custom-quality-v1')",
            "class CustomQualityEvaluator:",
            "    def __init__(self, value=0.5):",
            "        self.value = value",
            "    def evaluate(self, context):",
            "        return {'metrics': {'custom_quality': self.value}, "
            "'seen_runs': len(context.runs)}",
        ]) + "\n",
        encoding="utf-8",
    )
    return path, f"{name}:{registered}"


def _suite(evaluator_ref: str) -> dict:
    return {
        "suite_id": "custom_eval_demo",
        "suite_version": "1.0",
        "dataset": "evals/datasets/demo.jsonl",
        "runtime": "litellm",
        "skills": {
            "dir": "skills", "target": ["pdf"], "mode": "routing_only", "cfg": "v1",
        },
        "models": [{
            "id": "model-a", "model": "openai/model-a",
            "api_key_env": "MODEL_API_KEY", "params": {"temperature": 0},
        }],
        "tools": [],
        "repeats": 1,
        "scoring": {
            "metrics": ["custom_quality"],
            "evaluators": [evaluator_ref],
            "evaluator_options": {evaluator_ref: {"value": 0.75}},
            "gate": {"custom_quality": ">= 0.70"},
        },
    }


def test_external_evaluator_loads_by_module_reference_and_receives_options(
    tmp_path, monkeypatch
):
    _path, reference = _plugin(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    context = EvaluationContext(
        suite={}, snapshot={}, cases={}, runs=[{"case_id": "c1"}], scores={}, rows=[]
    )

    layers = evaluate_all([reference], context, {reference: {"value": 0.75}})

    assert layers[reference]["metrics"] == {"custom_quality": 0.75}
    assert layers[reference]["seen_runs"] == 1
    assert scalar_metrics(layers) == {"custom_quality": 0.75}


def test_evaluator_manifest_binds_version_source_and_options(tmp_path, monkeypatch):
    path, reference = _plugin(tmp_path, "manifest_eval_plugin")
    monkeypatch.syspath_prepend(str(tmp_path))

    first = evaluator_manifest([reference], {reference: {"value": 0.6}})[reference]
    assert first["version"] == "custom-quality-v1"
    assert first["source_sha256"].startswith("sha256:")
    assert first["options"] == {"value": 0.6}
    assert first["expose_scalar_metrics"] is True

    path.write_text(path.read_text(encoding="utf-8") + "# source changed\n", encoding="utf-8")
    second = evaluator_manifest([reference], {reference: {"value": 0.6}})[reference]
    assert second["source_sha256"] != first["source_sha256"]


def test_suite_validates_external_evaluator_and_options_before_run(tmp_path, monkeypatch):
    _path, reference = _plugin(tmp_path, "suite_eval_plugin")
    monkeypatch.syspath_prepend(str(tmp_path))

    suite = RoutingSuite.model_validate(_suite(reference))
    assert suite.scoring.evaluators == [reference]
    assert suite.scoring.evaluator_options[reference] == {"value": 0.75}

    invalid = _suite(reference)
    invalid["scoring"]["evaluator_options"] = {"not-enabled": {}}
    with pytest.raises(ValueError, match="未启用"):
        RoutingSuite.model_validate(invalid)

    invalid = _suite(reference)
    invalid["scoring"]["evaluator_options"][reference] = {"unknown_option": True}
    with pytest.raises(ValueError, match="构造器不匹配"):
        RoutingSuite.model_validate(invalid)


def test_evaluator_requires_version_and_metric_collisions_are_rejected():
    from evaluators import register

    with pytest.raises(ValueError, match="version"):
        @register("missing-version-test")
        class MissingVersion:
            pass

    with pytest.raises(ValueError, match="metric 冲突"):
        scalar_metrics({
            "one": {"metrics": {"same": 0.2}},
            "two": {"metrics": {"same": 0.8}},
        })

    assert scalar_metrics(
        {"reliability": {"metrics": {"variance": 0.0}}}, ["reliability"]
    ) == {}
