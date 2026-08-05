"""Pipeline preflight is read-only and must agree with the runner's naming."""
from __future__ import annotations

from pathlib import Path

from pipeline.plan import build_plan, render_plan
from contracts import SkillMeta, dataset_review_status, require_approved_dataset
import json
import pytest
import yaml


def test_plan_expands_suite_without_a_model_call(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    # The developer's .env may contain a real key; the preflight test must not
    # depend on it or load it just to assert a missing-credential plan.
    monkeypatch.setattr("pipeline.plan.load_dotenv", lambda *_: False)

    plan = build_plan("evals/suites/example_routing.yaml")

    assert plan.cases == 14
    assert plan.repeats == 2
    assert len(plan.models) == 1
    assert plan.models[0].tasks == 0
    assert plan.models[0].credential == "missing"
    assert plan.models[0].result_dir == (
        "outputs/routing_example_v1.0__qwen3.7-max-2026-05-17__v1/<execution-id>"
    )
    assert not plan.runnable
    assert "no runnable model" in plan.blocked_reasons[-1]


def test_mock_plan_is_runnable_and_shows_the_same_workload():
    plan = build_plan("evals/suites/example_routing.yaml", mock=True)

    assert plan.runnable
    assert plan.models[0].id == "mock"
    assert plan.models[0].tasks == plan.cases * plan.repeats
    assert plan.models[0].result_dir.endswith("__mock__v1/<execution-id>")
    rendered = render_plan(plan)
    assert "modules to run:" in rendered
    assert "results always live under project outputs/" in rendered
    assert "RUN MODE: SYNTHETIC MOCK" in rendered
    assert "none — mock never calls a model endpoint" in rendered
    assert "--confirm --mock" in rendered


def test_plan_with_execution_id_previews_the_exact_immutable_directory():
    plan = build_plan("evals/suites/example_routing.yaml", mock=True, execution_id="trial-01")

    assert plan.execution_id == "trial-01"
    assert plan.models[0].result_dir.endswith("__mock__v1/trial-01")


def test_draft_dataset_is_blocked_but_hand_authored_dataset_is_not(tmp_path):
    draft = tmp_path / "draft.jsonl"
    draft.write_text("# review_status: DRAFT — 未审核\n{}\n", encoding="utf-8")
    assert dataset_review_status(draft) == "DRAFT"
    with pytest.raises(ValueError, match="测试集仍是 DRAFT"):
        require_approved_dataset(draft)

    handwritten = tmp_path / "handwritten.jsonl"
    handwritten.write_text('{"id":"none-rej-01","prompt":"x"}\n', encoding="utf-8")
    assert dataset_review_status(handwritten) is None
    require_approved_dataset(handwritten)


def test_endpoint_health_把_dns_故障变成可操作的预检提示(monkeypatch):
    import socket

    from pipeline.plan import endpoint_health

    monkeypatch.setenv("MODEL_BASE", "https://api.example.test/v1")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(socket.gaierror("no DNS")))

    healthy, detail = endpoint_health({"id": "model-a", "api_base_env": "MODEL_BASE"})

    assert not healthy
    assert "MODEL_BASE" in detail
    assert "network/DNS" in detail


def test_real_plan_明确列出外发内容和单独授权(monkeypatch):
    monkeypatch.setenv("MODEL_BASE", "https://api.example.test/v1")
    from pipeline.plan import PlannedModel, build_egress_manifest

    model = PlannedModel(
        id="model-a",
        model="provider/model-a",
        params={},
        api_base_env="MODEL_BASE",
        api_key_env="MODEL_KEY",
        credential="configured",
        result_dir="outputs/x",
        config_hash="sha256:x",
        tasks=6,
    )
    manifest = build_egress_manifest(
        {
            "skills": {"mode": "routing_only"},
            "tools": [],
            "repeats": 2,
        },
        [
            # Payload values are deliberately absent from the manifest: it
            # describes categories without printing private prompts.
            type("Case", (), {"files": []})(),
            type("Case", (), {"files": []})(),
            type("Case", (), {"files": []})(),
        ],
        [model],
        mock=False,
    )

    assert manifest["approval_required"] is True
    assert manifest["planned_requests"] == 6
    assert manifest["destinations"] == [
        {"model_id": "model-a", "endpoint": "api.example.test"}
    ]
    assert "skill metadata only" in " ".join(manifest["payload_categories"])
    assert manifest["manifest_hash"].startswith("sha256:")


