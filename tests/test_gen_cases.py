"""P2 自动出题：薄生成层、严格校验、只写草稿。"""
from __future__ import annotations

import json

import pytest

from contracts import SkillMeta, dataset_review_status, load_cases, load_suite
from workflows.gen_cases import (
    GENERATOR_VERSION,
    REJ_REVIEW_SYSTEM_PROMPT,
    build_generation_prompt,
    build_rej_review_prompt,
    build_suite_draft,
    generate_batch,
    main,
    required_case_types,
    require_routing_metadata,
    resolve_skill_source,
    review_rejections,
    write_draft,
)


def _skill(skill_id: str, source_path: str) -> SkillMeta:
    return SkillMeta(
        skill_id=skill_id,
        name=skill_id,
        description=f"{skill_id} description",
        content_hash=f"sha256:{skill_id}",
        source_path=source_path,
    )


def _valid_response() -> str:
    return json.dumps(
        {
            "cases": [
                {
                    "id": "alpha-pos-01",
                    "prompt": "完成主要任务",
                    "expected_skills": ["alpha"],
                    "tags": ["positive"],
                    "severity": "high",
                },
                {
                    "id": "beta-amb-01",
                    "prompt": "完成边界任务",
                    "expected_skills": ["beta"],
                    "tags": ["ambiguous"],
                    "severity": "medium",
                },
                {
                    "id": "none-rej-01",
                    "prompt": "做一个贴边但无关的咨询",
                    "expected_skills": [],
                    "tags": ["no-skill"],
                    "severity": "low",
                },
                {
                    "id": "alpha+beta-multi-01",
                    "prompt": "先完成甲任务再完成乙任务",
                    "expected_skills": ["alpha", "beta"],
                    "tags": ["multi-skill"],
                    "severity": "high",
                },
            ],
            "review_notes": ["人工复核 beta-amb-01 的边界"],
            "rejection_notes": [
                {
                    "case_id": "none-rej-01",
                    "why_not": "alpha 与 beta 都不覆盖这类咨询请求",
                }
            ],
        },
        ensure_ascii=False,
    )


def test_单skill目录默认隔离相邻catalog(tmp_path):
    for name in ("alpha", "beta"):
        directory = tmp_path / name / "v1"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} desc\n---\n正文",
            encoding="utf-8",
        )

    root, skills, targets = resolve_skill_source(tmp_path / "alpha")

    assert root == tmp_path
    assert [skill.skill_id for skill in skills] == ["alpha"]
    assert targets == ["alpha"]
    assert required_case_types(skills) == ("pos", "amb", "rej")

    _, with_neighbors, _ = resolve_skill_source(tmp_path / "alpha", include_neighbors=True)
    assert [skill.skill_id for skill in with_neighbors] == ["alpha", "beta"]
    assert required_case_types(with_neighbors) == ("pos", "amb", "rej", "multi")


def test_缺description的目标skill在调用模型前被拒绝(tmp_path):
    skill = _skill("alpha", str(tmp_path / "alpha/SKILL.md"))
    skill.description = ""
    with pytest.raises(ValueError, match="缺少 routing metadata"):
        require_routing_metadata([skill], ["alpha"])


def test_generate_batch_调用注入的completion并在写文件前校验(tmp_path):
    skills = [
        _skill("alpha", str(tmp_path / "alpha/SKILL.md")),
        _skill("beta", str(tmp_path / "beta/SKILL.md")),
    ]
    captured = {}

    def completion(**kwargs):
        captured.update(kwargs)
        return f"```json\n{_valid_response()}\n```"

    batch, prompt, required = generate_batch(
        skills=skills,
        target_skill_ids=["alpha"],
        acceptance="alpha 任务做完算通过",
        case_count=4,
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={"temperature": 0},
        completion=completion,
    )

    assert len(batch.cases) == 4
    assert required == ("pos", "amb", "rej", "multi")
    assert "[业务目标与验收标准]" in prompt
    assert captured["model"] == "openai/test"


def test_十题默认配比压低multi且rej禁止泄漏答案(tmp_path):
    skills = [
        _skill("alpha", str(tmp_path / "alpha/SKILL.md")),
        _skill("beta", str(tmp_path / "beta/SKILL.md")),
    ]
    prompt = build_generation_prompt(
        skills=skills,
        target_skill_ids=["alpha"],
        acceptance="验收",
        case_count=10,
        required_types=("pos", "amb", "rej", "multi"),
    )

    assert '{"pos": 4, "amb": 3, "rej": 2, "multi": 1}' in prompt
    assert "禁止写“不需要画图" in prompt


