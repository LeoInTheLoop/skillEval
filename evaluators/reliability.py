"""Reliability 层：重复运行的通过率、方差和 flaky 题诊断。"""
from __future__ import annotations

from typing import Any

from workflows import metrics

from .base import EvaluationContext, register


@register("reliability")
class ReliabilityEvaluator:
    def evaluate(self, context: EvaluationContext) -> dict[str, Any]:
        rows = context.rows
        if not rows:
            return {"metrics": {"repeat_pass_rate": None, "variance": None, "flaky_rate": None}}
        by_repeat: dict[Any, list[float]] = {}
        for row in rows:
            if row.get("skipped"):
                continue
            by_repeat.setdefault(row.get("repeat"), []).append(float(bool(row.get("done"))))
        repeat_rates = [sum(values) / len(values) for values in by_repeat.values() if values]
        flaky = metrics.flaky_cases(rows, key="done") if rows and "done" in rows[0] else []
        case_count = len({str(row.get("case_id")) for row in rows})
        variance = metrics.stats(repeat_rates)
        return {
            "metrics": {
                "repeat_pass_rate": round(sum(repeat_rates) / len(repeat_rates), 4)
                if repeat_rates else None,
                # variance is intentionally the stddev field; lower is better.
                "variance": variance["stddev"] if variance else None,
                "flaky_rate": round(len(flaky) / case_count, 4) if case_count else None,
            },
            "repeat_rates": repeat_rates,
            "flaky_cases": flaky,
        }
