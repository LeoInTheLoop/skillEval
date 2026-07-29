"""run_routing 编排层的可复现性保护。"""
from __future__ import annotations

import json

from contracts import RunResult, RoutingCase, RuntimeCapabilities, SkillMeta
from workflows.matrix import build_matrix
from workflows.run_routing import config_hash, run_dir, run_one, snapshot_inputs


def test_dataset_hash_进入_config_hash():
    suite = {
        "suite_id": "s",
        "suite_version": "1.0",
        "dataset": "evals/datasets/demo.jsonl",
        "runtime": "mock",
        "skills": {"dir": "skills", "mode": "routing_only", "cfg": "v1"},
        "models": [{"id": "m"}],
        "repeats": 1,
    }
    model = {"id": "m", "model": "mock"}

    assert config_hash(suite, model, dataset_hash="sha256:a") != config_hash(
        suite, model, dataset_hash="sha256:b"
    )


def test_run_dir_为每次执行保留独立归档():
    suite = {"dataset": "evals/datasets/demo.jsonl", "skills": {"cfg": "v1"}}
    assert run_dir(suite, "qwen", "run-a") != run_dir(suite, "qwen", "run-b")
    assert str(run_dir(suite, "qwen", "run-a")).endswith("__qwen__v1/run-a")


def test_归档保存本次实际_dataset_和_skill_内容(tmp_path, monkeypatch):
    monkeypatch.setattr("workflows.run_routing.ROOT", tmp_path)
    dataset = tmp_path / "evals/datasets/demo.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text('{"id":"case-01"}\n', encoding="utf-8")
    source = tmp_path / "source/pdf/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: pdf\n---\nV1 body\n", encoding="utf-8")
    skill = SkillMeta(skill_id="pdf", name="pdf", description="d", content_hash="sha256:x",
                      source_path=str(source))

    archive = tmp_path / "outputs/run"
    archive.mkdir(parents=True)
    snapshot_inputs(archive, {"dataset": "evals/datasets/demo.jsonl"}, [skill])

    assert (archive / "inputs/dataset.jsonl").read_text(encoding="utf-8") == dataset.read_text(encoding="utf-8")
    assert (archive / "inputs/skills/pdf/SKILL.md").read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


class _CaptureRuntime:
    name = "capture"
    version = "test"

    def __init__(self):
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return RunResult(
            case_id=request.case_id,
            repeat_index=request.repeat_index,
            model=request.model["id"],
        )

    def capabilities(self):
        return RuntimeCapabilities(runtime=self.name, skill_modes=["routing_only"])

    def fingerprint(self):
        return {"adapter": "capture-test"}


def test_run_one_使用_matrix_里的_request_和_session_id(tmp_path, monkeypatch):
    suite = {
        "suite_id": "s",
        "suite_version": "1.0",
        "dataset": "evals/datasets/demo.jsonl",
        "runtime": "capture",
        "skills": {"mode": "routing_only", "cfg": "v1"},
        "models": [{"id": "m"}],
        "repeats": 2,
    }
    model = {"id": "m"}
    tasks = build_matrix(
        suite_id="s",
        cases=[RoutingCase(id="pdf-pos-01", prompt="p", expected_skills=["pdf"])],
        models=[model],
        repeats=2,
        execution_id="exec-1",
    )
    runtime = _CaptureRuntime()
    out = tmp_path / "run"
    monkeypatch.setattr("workflows.run_routing.run_dir", lambda *_: out)

    run_one(suite, model, [], tasks, runtime, False, "sha256:dataset")

    assert [request.request_id for request in runtime.requests] == [
        task.request_id for task in tasks
    ]
    assert [request.session_id for request in runtime.requests] == [
        task.session_id for task in tasks
    ]
    persisted = [
        json.loads(line) for line in (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(persisted) == 2


def test_judge_不进_config_hash():
    """judge 是评分阶段的量具，换它不改变 runs.jsonl —— 让它进 hash 会作废历史基准。"""
    from workflows.run_routing import config_hash

    base = {"dataset": "d.jsonl", "scoring": {"metrics": ["exact_set_match"],
                                              "gate": {"exact_set_match": ">= 0.9"}}}
    with_judge = {"dataset": "d.jsonl",
                  "scoring": {"metrics": ["exact_set_match"],
                              "gate": {"exact_set_match": ">= 0.9"},
                              "judge": {"id": "glm5", "model": "openai/glm-5.1"}}}
    assert config_hash(base) == config_hash(with_judge)


def test_换_judge_不改变_config_hash():
    from workflows.run_routing import config_hash

    def suite(jid, model):
        return {"dataset": "d.jsonl",
                "scoring": {"metrics": ["m"], "judge": {"id": jid, "model": model}}}

    assert config_hash(suite("glm5", "openai/glm-5.1")) == \
           config_hash(suite("qwen", "openai/qwen3.7-max"))


def test_改_gate_仍然改变_config_hash():
    """别把 judge 剔除写成把整个 scoring 剔除 —— 门槛变了结果就该判为不可比。"""
    from workflows.run_routing import config_hash

    a = {"scoring": {"metrics": ["m"], "gate": {"top1": ">= 0.9"}}}
    b = {"scoring": {"metrics": ["m"], "gate": {"top1": ">= 0.5"}}}
    assert config_hash(a) != config_hash(b)