def test_历史run引用过的版本内容改变后拒绝继续沿用同一版本(tmp_path):
    from pipeline.plan import skill_version_drift_errors

    snapshot = tmp_path / "outputs" / "old-run" / "config.snapshot.yaml"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(yaml.safe_dump({
        "suite": {
            "skills": {
                "dir": "subjects",
                "versions": {"alpha": "v2"},
            }
        },
        "skills": {"alpha": "sha256:old"},
    }), encoding="utf-8")
    skill = SkillMeta(
        skill_id="alpha",
        name="alpha",
        description="desc",
        content_hash="sha256:new",
        source_path="subjects/alpha/v2/SKILL.md",
        version="v2",
    )

    errors = skill_version_drift_errors(
        {"skills": {"dir": "subjects", "versions": {"alpha": "v2"}}},
        [skill],
        tmp_path / "outputs",
    )

    assert len(errors) == 1
    assert "alpha@v2" in errors[0]
    assert "新版本目录" in errors[0]


def test_不同skill根目录的同名版本不被误判为漂移(tmp_path):
    from pipeline.plan import skill_version_drift_errors

    snapshot = tmp_path / "outputs" / "old-run" / "config.snapshot.yaml"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(yaml.safe_dump({
        "suite": {"skills": {"dir": "legacy-skills"}},
        "skills": {"alpha": "sha256:old"},
    }), encoding="utf-8")
    skill = SkillMeta(
        skill_id="alpha",
        name="alpha",
        description="desc",
        content_hash="sha256:new",
        source_path="subjects/alpha/v1/SKILL.md",
        version="v1",
    )

    assert skill_version_drift_errors(
        {"skills": {"dir": "subjects"}},
        [skill],
        tmp_path / "outputs",
    ) == []


def _installed_skill(root, name="alpha"):
    source = root / "installed_skills" / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 的路由能力\n"
        "triggers: [明确任务]\nexclusions: [普通聊天]\n---\n正文",
        encoding="utf-8",
    )
    return source


def _generated_alpha_cases():
    return json.dumps({
        "cases": [
            {
                "id": "alpha-pos-01",
                "prompt": "请完成明确任务",
                "expected_skills": ["alpha"],
                "severity": "high",
            },
            {
                "id": "alpha-amb-01",
                "prompt": "这个请求可能需要专门能力",
                "expected_skills": ["alpha"],
                "severity": "medium",
            },
            {
                "id": "none-rej-01",
                "prompt": "今天天气怎么样",
                "expected_skills": [],
                "severity": "low",
            },
        ],
        "review_notes": ["复核边界题"],
        "rejection_notes": [
            {"case_id": "none-rej-01", "why_not": "不属于 alpha 的任务范围"}
        ],
    }, ensure_ascii=False)


def _fake_generator():
    """init 会连打两次：先出题，再拿 rej 题面回去盲判。两次必须分别应答。"""
    from workflows.gen_cases import REJ_REVIEW_SYSTEM_PROMPT

    def completion(*, system="", **_):
        if system == REJ_REVIEW_SYSTEM_PROMPT:
            return json.dumps({"verdicts": [
                {"case_id": "none-rej-01", "should_activate": [],
                 "why": "闲聊不落在 alpha 的能力范围内"}
            ]}, ensure_ascii=False)
        return _generated_alpha_cases()

    return completion


def test_pipeline_init_plan只读且明确生成器外发内容(tmp_path, monkeypatch):
    from pipeline.initialize import build_init_plan, render_init_plan

    source = _installed_skill(tmp_path)
    monkeypatch.setenv("MODEL_KEY", "real-test-key")
    monkeypatch.setenv("MODEL_BASE", "https://generator.example.test/v1")
    monkeypatch.setattr("pipeline.initialize.load_dotenv", lambda *_: False)

    plan = build_init_plan(
        source_path=source,
        acceptance="明确任务应激活；普通聊天拒绝",
        dest_root=tmp_path / "subjects",
        output_dir=tmp_path / "draft",
        count=3,
        api_base_env="MODEL_BASE",
        api_key_env="MODEL_KEY",
    )

    assert plan.runnable
    assert plan.snapshot_state == "new"
    assert not (tmp_path / "subjects").exists()
    assert not (tmp_path / "draft").exists()
    rendered = render_init_plan(plan)
    assert "no files written, no model call" in rendered
    assert "business goal and acceptance criteria" in rendered
    assert "SKILL.md body" in rendered and "do not send" in rendered
    # 出题 + 条件式结构修复 + rej gold 盲判复审：按最大外发量事先申报。
    assert plan.egress["planned_requests"] == 3
    assert "up to 3 requests" in rendered
    assert "invalid generated candidate" in rendered
    assert "blind gold cross-review" in rendered


