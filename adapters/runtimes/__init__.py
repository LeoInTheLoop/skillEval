"""Runtime 工厂（注册表模式）。

新增一个 runtime 只需要两步，**不用改这个文件**：
    1. 写 adapters/runtimes/<name>.py，类上加 @register("<name>")
    2. 在下面 _AUTOLOAD 里加模块名（或让它被 import 到）

上层永远只调 `create_runtime(name, ...)`，拿到的是 RuntimeAdapter Protocol，
不知道也不该知道背后是 LiteLLM 还是 OpenClaw（AGENTS.md §17.4）。
"""
from __future__ import annotations

import importlib
from typing import Callable, TypeVar

from .base import BaseRuntimeAdapter, RuntimeAdapter

_REGISTRY: dict[str, type] = {}
_AUTOLOAD = ("mock", "litellm", "openclaw")   # 导入即注册

T = TypeVar("T")


def register(name: str) -> Callable[[type[T]], type[T]]:
    """把 adapter 类登记到工厂。重名直接报错，避免静默覆盖。"""
    def deco(cls: type[T]) -> type[T]:
        if name in _REGISTRY:
            raise ValueError(f"runtime {name!r} already registered by {_REGISTRY[name]}")
        cls.name = name  # type: ignore[attr-defined]
        _REGISTRY[name] = cls
        return cls
    return deco


def _autoload() -> None:
    for mod in _AUTOLOAD:
        importlib.import_module(f"{__name__}.{mod}")


def available() -> list[str]:
    _autoload()
    return sorted(_REGISTRY)


def create_runtime(name: str, **kwargs) -> RuntimeAdapter:
    """工厂入口。suite 里的 `runtime: xxx` 直接喂进来。"""
    _autoload()
    if name not in _REGISTRY:
        raise ValueError(f"未知 runtime {name!r}；可选：{sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


__all__ = ["BaseRuntimeAdapter", "RuntimeAdapter", "available", "create_runtime", "register"]
