"""Environment Backend 稳定接口。

Environment 负责隔离和物化；Runtime 仍只负责运行；Evaluator 只消费 RunResult。
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from contracts import InvocationRequest, RuntimeHealth


@runtime_checkable
class EnvironmentBackend(Protocol):
    name: str

    def prepared(
        self, request: InvocationRequest
    ) -> AbstractContextManager[InvocationRequest]:
        """布置环境并返回带 ResolvedEnvironment 的 request；退出必须清理。"""
        ...

    def healthcheck(self) -> RuntimeHealth:
        ...

    def capabilities(self) -> dict:
        ...

    def fingerprint(self) -> dict:
        ...
