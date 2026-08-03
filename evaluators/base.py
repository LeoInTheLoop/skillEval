"""Evaluator registry and shared context.

Evaluator 只依赖归一化 RunResult/JSON、case contract 和 suite snapshot；不依赖
OpenClaw 内部对象。新增 evaluator 的最小改动是一个文件和注册表装饰器。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class EvaluationContext:
    suite: dict[str, Any]
    snapshot: dict[str, Any]
    cases: dict[str, Any]
    runs: list[dict[str, Any]]
    scores: dict[str, Any] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)


class Evaluator(Protocol):
    name: str

    def evaluate(self, context: EvaluationContext) -> dict[str, Any]:
        ...


_REGISTRY: dict[str, type] = {}


def register(name: str):
    def decorator(cls):
        if name in _REGISTRY and _REGISTRY[name] is not cls:
            raise ValueError(f"evaluator 已注册：{name}")
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return decorator


def create_evaluator(name: str, **options: Any) -> Evaluator:
    try:
        cls = _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"未知 evaluator {name!r}；可用：{sorted(_REGISTRY)}") from exc
    return cls(**options)


def available() -> list[str]:
    return sorted(_REGISTRY)


def evaluate_all(names: list[str], context: EvaluationContext) -> dict[str, dict[str, Any]]:
    return {
        name: create_evaluator(name).evaluate(context)
        for name in names
    }