def test_generate_batch_未知gold在生成阶段被拒绝(tmp_path):
    skills = [_skill("alpha", str(tmp_path / "alpha/SKILL.md"))]
    payload = json.loads(_valid_response())
    payload["cases"] = [
        {
            "id": "ghost-pos-01",
            "prompt": "不存在的能力",
            "expected_skills": ["ghost"],
        },
        {"id": "alpha-amb-01", "prompt": "边界", "expected_skills": ["alpha"]},
        {"id": "none-rej-01", "prompt": "拒答", "expected_skills": []},
    ]

    with pytest.raises(ValueError, match="不存在的 skill"):
        generate_batch(
            skills=skills,
            target_skill_ids=["alpha"],
            acceptance="验收",
            case_count=3,
            model="openai/test",
            api_base_env="TEST_BASE",
            api_key_env="TEST_KEY",
            params={"temperature": 0},
            completion=lambda **_: json.dumps(payload, ensure_ascii=False),
        )


def test_write_draft_产出可加载_dataset_和严格suite(tmp_path):
    skills = [
        _skill("alpha", str(tmp_path / "catalog/alpha/SKILL.md")),
        _skill("beta", str(tmp_path / "catalog/beta/SKILL.md")),
    ]
    batch, prompt, _ = generate_batch(
        skills=skills,
        target_skill_ids=["alpha"],
        acceptance="验收",
        case_count=4,
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={"temperature": 0},
        completion=lambda **_: _valid_response(),
    )
    output = tmp_path / "draft"
    suite = build_suite_draft(
        catalog_root=tmp_path / "catalog",
        dataset_path=output / "dataset.jsonl",
        scope="alpha",
        model_id="test",
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={"temperature": 0},
        target_skill_ids=["alpha"],
        include_skill_ids=["alpha", "beta"],
    )

    dataset_path, suite_path = write_draft(
        output_dir=output,
        batch=batch,
        suite=suite,
        prompt=prompt,
        acceptance="验收",
        skills=skills,
        model="openai/test",
        params={"temperature": 0},
        scope="alpha",
    )

    assert len(load_cases(dataset_path)) == 4
    assert load_suite(suite_path).suite_version == "0.1-draft"
    assert load_suite(suite_path).skills.target == ["alpha"]
    assert load_suite(suite_path).skills.include == ["alpha", "beta"]


def test_gen_cases_count过小会在调用模型前给出邻居模式提示(tmp_path, monkeypatch, capsys):
    import sys

    catalog = tmp_path / "catalog"
    for name in ("alpha", "beta"):
        directory = catalog / name / "v1"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} 的能力描述\n---\n正文",
            encoding="utf-8",
        )

    monkeypatch.setattr(sys, "argv", [
        "gen_cases",
        "--skill-dir", str(catalog / "alpha"),
        "--acceptance", "验收标准",
        "--count", "3",
        "--include-neighbors",
    ])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    assert "至少设为 4" in capsys.readouterr().err


def _rej_review_skills(tmp_path) -> list[SkillMeta]:
    return [
        _skill("alpha", str(tmp_path / "catalog/alpha/SKILL.md")),
        _skill("beta", str(tmp_path / "catalog/beta/SKILL.md")),
    ]


def _draft_with_review(tmp_path, skills, review, output_name="draft"):
    """走一遍真实写盘路径，返回 (dataset 路径, REVIEW.md 文本)。"""
    batch, prompt, _ = generate_batch(
        skills=skills,
        target_skill_ids=["alpha"],
        acceptance="验收",
        case_count=4,
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={"temperature": 0},
        completion=lambda **_: _valid_response(),
    )
    output = tmp_path / output_name
    suite = build_suite_draft(
        catalog_root=tmp_path / "catalog",
        dataset_path=output / "dataset.jsonl",
        scope="alpha",
        model_id="test",
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={"temperature": 0},
        target_skill_ids=["alpha"],
        include_skill_ids=["alpha", "beta"],
    )
    dataset_path, _ = write_draft(
        output_dir=output,
        batch=batch,
        suite=suite,
        prompt=prompt,
        acceptance="验收",
        skills=skills,
        model="openai/test",
        params={"temperature": 0},
        scope="alpha",
        rej_review=review,
    )
    return dataset_path, (output / "REVIEW.md").read_text(encoding="utf-8")


