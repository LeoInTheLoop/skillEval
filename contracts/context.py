"""生产上下文契约：让 routing eval 不再是假想的单句分类题。

这些字段只描述模型在生产路由时**能看到什么**，不授权执行 tool，也不改变
Environment Backend。真正可执行的 tool 仍由 suite/runtime capabilities 控制。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ContextRole = Literal["system", "user", "assistant", "tool"]
ToolSource = Literal["builtin", "mcp"]


class ContextMessage(BaseModel):
    """最终用户话之前的历史消息。

    `tool` 消息在不同 provider 的原生格式差异很大，routing adapter 会把它稳定地
    渲染成历史记录文本，避免为了模拟上下文反而触发真实 tool calling。
    """

    model_config = ConfigDict(extra="forbid")

    role: ContextRole
    content: str
    name: str | None = None


class ContextTool(BaseModel):
    """生产环境里模型可见的一个 tool/MCP tool 定义（只读描述，不执行）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    source: ToolSource = "builtin"
    server: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class RoutingContext(BaseModel):
    """一次路由判断所处的真实生产上下文。

    `prompt` 不放在这里：它仍是 EvalCase.prompt，确保最后一条用户消息不会被
    历史长文本吞掉，也方便报告直接展示被判定的那句话。
    """

    model_config = ConfigDict(extra="forbid")

    role_prompt: str = ""
    long_context: str = ""
    messages: list[ContextMessage] = Field(default_factory=list)
    tools: list[ContextTool] = Field(default_factory=list)
