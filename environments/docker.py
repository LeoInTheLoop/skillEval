"""Docker Environment Backend（Docker SDK，非 shell 拼命令）。

本层只负责容器、mount、资源和网络边界；镜像里跑哪个 agent 由 Runtime Adapter 决定。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Callable, Iterator

from contracts import (
    PINNED_IMAGE,
    InvocationRequest,
    NetworkMode,
    ResolvedEnvironment,
    RuntimeHealth,
)

from . import register
from .filesystem import materialized_files

OPENCLAW_BUILD_COMMAND = (
    "docker build -f environments/openclaw.Dockerfile -t skilleval-openclaw ."
)
OPENCLAW_EXPORT_COMMAND = (
    "export SKILLEVAL_OPENCLAW_IMAGE=\"$(docker image inspect "
    "skilleval-openclaw --format '{{.Id}}')\""
)


def _docker_failure_detail(exc: Exception, *, phase: str, image: str) -> str:
    """Classify host Docker failures without ever calling them skill failures."""
    blob = f"{type(exc).__name__}: {exc}"
    lowered = blob.lower()
    storage_hints = (
        "input/output error",
        "read-only file system",
        "containerdmeta.db",
        "metadata.db",
        "no space left on device",
    )
    if any(hint in lowered for hint in storage_hints):
        return (
            f"Docker daemon storage is unhealthy during {phase}: {blob}. "
            "This is an environment/harness failure, not a skill result. Restart Docker Desktop "
            "or the Docker daemon, verify its disk/storage, then rerun `docker info` before "
            "rebuilding the image."
        )
    if phase == "daemon":
        return (
            f"Docker daemon is unavailable: {blob}. Start/restart Docker and confirm `docker info` "
            "works; no skill execution was attempted."
        )
    return (
        f"Pinned Docker image is unavailable: {image} ({blob}). This is an environment setup "
        f"failure, not a skill result. Build and pin the shipped image:\n  {OPENCLAW_BUILD_COMMAND}"
        f"\n  {OPENCLAW_EXPORT_COMMAND}\nThen rerun `pipeline plan --healthcheck`."
    )


@register("docker")
class DockerEnvironmentBackend:
    name = "docker"

    def __init__(
        self,
        *,
        image: str,
        # 默认给网络，和 suite 契约里 environment.network 的默认值一致。
        # 容器里的 agent 要调模型 API 才能干活，默认断网会让第一次容器化运行
        # 全题失败，且失败长得像上游抖动 —— 那是最难查的一种默认值。
        # 要隔离就在 suite 里显式写 network: disabled，让它进 config_hash。
        network: NetworkMode = "full",
        cpus: float | None = None,
        memory: str | None = None,
        env_passthrough: list[str] | None = None,
        client_factory: Callable | None = None,
        **options,
    ):
        if not PINNED_IMAGE.search(image):
            raise ValueError(
                "Docker image 必须按内容寻址固定：name@sha256:<64位> 或本地 image ID"
            )
        if network not in {"disabled", "full"}:
            raise ValueError("Docker v0 先支持 disabled/full；mock/allowlist 不能静默降级")
        self.image = image
        self.network = network
        self.cpus = cpus
        self.memory = memory
        # 只存**变量名**；值到 prepared() 才从宿主环境读，绝不进 fingerprint 或快照
        self.env_passthrough = list(env_passthrough or [])
        self.options = options
        self._client_factory = client_factory

    @staticmethod
    def _split(spec: str) -> tuple[str, str]:
        """`NAME` 或 `容器里的名字=宿主机的名字`。

        改名是真实需要，不是备用灵活性：OpenClaw 的 onboard 只认 QWEN_API_KEY，
        而 .env 里的真相源叫 DASHSCOPE_API_KEY。没有改名就只能往用户的 .env 里塞
        一个纯为 docker 存在的别名变量 —— 那是把容器的事泄漏到全局配置里。
        """
        target, _, source = spec.partition("=")
        return target, (source or target)

    def _passthrough_env(self) -> dict[str, str]:
        resolved = {}
        for spec in self.env_passthrough:
            target, source = self._split(spec)
            if os.environ.get(source):
                resolved[target] = os.environ[source]
        return resolved

    def _client(self):
        if self._client_factory:
            return self._client_factory()
        try:
            import docker
        except ImportError as exc:
            raise RuntimeError(
                "缺 Docker SDK：.venv/bin/pip install 'docker>=7'"
            ) from exc
        return docker.from_env()

    @contextmanager
    def prepared(self, request: InvocationRequest) -> Iterator[InvocationRequest]:
        with materialized_files(request) as (workspace, skills):
            client = self._client()
            container = None
            try:
                run_options = {
                    "image": self.image,
                    # 清掉镜像自己的 ENTRYPOINT；Environment Backend 需要一个稳定的
                    # 守护进程，Runtime 再通过 command_prefix 进入容器执行 agent。
                    "entrypoint": "",
                    "command": ["sh", "-lc", "trap : TERM INT; sleep infinity & wait"],
                    "detach": True,
                    "network_mode": "none" if self.network == "disabled" else "bridge",
                    "volumes": {
                        str(workspace): {"bind": "/workspace", "mode": "rw"},
                        str(skills): {"bind": "/skills", "mode": "ro"},
                    },
                    "labels": {
                        "skill-eval.managed": "true",
                        "skill-eval.request-id": request.request_id,
                    },
                }
                if self.cpus is not None:
                    run_options["nano_cpus"] = int(self.cpus * 1_000_000_000)
                if self.memory is not None:
                    run_options["mem_limit"] = self.memory
                # 容器创建时注入，`docker exec` 会继承 —— 这样 key 不用出现在
                # 任何一条 exec 命令行上（不进宿主机的 ps / shell history）。
                passthrough = self._passthrough_env()
                if passthrough:
                    run_options["environment"] = passthrough
                reserved = set(run_options) & set(self.options)
                if reserved:
                    raise ValueError(f"docker options 不得覆盖隔离保留项：{sorted(reserved)}")
                run_options.update(self.options)
                container = client.containers.run(**run_options)
                resolved = ResolvedEnvironment(
                    backend=self.name,
                    host_workspace=str(workspace),
                    runtime_workspace="/workspace",
                    host_skill_dirs=[str(skills)] if request.skills else [],
                    runtime_skill_dirs=["/skills"] if request.skills else [],
                    container_id=container.id,
                    command_prefix=["docker", "exec", container.id],
                    network_mode=self.network,
                    fingerprint=self.fingerprint(),
                )
                yield request.model_copy(update={"environment": resolved})
            finally:
                if container is not None:
                    try:
                        container.remove(force=True)
                    finally:
                        try:
                            client.close()
                        except Exception:
                            pass
                else:
                    try:
                        client.close()
                    except Exception:
                        pass

    def healthcheck(self) -> RuntimeHealth:
        # 缺变量要在跑之前说，别等 agent 在容器里 401 了才发现 —— 那会被记成
        # network 错误，看上去像上游抖动，其实是 key 压根没进容器。
        missing = [src for _, src in map(self._split, self.env_passthrough)
                   if not os.environ.get(src)]
        if missing:
            return RuntimeHealth(
                healthy=False, runtime="environment:docker",
                detail=f"env_passthrough 点名的变量在宿主环境里没有值：{missing}；"
                       f"把它们写进 .env",
            )
        client = None
        try:
            client = self._client()
            info = client.ping()
        except Exception as exc:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            return RuntimeHealth(
                healthy=False,
                runtime="environment:docker",
                detail=_docker_failure_detail(exc, phase="daemon", image=self.image),
            )
        try:
            client.images.get(self.image)
        except Exception as exc:
            return RuntimeHealth(
                healthy=False,
                runtime="environment:docker",
                detail=_docker_failure_detail(exc, phase="image", image=self.image),
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        return RuntimeHealth(
            healthy=bool(info), runtime="environment:docker",
            detail=f"image={self.image}",
        )

    def capabilities(self) -> dict:
        return {
            "workspace_isolation": True,
            "container_isolation": True,
            "readonly_skill_mount": True,
            "network_modes": ["disabled", "full"],
            "resource_limits": ["cpu", "memory"],
        }

    def fingerprint(self) -> dict:
        return {
            "backend": self.name,
            "schema": "environment-v1",
            "image": self.image,
            "network": self.network,
            "cpus": self.cpus,
            "memory": self.memory,
            # 只记变量**名**：换 key 不该改变 config_hash（同一个实验），
            # 但改变「往容器里传哪些变量」会改变 agent 能做什么，必须入指纹。
            "env_passthrough": sorted(self.env_passthrough),
        }
