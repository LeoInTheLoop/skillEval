"""N0 Contract 验收测试（AGENTS.md §6.5）。

覆盖：严格类型验证 / 拒绝非法枚举 / 拒绝未声明字段 / JSON Schema 可导出 /
序列化往返一致 / routing-only 不读正文 / 内容变化则 hash 变化。
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from contracts import (
    CaseSetValidationError,
    RoutingCase,
    RoutingRun,
    SkillMeta,
    build_catalog,
    diff_case_sets,
    load_cases,
    load_skills,
    validate_case_set,
)

SKILL_MD = """---
name: demo
description: 演示用 skill
triggers: [a, b]
exclusions: [c]
---

# 正文标题

这是**正文**，routing-only 模式绝不能读到这里。
"""


# ---- 严格性 ----

def test_未声明字段被拒绝():
    with pytest.raises(ValidationError):
        RoutingCase(id="x-pos-01", prompt="p", 未知字段="v")


def test_非法_severity_被拒绝():
    with pytest.raises(ValidationError):
        RoutingCase(id="x-pos-01", prompt="p", severity="urgent")


@pytest.mark.parametrize("sev", ["low", "medium", "high", "critical"])
def test_合法_severity_被接受(sev):
    assert RoutingCase(id="x-pos-01", prompt="p", severity=sev).severity == sev


def test_expected_skills_默认空表示_no_skill():
    assert RoutingCase(id="none-rej-01", prompt="p").expected_skills == []


# ---- Schema 与往返 ----

@pytest.mark.parametrize("model", [SkillMeta, RoutingCase, RoutingRun])
def test_可导出_json_schema(model):
    schema = model.model_json_schema()
    assert schema["type"] == "object" and schema["properties"]


def test_序列化往返一致():
    r = RoutingRun(case_id="pdf-pos-01", repeat_index=0, model="m",
                   selected_skills=["pdf"], reasoning="因为是 pdf")
    assert RoutingRun.model_validate_json(r.model_dump_json()) == r


# ---- Skill 加载：routing-only ----

@pytest.fixture
def skill_dir(tmp_path):
    (tmp_path / "demo" / "v1").mkdir(parents=True)
    (tmp_path / "demo" / "v1" / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return tmp_path


def test_只读_frontmatter_不含正文(skill_dir):
    """AGENTS.md §7.3：routing-only 不得读取正文。"""
    s = load_skills(skill_dir)[0]
    assert s.skill_id == "demo" and s.triggers == ["a", "b"] and s.exclusions == ["c"]
    blob = s.model_dump_json()
    assert "正文" not in blob and "正文标题" not in blob


def test_内容变化则_hash_变化(skill_dir):
    before = load_skills(skill_dir)[0].content_hash
    (skill_dir / "demo" / "v1" / "SKILL.md").write_text(
        SKILL_MD.replace("演示用 skill", "改过的描述"), encoding="utf-8")
    assert load_skills(skill_dir)[0].content_hash != before


def test_重复导入幂等(skill_dir):
    """AGENTS.md §7.4：同一 skill 重复导入结果幂等。"""
    assert load_skills(skill_dir) == load_skills(skill_dir)


def test_按版本钉选且不修改源版本(skill_dir):
    v2 = skill_dir / "demo" / "v2"
    v2.mkdir()
    changed = SKILL_MD.replace("演示用 skill", "V2 描述")
    (v2 / "SKILL.md").write_text(changed, encoding="utf-8")
    source_before = (skill_dir / "demo" / "v1" / "SKILL.md").read_text(encoding="utf-8")

    skills = load_skills(skill_dir, versions={"demo": "v2"})

    assert [(skill.skill_id, skill.version, skill.description) for skill in skills] == [
        ("demo", "v2", "V2 描述")
    ]
    assert (skill_dir / "demo" / "v1" / "SKILL.md").read_text(encoding="utf-8") == source_before


def test_未钉版本按数字顺序取最小值以固定基线(skill_dir):
    v10 = skill_dir / "demo" / "v10"
    v10.mkdir()
    (v10 / "SKILL.md").write_text(
        SKILL_MD.replace("演示用 skill", "V10 描述"), encoding="utf-8"
    )

    assert [(skill.version, skill.description) for skill in load_skills(skill_dir)] == [
        ("v1", "演示用 skill")
    ]


def test_exclude_目标必须存在(skill_dir):
    assert load_skills(skill_dir, exclude=["demo"]) == []
    with pytest.raises(ValueError, match="不存在"):
        load_skills(skill_dir, exclude=["missing"])


def test_catalog_只包含_routing_metadata(skill_dir):
    catalog = build_catalog(load_skills(skill_dir))
    assert "demo" in catalog and "触发词: a, b" in catalog and "排除: c" in catalog
    assert "正文" not in catalog


# ---- Case 加载 ----

def test_load_cases_跳过注释与空行(tmp_path):
    f = tmp_path / "c.jsonl"
    f.write_text(
        "# 注释\n\n"
        + json.dumps({"id": "pdf-pos-01", "prompt": "p", "expected_skills": ["pdf"]})
        + "\n\n", encoding="utf-8")
    cases = load_cases(f)
    assert len(cases) == 1 and cases[0].id == "pdf-pos-01"


def test_load_cases_遇到非法行报错(tmp_path):
    f = tmp_path / "c.jsonl"
    f.write_text(json.dumps({"id": "x", "prompt": "p", "乱七八糟": 1}) + "\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_cases(f)


def test_case_type_来自_id_倒数第二段():
    assert RoutingCase(id="pdf-amb-01", prompt="p").case_type == "amb"
    assert RoutingCase(id="short", prompt="p").case_type == "?"


def test_case_stage_支持四层并兼容旧题推断():
    assert RoutingCase(id="x-pos-01", prompt="p", stage="trigger").case_stage == "trigger"
    assert RoutingCase(
        id="x-pos-01", prompt="p", expect_tools=["write"]
    ).case_stage == "logic"
    assert RoutingCase(
        id="x-pos-01", prompt="p", expect_artifacts=["out/a.md"]
    ).case_stage == "artifact"
    assert RoutingCase(
        id="x-pos-01", prompt="p", tags=["failure"]
    ).case_stage == "failure"


def test_case_stage_非法值被拒绝():
    with pytest.raises(ValidationError):
        RoutingCase(id="x-pos-01", prompt="p", stage="security")


# ---- P2 生成集跨题校验 ----

def _generated_cases():
    return [
        RoutingCase(id="pdf-pos-01", prompt="合并这两份扫描件", expected_skills=["pdf"]),
        RoutingCase(id="pdf-amb-01", prompt="整理这份材料", expected_skills=["pdf"]),
        RoutingCase(id="none-rej-01", prompt="分析合同风险", expected_skills=[]),
        RoutingCase(
            id="pdf+xlsx-multi-01",
            prompt="抽表后做汇总",
            expected_skills=["pdf", "xlsx"],
        ),
    ]


def test_validate_case_set_接受四类齐全的生成集():
    cases = _generated_cases()
    assert validate_case_set(
        cases,
        skill_ids={"pdf", "xlsx"},
        required_types=("pos", "amb", "rej", "multi"),
        max_cases=10,
    ) == cases


def test_validate_case_set_一次报告未知_gold_缺类_和重复_prompt():
    cases = [
        RoutingCase(id="ghost-pos-01", prompt=" 同一句 ", expected_skills=["ghost"]),
        RoutingCase(id="none-rej-01", prompt="同一句", expected_skills=[]),
    ]
    with pytest.raises(CaseSetValidationError) as caught:
        validate_case_set(
            cases,
            skill_ids={"pdf"},
            required_types=("pos", "amb", "rej", "multi"),
        )
    message = str(caught.value)
    assert "不存在的 skill" in message
    assert "同 prompt 不同 gold" in message
    assert "题型配比缺类" in message


@pytest.mark.parametrize(
    ("case", "message"),
    [
        (RoutingCase(id="none-rej-01", prompt="p", expected_skills=["pdf"]), "rej"),
        (RoutingCase(id="pdf-pos-01", prompt="p", expected_skills=[]), "pos"),
        (RoutingCase(id="pdf-multi-01", prompt="p", expected_skills=["pdf"]), "multi"),
        (RoutingCase(id="bad", prompt="p", expected_skills=[]), "id 必须符合"),
    ],
)
def test_validate_case_set_拒绝题型与_gold_矛盾(case, message):
    with pytest.raises(CaseSetValidationError, match=message):
        validate_case_set([case], skill_ids={"pdf"}, required_types=())


def test_validate_case_set_拒绝id_scope缩写或与gold不一致():
    case = RoutingCase(
        id="iad-pos-01",
        prompt="画架构图",
        expected_skills=["interactive-architecture-diagram"],
    )
    with pytest.raises(CaseSetValidationError, match="id scope=.*不一致"):
        validate_case_set(
            [case],
            skill_ids={"interactive-architecture-diagram"},
            required_types=(),
        )


def test_diff_case_sets_解释_prompt_gold_新增与缺失():
    reference = [
        RoutingCase(id="pdf-pos-01", prompt="旧", expected_skills=["pdf"]),
        RoutingCase(id="none-rej-01", prompt="删", expected_skills=[]),
    ]
    candidate = [
        RoutingCase(id="pdf-pos-01", prompt="新", expected_skills=[]),
        RoutingCase(id="xlsx-pos-01", prompt="加", expected_skills=["xlsx"]),
    ]
    assert diff_case_sets(reference, candidate) == [
        {
            "case_id": "none-rej-01",
            "kind": "missing",
        },
        {
            "case_id": "pdf-pos-01",
            "kind": "prompt+gold",
            "reference_gold": ["pdf"],
            "candidate_gold": [],
        },
        {
            "case_id": "xlsx-pos-01",
            "kind": "added",
        },
    ]
