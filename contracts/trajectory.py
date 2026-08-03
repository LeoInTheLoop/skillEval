"""通用 Agent 执行轨迹契约。

Trajectory 不绑定 read/write/file。任何 runtime 都可以把一次可观察的动作、
工具结果、环境状态变化或最终回复归一到这里。runtime 拿不到的粒度必须明确
标记为 ``coarse`` / ``missing``，评测层不得根据聚合摘要编造顺序和参数。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TrajectoryEventType = Literal[
    "observation", "tool_call", "tool_result", "state_change", "verification",
    "final", "error", "unknown",
]
EvidenceLevel = Literal["exact", "coarse", "derived", "missing"]
EventStatus = Literal["started", "success", "failed", "skipped", "unknown"]


class TrajectoryEvent(BaseModel):
    """一个可观察事件；不保存隐藏 CoT。"""

    model_config = ConfigDict(extra="forbid")

    step_index: int = Field(ge=1)
    event_type: TrajectoryEventType
    name: str = Field(min_length=1)
    tool_name: str | None = None
    # runtime 给的调用 id：tool_call ↔ tool_result 靠它配对，不靠数组位置或时间戳
    call_id: str | None = None
    arguments: dict[str, Any] | None = None
    result_summary: str | None = None
    status: EventStatus = "unknown"
    evidence_level: EvidenceLevel = "exact"
    evidence_refs: list[str] = Field(default_factory=list)
    state_before: dict[str, Any] | None = None
    state_after: dict[str, Any] | None = None
    timestamp_ms: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _tool_event_has_tool_name(self) -> "TrajectoryEvent":
        if self.event_type in {"tool_call", "tool_result"} and not self.tool_name:
            raise ValueError("tool_call/tool_result 事件必须有 tool_name")
        return self


class TrajectoryExpectation(BaseModel):
    """题目对轨迹的通用期望；不假设具体 skill 或文件工具。"""

    model_config = ConfigDict(extra="forbid")

    goal: str = ""
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    required_order: list[str] = Field(default_factory=list)
    required_state_change: bool = False
    required_verification: bool = False
    # 哪些 tool 算「观察/验证」由题目声明。evaluator 不猜 tool 语义：
    # 不填就保持 N/A，填了才用「改过的东西事后被它成功读过」判 verification。
    verification_tools: list[str] = Field(default_factory=list)
    assertions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_tools(self) -> "TrajectoryExpectation":
        if len(self.required_tools) != len(set(self.required_tools)):
            raise ValueError("trajectory.required_tools 不能重复")
        if len(self.verification_tools) != len(set(self.verification_tools)):
            raise ValueError("trajectory.verification_tools 不能重复")
        if self.required_verification and not self.verification_tools:
            raise ValueError(
                "声明 required_verification 就必须给 verification_tools；"
                "否则 verification_rate 只能永远记 N/A")
        if len(self.forbidden_tools) != len(set(self.forbidden_tools)):
            raise ValueError("trajectory.forbidden_tools 不能重复")
        overlap = sorted(set(self.required_tools) & set(self.forbidden_tools))
        if overlap:
            raise ValueError(f"trajectory.required_tools 与 forbidden_tools 重叠：{overlap}")
        return self


class TrajectoryDimensionScore(BaseModel):
    """一条轨迹维度分；score=None 表示证据粒度不足，必须记 N/A。"""

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(min_length=1)
    score: float | None = Field(default=None, ge=0, le=1)
    evidence: str = Field(min_length=1)
    source: Literal["deterministic", "judge"] = "judge"


TRAJECTORY_DIMENSIONS = (
    "tool_selection",
    "argument_correctness",
    "order_correctness",
    "state_persistence",
    "verification_rate",
)
