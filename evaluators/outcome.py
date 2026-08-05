"""Outcome 层的统一展示视图；保留旧 scores 字段，不改变旧 gate 行为。"""
from __future__ import annotations

from statistics import fmean
from typing import Any

from .base import EvaluationContext, register

EVALUATOR_VERSION = "outcome-v1"


def _mean(values: list[float]) -> float | None:
    return round(fmean(values), 4) if values else None


@register("outcome", version=EVALUATOR_VERSION)
class OutcomeEvaluator:
    def evaluate(self, context: EvaluationContext) -> dict[str, Any]:
        scores = context.scores
        semantic = [
            scores.get(name) for name in (
                "correctness", "faithfulness", "completeness", "relevancy",
                "instruction_following", "conciseness",
            )
            if scores.get(name) is not None
        ]
        return {
            "metrics": {
                "task_completion": scores.get("task_completion"),
                # 当前 deterministic artifact_hit 是存在/非空/MIME 命中；保留
                # artifact_hit 原名，同时给三层报告一个稳定的业务别名。
                "artifact_correctness": scores.get("artifact_hit"),
                "final_answer_quality": _mean(semantic),
            },
            "source": {
                "task_completion": "score_full.aggregate",
                "artifact_correctness": "artifact_hit (existence/non-empty/MIME)",
                "final_answer_quality": "mean of available semantic judge dimensions",
            },
        }