def _review(tmp_path, completion) -> object:
    return review_rejections(
        skills=_rej_review_skills(tmp_path),
        cases=load_cases(_valid_response_dataset(tmp_path)),
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={"temperature": 0},
        completion=completion,
    )


def _valid_response_dataset(tmp_path) -> str:
    """把 _valid_response 的题落成 jsonl，好让复审拿到真的 RoutingCase。"""
    path = tmp_path / "cases.jsonl"
    payload = json.loads(_valid_response())
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in payload["cases"]) + "\n",
        encoding="utf-8",
    )
    return str(path)


def test_rej复审是盲判_不给gold也不给生成器自述的理由(tmp_path):
    cases = load_cases(_valid_response_dataset(tmp_path))
    prompt = build_rej_review_prompt(
        skills=_rej_review_skills(tmp_path),
        cases=[case for case in cases if case.case_type == "rej"],
    )

    assert "none-rej-01" in prompt
    # 给了 gold 或生成时的 why_not，复审就只会确认它自己刚写的答案
    assert "expected_skills" not in prompt
    assert "都不覆盖这类咨询请求" not in prompt
    assert "alpha-pos-01" not in prompt          # 只送 rej 题，不为无关题付费


def test_复审点名skill时草稿照常写出但争议顶到REVIEW最前面(tmp_path):
    review = _review(
        tmp_path,
        lambda **_: json.dumps(
            {"verdicts": [{
                "case_id": "none-rej-01",
                "should_activate": ["beta"],
                "why": "beta 的能力描述覆盖这类咨询",
            }]},
            ensure_ascii=False,
        ),
    )
    assert [verdict.case_id for verdict in review.disputed] == ["none-rej-01"]

    dataset_path, review_md = _draft_with_review(tmp_path, _rej_review_skills(tmp_path), review)

    # 只标注、不阻断：草稿照常可加载，仍然停在 DRAFT 门
    assert len(load_cases(dataset_path)) == 4
    assert dataset_review_status(dataset_path) == "DRAFT"
    assert "争议 1 道：none-rej-01" in dataset_path.read_text(encoding="utf-8")
    # 独立复审的反对意见必须排在生成器自证的 rejection_notes 之前
    assert review_md.index("交叉复审认为") < review_md.index("必审 · 每道 rej")
    assert "`beta`" in review_md


def test_复审失败不丢草稿_也不伪装成无争议(tmp_path):
    # 多行 pydantic 报错曾把后面的 `# review_status: DRAFT` 挤出注释区，DRAFT 门当场失效
    review = _review(tmp_path, lambda **_: json.dumps({"cases": []}))

    assert review.error and not review.disputed
    dataset_path, review_md = _draft_with_review(tmp_path, _rej_review_skills(tmp_path), review)

    assert dataset_review_status(dataset_path) == "DRAFT"
    header = [line for line in dataset_path.read_text(encoding="utf-8").splitlines()
              if line.startswith("# rejection_review:")]
    assert len(header) == 1 and "FAILED" in header[0]
    assert "rej 交叉复审未完成" in review_md


def test_复审给出catalog外的skill_id算复审失败而不是有效异议(tmp_path):
    review = _review(
        tmp_path,
        lambda **_: json.dumps(
            {"verdicts": [{"case_id": "none-rej-01",
                           "should_activate": ["ghost"],
                           "why": "编出来的 skill"}]},
            ensure_ascii=False,
        ),
    )

    assert review.disputed == []
    assert "ghost" in review.error


def test_复审用的是另一套system_prompt(tmp_path):
    seen = {}
    _review(tmp_path, lambda **kwargs: seen.update(kwargs) or json.dumps({"verdicts": []}))

    assert seen["system"] == REJ_REVIEW_SYSTEM_PROMPT


def test_跳过复审的草稿明确标注没人复核过rej(tmp_path):
    dataset_path, review_md = _draft_with_review(tmp_path, _rej_review_skills(tmp_path), None)

    assert "SKIPPED（未启用交叉复审）" in dataset_path.read_text(encoding="utf-8")
    assert "未做 rej 交叉复审" in review_md
    assert dataset_review_status(dataset_path) == "DRAFT"


def test_三十题生成被契约允许():
    skills = [_skill("alpha", "/tmp/alpha/SKILL.md")]
    prompt = build_generation_prompt(
        skills=skills,
        target_skill_ids=["alpha"],
        acceptance="验收",
        case_count=30,
        required_types=required_case_types(skills),
    )
    assert "一共 30 道" in prompt
