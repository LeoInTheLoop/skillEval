"""Environment Backend 注册表。"""
from __future__ import annotations

import importlib
from typing import Callable, TypeVar

from .base import EnvironmentBackend

_REGISTRY: dict[str, type] = {}
_AUTOLOAD = ("local", "docker")
T = TypeVar("T")


def register(name: str) -> Callable[[type[T]], type[T]]:
    def decorate(cls: type[T]) -> type[T]:
        if name in _REGISTRY:
            raise ValueError(f"environment {name!r} already registered")
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


def create_environment(name: str, **kwargs) -> EnvironmentBackend:
    _autoload()
    if name not in _REGISTRY:
        raise ValueError(f"未知 environment {name!r}；可选：{sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


__all__ = ["EnvironmentBackend", "available", "create_environment", "register"]
