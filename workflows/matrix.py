"""N3：把 suite 展开成确定性的运行矩阵（AGENTS.md §9）。

这一层只做纯数据展开，不创建 runtime、也不调用模型。这样任务数量、顺序、ID 和
session 隔离都能在真正花 token 之前被验证。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import product
from typing import Any

from contracts import FullEvalTurn, RoutingCase


def _slug(value: str) -> str:
    """把外部 ID 收敛成可安全用于 request/session 的短文本。"""
    return re.sub(r"[^A-Za-z0-9._+-]", "-", value).strip("-") or "unknown"


@dataclass(frozen=True, slots=True)
class MatrixTask:
    """一次可独立执行/重跑的最小任务。"""

    suite_id: str
    execution_id: str
    model: dict[str, Any]
    case: RoutingCase
    repeat_index: int
    request_id: str
    session_id: str
    turn_index: int = 1

    @property
    def model_id(self) -> str:
        return str(self.model["id"])

    @property
    def turn(self) -> FullEvalTurn:
        return self.case.resolved_turn(self.turn_index)

    @property
    def conversation_key(self) -> tuple[str, str, int]:
        """可并发的最小隔离单元；同 key 的 turns 必须串行。"""
        return self.model_id, self.case.id, self.repeat_index


def build_matrix(
    *,
    suite_id: str,
    cases: list[RoutingCase],
    models: list[dict[str, Any]],
    repeats: int,
    execution_id: str,
) -> list[MatrixTask]:
    """展开 ``model × case × repeat``，顺序稳定且不产生任何副作用。

    ``request_id`` 只由实验配置维度决定，便于定位并单独重跑；
    ``session_id`` 额外包含本次 execution_id，避免重新执行同一任务时复用旧会话。
    后续接多轮时，同一 case/repeat 的各 turn 复用这个 session_id 即可。
    """
    if not suite_id.strip():
        raise ValueError("suite_id 不能为空")
    if not execution_id.strip():
        raise ValueError("execution_id 不能为空")
    if repeats < 1:
        raise ValueError(f"repeats 必须 >= 1，实际为 {repeats}")
    if not cases:
        raise ValueError("dataset 没有可运行的 case")
    if not models:
        raise ValueError("suite 没有可运行的 model")

    case_ids = [case.id for case in cases]
    model_ids = [str(model.get("id", "")) for model in models]
    if any(not model_id for model_id in model_ids):
        raise ValueError("每个 model 都必须声明非空 id")
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("dataset 的 case id 必须唯一")
    if len(model_ids) != len(set(model_ids)):
        raise ValueError("suite 的 model id 必须唯一")

    suite_slug = _slug(suite_id)
    execution_slug = _slug(execution_id)
    tasks: list[MatrixTask] = []
    for model, case, repeat_index in product(models, cases, range(repeats)):
        model_slug = _slug(str(model["id"]))
        case_slug = _slug(case.id)
        session_id = (
            f"skilleval.{execution_slug}.{suite_slug}.{model_slug}.{case_slug}.r{repeat_index}"
        )
        for turn_index in range(1, case.turn_count + 1):
            task_key = (
                f"{suite_slug}.{model_slug}.{case_slug}.t{turn_index}.r{repeat_index}"
            )
            tasks.append(
                MatrixTask(
                    suite_id=suite_id,
                    execution_id=execution_id,
                    model=model,
                    case=case,
                    repeat_index=repeat_index,
                    request_id=task_key,
                    session_id=session_id,
                    turn_index=turn_index,
                )
            )
    return tasks
