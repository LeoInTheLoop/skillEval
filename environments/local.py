"""本机 Environment Backend：隔离 workspace，但不提供进程/网络隔离。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from contracts import InvocationRequest, ResolvedEnvironment, RuntimeHealth

from . import register
from .filesystem import materialized_files


@register("local")
class LocalEnvironmentBackend:
    name = "local"

    @contextmanager
    def prepared(self, request: InvocationRequest) -> Iterator[InvocationRequest]:
        with materialized_files(request) as (workspace, skills):
            resolved = ResolvedEnvironment(
                backend=self.name,
                host_workspace=str(workspace),
                runtime_workspace=str(workspace),
                host_skill_dirs=[str(skills)] if request.skills else [],
                runtime_skill_dirs=[str(skills)] if request.skills else [],
                network_mode="full",
                fingerprint=self.fingerprint(),
            )
            yield request.model_copy(update={"environment": resolved})

    def healthcheck(self) -> RuntimeHealth:
        return RuntimeHealth(healthy=True, runtime="environment:local")

    def capabilities(self) -> dict:
        return {
            "workspace_isolation": True,
            "container_isolation": False,
            "network_modes": ["full"],
        }

    def fingerprint(self) -> dict:
        return {"backend": self.name, "schema": "environment-v1"}
