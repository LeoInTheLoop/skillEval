"""路由模型输入策略接口。

它只决定“给模型看什么”，不负责调用模型、不执行 tool、不评分。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from contracts import InvocationRequest


@runtime_checkable
class RoutingInputStrategy(Protocol):
    name: str

    def build_messages(self, request: InvocationRequest) -> list[dict[str, str]]:
        ...

    def fingerprint(self) -> dict:
        ...
