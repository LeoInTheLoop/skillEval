"""第二阶段：模拟生产 role / 长上下文 / 对话 / Tool / MCP 干扰。"""
from __future__ import annotations

import json

from contracts import InvocationRequest, build_catalog

from . import register

INSTRUCTION = (
    "现在只做 skill 路由判断。下面给你完整的生产上下文和一个 skill 目录（只有元数据）。"
    "要根据整段对话的真实未完成意图判断，不能只按最后一句是否出现 skill 触发词。"
    "判断应该激活哪个 skill；如果没有任何 skill 合适，就返回空列表。"
    '只返回 JSON，格式：{"selected_skills": ["<skill_id>", ...], "reasoning": "一句话"}。'
    "多个 skill 都确实需要时可以都选；宁缺毋滥，不确定就返回空。"
)


@register("production_context")
class ProductionContextRoutingInput:
    name = "production_context"

    def __init__(
        self,
        *,
        include_role: bool = True,
        include_long_context: bool = True,
        include_messages: bool = True,
        include_tools: bool = True,
    ):
        self.include_role = include_role
        self.include_long_context = include_long_context
        self.include_messages = include_messages
        self.include_tools = include_tools

    @staticmethod
    def _tool_catalog(request: InvocationRequest) -> str:
        rows: list[str] = []
        for tool in request.context.tools:
            origin = f"mcp:{tool.server or '?'}" if tool.source == "mcp" else "builtin"
            schema = json.dumps(tool.input_schema, ensure_ascii=False, sort_keys=True)
            rows.append(f"- {tool.name} [{origin}]: {tool.description}; input_schema={schema}")
        return "\n".join(rows) or "(无)"

    def build_messages(self, request: InvocationRequest) -> list[dict[str, str]]:
        sections: list[str] = []
        if self.include_role:
            sections.append(
                "[生产角色]\n" + (request.context.role_prompt or "(未声明)")
            )
        if self.include_long_context:
            sections.append(
                "[生产长上下文]\n" + (request.context.long_context or "(无)")
            )
        if self.include_tools:
            sections.append(
                "[生产可见 Tool / MCP 目录；仅供判断，不得调用]\n"
                + self._tool_catalog(request)
            )
        sections.extend([
            "[Skill 目录；仅 metadata]\n" + build_catalog(request.skills),
            "[路由任务]\n" + INSTRUCTION,
        ])
        messages: list[dict[str, str]] = [
            {"role": "system", "content": "\n\n".join(sections)}
        ]
        if self.include_messages:
            for item in request.context.messages:
                if item.role == "tool":
                    label = f"tool={item.name}" if item.name else "tool"
                    messages.append({
                        "role": "user",
                        "content": f"[历史 {label} 返回；不是新请求]\n{item.content}",
                    })
                else:
                    messages.append({"role": item.role, "content": item.content})
        messages.append({"role": "user", "content": request.prompt})
        return messages

    def fingerprint(self) -> dict:
        return {
            "strategy": self.name,
            "version": "production-context-v1",
            "instruction": self._sha(INSTRUCTION),
            "options": {
                "include_role": self.include_role,
                "include_long_context": self.include_long_context,
                "include_messages": self.include_messages,
                "include_tools": self.include_tools,
            },
        }

    @staticmethod
    def _sha(text: str) -> str:
        import hashlib
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
