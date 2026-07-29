"""两级路由输入：direct 先调 metadata，production 再测真实上下文。"""
from __future__ import annotations

import pytest

from adapters.routing_inputs import available, create_routing_input
from adapters.runtimes.litellm import LiteLLMRuntimeAdapter
from contracts import (
    ContextMessage,
    ContextTool,
    InvocationRequest,
    RoutingCase,
    RoutingContext,
    SkillMeta,
)


def _skill(skill_id: str, description: str) -> SkillMeta:
    return SkillMeta(
        skill_id=skill_id,
        name=skill_id,
        description=description,
        source_path=f"/skills/{skill_id}/SKILL.md",
        content_hash=f"sha256:{skill_id}",
    )


def test_legacy_case_自动补空上下文():
    case = RoutingCase(id="pdf-pos-01", prompt="拆这个 PDF", expected_skills=["pdf"])
    assert case.context == RoutingContext()


def _contextual_request() -> InvocationRequest:
    return InvocationRequest(
        request_id="r1",
        case_id="interactive-architecture-diagram-pos-01",
        repeat_index=0,
        prompt="好，就按刚才说的那个做。",
        skills=[
            _skill("interactive-architecture-diagram", "把复杂系统转成架构图"),
            _skill("pptx", "制作演示文稿"),
        ],
        context=RoutingContext(
            role_prompt="你是生产研发助手，先理解未完成意图再选择能力。",
            long_context="项目是支付网关；前文还包含很长的服务依赖说明。",
            messages=[
                ContextMessage(role="user", content="把这套服务依赖画成一张架构图"),
                ContextMessage(role="assistant", content="我会按调用链分层绘制，确认后开始。"),
                ContextMessage(role="tool", name="repo_search", content="找到 gateway/payment"),
            ],
            tools=[
                ContextTool(name="read_file", description="读取仓库文件"),
                ContextTool(
                    name="search_pages",
                    description="搜索知识库",
                    source="mcp",
                    server="notion",
                    input_schema={"type": "object"},
                ),
            ],
        ),
        model={"id": "m", "model": "openai/demo"},
    )



def test_routing_input_工厂有两个阶段():
    assert available() == ["direct", "production_context"]


def test_direct_只看metadata和当前问题_忽略生产上下文():
    request = _contextual_request()
    messages = create_routing_input("direct").build_messages(request)

    blob = "\n".join(item["content"] for item in messages)
    assert "interactive-architecture-diagram" in blob
    assert "好，就按刚才说的那个做。" in blob
    assert "生产研发助手" not in blob
    assert "项目是支付网关" not in blob
    assert "search_pages" not in blob
    assert len(messages) == 2


def test_production_context_保留长上下文_tool_mcp_历史和模糊末句():
    request = _contextual_request()
    strategy = create_routing_input("production_context")
    messages = strategy.build_messages(request)
    system = messages[0]["content"]
    assert "生产研发助手" in system
    assert "项目是支付网关" in system
    assert "read_file [builtin]" in system
    assert "search_pages [mcp:notion]" in system
    assert "interactive-architecture-diagram" in system
    assert messages[-1] == {"role": "user", "content": "好，就按刚才说的那个做。"}
    assert any("历史 tool=repo_search 返回" in item["content"] for item in messages)
    assert strategy.fingerprint()["version"] == "production-context-v1"


def test_production_context_参数能做消融():
    request = _contextual_request()
    messages = create_routing_input(
        "production_context",
        include_role=False,
        include_long_context=False,
        include_messages=False,
        include_tools=False,
    ).build_messages(request)
    blob = "\n".join(item["content"] for item in messages)
    assert "生产研发助手" not in blob
    assert "项目是支付网关" not in blob
    assert "search_pages" not in blob
    assert len(messages) == 2


def test_litellm_runtime_由工厂参数切换策略():
    direct = LiteLLMRuntimeAdapter()
    production = LiteLLMRuntimeAdapter(
        routing_input={"strategy": "production_context", "options": {"include_tools": False}}
    )
    assert direct.routing_input.name == "direct"
    assert production.routing_input.name == "production_context"
    assert direct.fingerprint() != production.fingerprint()


def test_未知策略和direct多余参数运行前拒绝():
    with pytest.raises(ValueError, match="未知 routing input"):
        create_routing_input("not-real")
    with pytest.raises(ValueError, match="暂不接受参数"):
        create_routing_input("direct", include_tools=True)


def test_context_进入_case_json_因此自然进入_dataset_hash(tmp_path):
    case = RoutingCase(
        id="pdf-pos-01",
        prompt="照上面说的处理",
        expected_skills=["pdf"],
        context=RoutingContext(messages=[ContextMessage(role="user", content="拆分 PDF")]),
    )
    path = tmp_path / "cases.jsonl"
    path.write_text(case.model_dump_json() + "\n", encoding="utf-8")
    assert '"context"' in path.read_text(encoding="utf-8")
