"""N2 Experiment Config 验收测试（AGENTS.md §8.3）。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from adapters.runtimes.mock import MockRuntimeAdapter
from contracts import (
    InvocationRequest,
    RoutingSuite,
    load_suite,
    resolve_suite_references,
)
from workflows.run_routing import config_hash, resolve_skills


def _valid_suite() -> dict:
    return {
        "suite_id": "routing_demo",
        "suite_version": "1.0",
        "description": "demo",
        "dataset": "evals/datasets/demo.jsonl",
        "runtime": "litellm",
        "skills": {
            "dir": "skills",
            "target": ["pdf"],
            "mode": "routing_only",
            "cfg": "v1",
        },
        "models": [
            {
                "id": "model-a",
                "model": "openai/model-a",
                "api_key_env": "MODEL_API_KEY",
                "params": {"temperature": 0},
            }
        ],
        "tools": [],
        "repeats": 3,
        "scoring": {
            "metrics": ["exact_set_match", "top1"],
            "gate": {"exact_set_match": ">= 0.80"},
        },
    }


def test_仓库内所有_suite_都通过严格契约():
    root = Path(__file__).parents[1]
    for path in sorted((root / "evals/suites").glob("*.yaml")):
        assert load_suite(path).suite_id


@pytest.mark.parametrize("kind", ["capability", "regression"])
def test_dataset_kind_区分能力集与回归集(kind):
    data = _valid_suite()
    data["dataset_kind"] = kind
    assert RoutingSuite.model_validate(data).dataset_kind == kind


def test_dataset_kind_非法值被拒绝():
    data = _valid_suite()
    data["dataset_kind"] = "benchmark"
    with pytest.raises(ValidationError):
        RoutingSuite.model_validate(data)


@pytest.mark.parametrize(
    "change",
    [
        {"未知字段": True},
        {"repeats": "3"},
        {"repeats": 0},
        {"parallelism": "4"},
        {"parallelism": 0},
        {"parallelism": 65},
        {"suite_version": 1.0},
    ],
)
def test_未知字段与类型漂移在执行前被拒绝(change):
    data = _valid_suite()
    data.update(change)
    with pytest.raises(ValidationError):
        RoutingSuite.model_validate(data)


def test_model_id_tools_metrics和exclude_都不能重复():
    cases = []
    duplicate_model = _valid_suite()
    duplicate_model["models"] *= 2
    cases.append(duplicate_model)

    duplicate_tools = _valid_suite()
    duplicate_tools["skills"]["mode"] = "full"
    duplicate_tools["tools"] = ["filesystem", "filesystem"]
    cases.append(duplicate_tools)

    duplicate_metrics = _valid_suite()
    duplicate_metrics["scoring"]["metrics"] = ["top1", "top1"]
    cases.append(duplicate_metrics)

    duplicate_exclude = _valid_suite()
    duplicate_exclude["skills"]["exclude"] = ["pdf", "pdf"]
    cases.append(duplicate_exclude)

    overlap_include_exclude = _valid_suite()
    overlap_include_exclude["skills"].update({"include": ["pdf"], "exclude": ["pdf"]})
    cases.append(overlap_include_exclude)

    duplicate_target = _valid_suite()
    duplicate_target["skills"]["target"] = ["pdf", "pdf"]
    cases.append(duplicate_target)

    for data in cases:
        with pytest.raises(ValidationError):
            RoutingSuite.model_validate(data)


def test_routing_only_不能声明_tools():
    data = _valid_suite()
    data["tools"] = ["filesystem"]
    with pytest.raises(ValidationError, match="routing_only"):
        RoutingSuite.model_validate(data)


def test_target必须显式声明且与catalog候选分开():
    missing = _valid_suite()
    del missing["skills"]["target"]
    with pytest.raises(ValidationError, match="target"):
        RoutingSuite.model_validate(missing)

    no_skill_baseline = _valid_suite()
    no_skill_baseline["skills"].update(
        {"target": ["pdf"], "include": ["docx"], "mode": "none"}
    )
    suite = RoutingSuite.model_validate(no_skill_baseline)
    assert suite.skills.target == ["pdf"]
    assert suite.skills.include == ["docx"]


def test_litellm_model条目必须声明_model_而openclaw不用():
    data = _valid_suite()
    del data["models"][0]["model"]
    with pytest.raises(ValidationError, match="litellm"):
        RoutingSuite.model_validate(data)

    data["runtime"] = "openclaw"
    assert RoutingSuite.model_validate(data).models[0].model is None


@pytest.mark.parametrize("condition", ["> 0.8", ">= 80", "<= -0.1", "yes"])
def test_gate_条件语法在运行前校验(condition):
    data = _valid_suite()
    data["scoring"]["gate"]["top1"] = condition
    with pytest.raises(ValidationError, match="gate"):
        RoutingSuite.model_validate(data)


def test_argument_correctness_校准前只能出数不能进_gate():
    data = _valid_suite()
    data["scoring"]["gate"] = {"argument_correctness": ">= 0.80"}
    with pytest.raises(ValidationError, match="尚未登记人工校准"):
        RoutingSuite.model_validate(data)


def test_calibration_registry路径进入规范配置和hash():
    data = _valid_suite()
    baseline = RoutingSuite.model_validate(data).canonical_dict()
    data["scoring"]["calibration_registry"] = "evals/calibration/registry.json"
    calibrated = RoutingSuite.model_validate(data).canonical_dict()
    assert calibrated["scoring"]["calibration_registry"].endswith("registry.json")
    assert config_hash(baseline) != config_hash(calibrated)


def test_suite_禁止明文_secret_但允许环境变量或secret_id():
    data = _valid_suite()
    data["runtime_options"] = {"api_key": "sk-plaintext"}
    with pytest.raises(ValidationError, match="明文 secret"):
        RoutingSuite.model_validate(data)

    data["runtime_options"] = {"providers": [{"password": "plaintext"}]}
    with pytest.raises(ValidationError, match=r"providers\[0\]\.password"):
        RoutingSuite.model_validate(data)

    data["runtime_options"] = {"secret_id": "provider/skilleval"}
    suite = RoutingSuite.model_validate(data)
    assert suite.models[0].api_key_env == "MODEL_API_KEY"


def test_env引用必须是合法环境变量名():
    data = _valid_suite()
    data["models"][0]["api_key_env"] = "not a variable"
    with pytest.raises(ValidationError):
        RoutingSuite.model_validate(data)


def test_docker_image_env解析为固定镜像并进入实际配置(monkeypatch):
    data = _valid_suite()
    data["runtime"] = "openclaw"
    data["skills"]["mode"] = "full"
    data["environment"] = {
        "backend": "docker",
        "image_env": "SKILLEVAL_TEST_IMAGE",
    }
    suite = RoutingSuite.model_validate(data)

    with pytest.raises(ValueError, match="SKILLEVAL_TEST_IMAGE 未设置"):
        resolve_suite_references(suite)

    monkeypatch.setenv("SKILLEVAL_TEST_IMAGE", "latest")
    with pytest.raises(ValueError, match="固定 image"):
        resolve_suite_references(suite)

    pinned = "sha256:" + "a" * 64
    monkeypatch.setenv("SKILLEVAL_TEST_IMAGE", pinned)
    resolved = resolve_suite_references(suite)
    assert resolved["environment"]["image_env"] == "SKILLEVAL_TEST_IMAGE"
    assert resolved["environment"]["image"] == pinned

    monkeypatch.setenv("SKILLEVAL_TEST_IMAGE", "sha256:" + "b" * 64)
    second = resolve_suite_references(suite)
    assert config_hash(resolved) != config_hash(second)


def test_docker_image与image_env不能同时声明():
    data = _valid_suite()
    data["environment"] = {
        "backend": "docker",
        "image": "sha256:" + "a" * 64,
        "image_env": "SKILLEVAL_TEST_IMAGE",
    }
    with pytest.raises(ValidationError, match="只能声明 image 或 image_env"):
        RoutingSuite.model_validate(data)


def test_跟踪的full示例默认Docker且开放完整toolset(monkeypatch):
    monkeypatch.setenv("SKILLEVAL_OPENCLAW_IMAGE", "sha256:" + "b" * 64)
    suite = resolve_suite_references(load_suite("evals/suites/example_full.yaml"))
    assert suite["environment"]["backend"] == "docker"
    assert suite["environment"]["image"] == "sha256:" + "b" * 64
    assert suite["tools"] == ["*"]


def test_full_suite_省略运行时和环境时默认Docker加OpenClaw():
    data = _valid_suite()
    data.pop("runtime")
    data["skills"]["mode"] = "full"
    data["tools"] = ["read", "write"]

    suite = RoutingSuite.model_validate(data).canonical_dict()

    assert suite["runtime"] == "openclaw"
    assert suite["runtime_options"] == {"bin": "openclaw", "profile": "skilleval"}
    assert suite["environment"] == {
        "backend": "docker",
        "image": None,
        "image_env": "SKILLEVAL_OPENCLAW_IMAGE",
        "network": "full",
        "cpus": None,
        "memory": None,
        "env_passthrough": [],
        "options": {},
    }


def test_full_suite_显式local环境仍可覆盖默认值():
    data = _valid_suite()
    data.pop("runtime")
    data["skills"]["mode"] = "full"
    data["tools"] = ["read", "write"]
    data["environment"] = {"backend": "local"}

    suite = RoutingSuite.model_validate(data).canonical_dict()

    assert suite["runtime"] == "openclaw"
    assert suite["environment"]["backend"] == "local"


def test_canonical_config_补默认值且相同语义_hash稳定():
    implicit = RoutingSuite.model_validate(_valid_suite())
    explicit_data = _valid_suite()
    explicit_data.update({"runtime_options": {}, "timeout_seconds": 300})
    explicit = RoutingSuite.model_validate(explicit_data)

    assert implicit.canonical_dict() == explicit.canonical_dict()
    assert config_hash(implicit.canonical_dict()) == config_hash(explicit.canonical_dict())


def test_target只管归属不改变运行config_hash():
    first = RoutingSuite.model_validate(_valid_suite()).canonical_dict()
    second_data = _valid_suite()
    second_data["skills"]["target"] = ["docx"]
    second = RoutingSuite.model_validate(second_data).canonical_dict()

    assert first["skills"]["target"] != second["skills"]["target"]
    assert config_hash(first) == config_hash(second)


def test_parallelism进入config_hash():
    serial = RoutingSuite.model_validate(_valid_suite()).canonical_dict()
    parallel_data = _valid_suite()
    parallel_data["parallelism"] = 4
    parallel = RoutingSuite.model_validate(parallel_data).canonical_dict()
    legacy_serial = dict(serial)
    legacy_serial.pop("parallelism")

    assert serial["parallelism"] == 1
    assert parallel["parallelism"] == 4
    assert config_hash(serial) == config_hash(legacy_serial)
    assert config_hash(serial) != config_hash(parallel)


def test_可导出_json_schema():
    schema = RoutingSuite.model_json_schema()
    assert schema["type"] == "object" and "models" in schema["properties"]


def test_load_suite_拒绝空文件(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_suite(path)


def test_none_mode_实际暴露空catalog_且mock不会误激活():
    data = _valid_suite()
    data["skills"]["mode"] = "none"
    suite = RoutingSuite.model_validate(data).canonical_dict()
    assert resolve_skills(suite) == []

    runtime = MockRuntimeAdapter(expected={"none-rej-01": []})
    result = runtime.run(
        InvocationRequest(
            request_id="r",
            case_id="none-rej-01",
            repeat_index=0,
            prompt="不应激活",
            skills=[],
            skill_mode="none",
            model={"id": "mock"},
        )
    )
    assert result.ok and result.selected_skills == []


def test_include仅暴露指定skill(tmp_path):
    for name in ("alpha", "beta"):
        skill = tmp_path / "skills" / name / "v1"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}\n---\nbody", encoding="utf-8"
        )
    data = _valid_suite()
    data["skills"].update({"dir": str(tmp_path / "skills"), "include": ["alpha"]})
    suite = RoutingSuite.model_validate(data).canonical_dict()
    assert [skill.skill_id for skill in resolve_skills(suite)] == ["alpha"]


def test_yaml_序列化后可无损读回(tmp_path):
    data = _valid_suite()
    path = tmp_path / "suite.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert load_suite(path).canonical_dict() == RoutingSuite.model_validate(data).canonical_dict()
