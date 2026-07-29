"""Mock runtime：不调 API，产生带近邻混淆的假数据。

只用来验证链路（改了 scoring/命名之后跑一遍看有没有断），**结果不可用于任何判断**——
所以 mock 会进 config_hash，跟真实 run 的指纹必然不同。
"""
from __future__ import annotations

import random

from contracts import InvocationRequest, RunResult, RuntimeCapabilities

from . import register
from .base import BaseRuntimeAdapter

# 刻意让它错在「相邻」skill 上，这样混淆矩阵有东西可看
_CONFUSE = {
    "pdf": "docx", "docx": "pdf",
    "xlsx": "pptx", "pptx": "xlsx",
    "mcp-builder": "artifacts-builder", "artifacts-builder": "mcp-builder",
    # 仓库自带示例 catalog 的那对真近邻（都围着「一份表格」转，一个体检一个交付）
    "csv-profiler": "deliverable-pack", "deliverable-pack": "csv-profiler",
}


@register("mock")
class MockRuntimeAdapter(BaseRuntimeAdapter):
    """~85% 命中 / ~80% 拒答，按 (case_id, repeat) 定种子，可复现。"""

    def __init__(self, expected: dict[str, list[str]] | None = None):
        # 编排层把 case → expected_skills 传进来；mock 靠它造"大致正确"的假数据
        self.expected = expected or {}

    def _run_impl(self, request: InvocationRequest) -> RunResult:
        valid = [s.skill_id for s in request.skills]
        exp = self.expected.get(request.case_id, [])
        rng = random.Random(f"{request.case_id}-{request.repeat_index}")

        if not exp:                                   # No-Skill：80% 正确拒答
            # mode=none 时 catalog 为空，不存在可误激活的 skill，只能拒答。
            sel = [] if not valid or rng.random() < 0.80 else [rng.choice(valid)]
        else:
            gold = exp[0]
            r = rng.random()
            if r < 0.85:
                sel = [gold]                          # 命中（multi 题只给第一个 → 故意评错）
            elif r < 0.95 and gold in _CONFUSE:
                sel = [_CONFUSE[gold]]                # 混淆到近邻
            else:
                sel = []                              # 漏选

        return RunResult(
            case_id=request.case_id, repeat_index=request.repeat_index,
            model="mock", selected_skills=sel, reasoning="mock",
        )

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(runtime=self.name,
                                   skill_modes=["none", "routing_only", "full"],
                                   multi_turn=True, workspace=True)
