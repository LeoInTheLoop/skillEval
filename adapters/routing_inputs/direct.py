"""第一阶段：干净、快速的 metadata 路由判断。"""
from __future__ import annotations

from contracts import InvocationRequest, build_catalog

from . import register

INSTRUCTION = (
    "你是一个 skill 路由器。给你一个 skill 目录（只有元数据）和当前用户问题，"
    "判断应该激活哪个 skill；没有合适的就返回空列表。"
    '只返回 JSON：{"selected_skills": ["<skill_id>", ...], "reasoning": "一句话"}。'
    "多个 skill 都确实需要时可以都选；宁缺毋滥。"
)


@register("direct")
class DirectRoutingInput:
    name = "direct"

    def __init__(self, **options):
        if options:
            raise ValueError(f"direct routing input 暂不接受参数：{sorted(options)}")

    def build_messages(self, request: InvocationRequest) -> list[dict[str, str]]:
        user = (
            f"[Skill 目录；仅 metadata]\n{build_catalog(request.skills)}\n\n"
            f"[当前用户问题]\n{request.prompt}"
        )
        return [
            {"role": "system", "content": INSTRUCTION},
            {"role": "user", "content": user},
        ]

    def fingerprint(self) -> dict:
        return {
            "strategy": self.name,
            "version": "direct-v1",
            "instruction": self._sha(INSTRUCTION),
        }

    @staticmethod
    def _sha(text: str) -> str:
        import hashlib
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