def test_pipeline_init复用现有模块并停在人工审核门(tmp_path, monkeypatch):
    from pipeline.initialize import build_init_plan, execute_init

    source = _installed_skill(tmp_path)
    monkeypatch.setenv("MODEL_KEY", "real-test-key")
    monkeypatch.setenv("MODEL_BASE", "https://generator.example.test/v1")
    monkeypatch.setattr("pipeline.initialize.load_dotenv", lambda *_: False)
    acceptance = "明确任务应激活；普通聊天拒绝"
    plan = build_init_plan(
        source_path=source,
        acceptance=acceptance,
        dest_root=tmp_path / "subjects",
        output_dir=tmp_path / "draft",
        count=3,
        api_base_env="MODEL_BASE",
        api_key_env="MODEL_KEY",
    )

    dataset, suite = execute_init(
        plan,
        acceptance=acceptance,
        api_base_env="MODEL_BASE",
        api_key_env="MODEL_KEY",
        completion=_fake_generator(),
    )

    assert (tmp_path / "subjects" / "alpha" / "v1" / "SKILL.md").is_file()
    assert dataset.is_file() and suite.is_file()
    assert dataset_review_status(dataset) == "DRAFT"
    assert "争议 0 道" in dataset.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="测试集仍是 DRAFT"):
        require_approved_dataset(dataset)
    suite_data = yaml.safe_load(suite.read_text(encoding="utf-8"))
    assert suite_data["skills"]["dir"] == str((tmp_path / "subjects").resolve())
    assert suite_data["skills"]["include"] == ["alpha"]


def test_pipeline_init生成失败后快照可安全复用(tmp_path, monkeypatch):
    from pipeline.initialize import build_init_plan, execute_init

    source = _installed_skill(tmp_path)
    # SkillHub installations carry their own root _meta.json.  Import replaces
    # that transport metadata with skillEval provenance, so comparison must
    # exclude it on both sides rather than treating the retry as a conflict.
    (source / "_meta.json").write_text(
        json.dumps({"slug": "alpha", "version": "1.2.3"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_KEY", "real-test-key")
    monkeypatch.setenv("MODEL_BASE", "https://generator.example.test/v1")
    monkeypatch.setattr("pipeline.initialize.load_dotenv", lambda *_: False)
    kwargs = {
        "source_path": source,
        "acceptance": "明确任务应激活；普通聊天拒绝",
        "dest_root": tmp_path / "subjects",
        "output_dir": tmp_path / "draft",
        "count": 3,
        "api_base_env": "MODEL_BASE",
        "api_key_env": "MODEL_KEY",
    }
    first = build_init_plan(**kwargs)

    with pytest.raises(RuntimeError, match="快照已就绪"):
        execute_init(
            first,
            acceptance=kwargs["acceptance"],
            api_base_env="MODEL_BASE",
            api_key_env="MODEL_KEY",
            completion=lambda **_: (_ for _ in ()).throw(ConnectionError("down")),
        )

    assert Path(first.snapshot_destination).is_dir()
    assert not Path(first.draft_output).exists()
    imported_meta = json.loads(
        (Path(first.snapshot_destination) / "_meta.json").read_text(encoding="utf-8")
    )
    assert imported_meta["snapshot_content_hash"].startswith("sha256:")
    assert imported_meta["upstream_meta_sha256"].startswith("sha256:")
    retry = build_init_plan(**kwargs)
    assert retry.runnable
    assert retry.snapshot_state == "reusable"

    # Updating registry/install metadata alone does not alter evaluated skill
    # content and remains reusable.
    (source / "_meta.json").write_text(
        json.dumps({"slug": "alpha", "version": "1.2.4"}),
        encoding="utf-8",
    )
    assert build_init_plan(**kwargs).snapshot_state == "reusable"


def test_pipeline_init仍拒绝复用内容不同的快照(tmp_path, monkeypatch):
    from pipeline.initialize import build_init_plan, execute_init

    source = _installed_skill(tmp_path)
    (source / "_meta.json").write_text(
        json.dumps({"slug": "alpha", "version": "1.2.3"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_KEY", "real-test-key")
    monkeypatch.setenv("MODEL_BASE", "https://generator.example.test/v1")
    monkeypatch.setattr("pipeline.initialize.load_dotenv", lambda *_: False)
    kwargs = {
        "source_path": source,
        "acceptance": "明确任务应激活；普通聊天拒绝",
        "dest_root": tmp_path / "subjects",
        "output_dir": tmp_path / "draft",
        "count": 3,
        "api_base_env": "MODEL_BASE",
        "api_key_env": "MODEL_KEY",
    }
    first = build_init_plan(**kwargs)
    with pytest.raises(RuntimeError):
        execute_init(
            first,
            acceptance=kwargs["acceptance"],
            api_base_env="MODEL_BASE",
            api_key_env="MODEL_KEY",
            completion=lambda **_: (_ for _ in ()).throw(ConnectionError("down")),
        )

    (source / "SKILL.md").write_text(
        (source / "SKILL.md").read_text(encoding="utf-8") + "\n真实能力变化\n",
        encoding="utf-8",
    )
    retry = build_init_plan(**kwargs)

    assert retry.snapshot_state == "conflict"
    assert not retry.runnable
    assert "拒绝覆盖" in " ".join(retry.blocked_reasons)
