"""Efficiency 层的稳定展示视图。"""
from __future__ import annotations

from typing import Any

from workflows import metrics

from .base import EvaluationContext, register


@register("efficiency")
class EfficiencyEvaluator:
    def evaluate(self, context: EvaluationContext) -> dict[str, Any]:
        return {"metrics": metrics.efficiency(context.runs)}
