"""路由输入策略工厂：suite 参数切 direct / production_context。"""
from __future__ import annotations

import importlib
from typing import Callable, TypeVar

from .base import RoutingInputStrategy

_REGISTRY: dict[str, type] = {}
_AUTOLOAD = ("direct", "production_context")
T = TypeVar("T")


def register(name: str) -> Callable[[type[T]], type[T]]:
    def decorate(cls: type[T]) -> type[T]:
        if name in _REGISTRY:
            raise ValueError(f"routing input {name!r} already registered")
        cls.name = name  # type: ignore[attr-defined]
        _REGISTRY[name] = cls
        return cls
    return decorate


def _autoload() -> None:
    for module in _AUTOLOAD:
        importlib.import_module(f"{__name__}.{module}")


def available() -> list[str]:
    _autoload()
    return sorted(_REGISTRY)


def create_routing_input(name: str, **options) -> RoutingInputStrategy:
    _autoload()
    if name not in _REGISTRY:
        raise ValueError(f"未知 routing input {name!r}；可选：{sorted(_REGISTRY)}")
    return _REGISTRY[name](**options)


__all__ = [
    "RoutingInputStrategy",
    "available",
    "create_routing_input",
    "register",
]
