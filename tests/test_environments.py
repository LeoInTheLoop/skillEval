"""Environment Backend 的隔离、mount 和清理契约。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from contracts import InvocationRequest, load_suite
from environments import available, create_environment
from environments.docker import DockerEnvironmentBackend

_DOCKER_SUITE = Path(__file__).resolve().parents[1] / "evals/suites/full_deliverable_v1_docker.yaml"


def _pinned_image() -> str | None:
    """suite 里那张固定镜像；测试不硬编码 ID，重新 build 后不用改测试。"""
    try:
        return load_suite(_DOCKER_SUITE).canonical_dict()["environment"]["image"]
    except Exception:
        return None


def _docker_ready() -> bool:
    image = _pinned_image()
    if not image:
        return False
    try:
        import docker

        client = docker.from_env()
        client.ping()
        client.images.get(image)
        client.close()
        return True
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_ready(),
    reason="需要 Docker daemon 和 suite 里那张固定镜像："
           "docker build -f environments/openclaw.Dockerfile -t skilleval-openclaw .",
)


def _request() -> InvocationRequest:
    return InvocationRequest(
        request_id="request-1",
        case_id="none-rej-01",
        repeat_index=0,
        prompt="hello",
        skill_mode="none",
        model={"id": "mock"},
    )


def test_environment_注册表():
    assert available() == ["docker", "local"]
    assert create_environment("local").name == "local"


def test_local_每个_request独立_workspace且退出清理():
    backend = create_environment("local")
    seen: list[str] = []
    for _ in range(2):
        with backend.prepared(_request()) as prepared:
            workspace = prepared.environment.host_workspace
            assert workspace
            Path(workspace, "state.txt").write_text("x", encoding="utf-8")
            seen.append(workspace)
        assert not Path(workspace).exists()
    assert seen[0] != seen[1]


class _FakeContainer:
    id = "container-123"

    def __init__(self):
        self.removed = False

    def remove(self, force=False):
        assert force is True
        self.removed = True


class _FakeContainers:
    def __init__(self, container):
        self.container = container
        self.kwargs = None

    def run(self, **kwargs):
        self.kwargs = kwargs
        return self.container


class _FakeClient:
    def __init__(self):
        self.container = _FakeContainer()
        self.containers = _FakeContainers(self.container)
        self.closed = False

    def close(self):
        self.closed = True


def test_docker_mount_网络资源_prefix和清理():
    client = _FakeClient()
    backend = DockerEnvironmentBackend(
        image="example/openclaw@sha256:" + "a" * 64,
        network="disabled",
        cpus=1.5,
        memory="512m",
        client_factory=lambda: client,
    )
    with backend.prepared(_request()) as prepared:
        env = prepared.environment
        assert env.container_id == "container-123"
        assert env.command_prefix == ["docker", "exec", "container-123"]
        assert env.runtime_workspace == "/workspace"
        assert client.containers.kwargs["network_mode"] == "none"
        assert client.containers.kwargs["nano_cpus"] == 1_500_000_000
        assert client.containers.kwargs["mem_limit"] == "512m"
        volumes = client.containers.kwargs["volumes"]
        assert any(spec == {"bind": "/skills", "mode": "ro"} for spec in volumes.values())
    assert client.container.removed and client.closed


def test_docker拒绝浮动tag和未实现网络模式():
    with pytest.raises(ValueError, match="内容寻址"):
        DockerEnvironmentBackend(image="example/openclaw:latest")
    with pytest.raises(ValueError, match="内容寻址"):
        DockerEnvironmentBackend(image="sha256:" + "a" * 63)  # 少一位
    with pytest.raises(ValueError, match="mock/allowlist"):
        DockerEnvironmentBackend(
            image="example/openclaw@sha256:" + "a" * 64,
            network="allowlist",
        )


def test_docker默认给网络_断网必须是显式选择():
    """默认断网会让容器里的 agent 连不上模型 API，且失败长得像上游抖动。

    要隔离就在 suite 里显式写 network: disabled —— 那样它才会进 config_hash，
    「这批结果是断网跑的」才留得下痕迹。
    """
    client = _FakeClient()
    backend = DockerEnvironmentBackend(
        image="sha256:" + "c" * 64, client_factory=lambda: client,
    )
    assert backend.network == "full"
    assert backend.fingerprint()["network"] == "full"
    with backend.prepared(_request()):
        assert client.containers.kwargs["network_mode"] == "bridge"


def test_docker接受本地build的裸image_id():
    """本地 build 的镜像没有 registry digest，image ID 就是它唯一的固定名字。"""
    backend = DockerEnvironmentBackend(image="sha256:" + "b" * 64)
    assert backend.fingerprint()["image"] == "sha256:" + "b" * 64


def test_docker按变量名注入凭据且不进指纹(monkeypatch):
    monkeypatch.setenv("SKILLEVAL_TEST_KEY", "super-secret")
    monkeypatch.delenv("SKILLEVAL_TEST_ABSENT", raising=False)
    client = _FakeClient()
    backend = DockerEnvironmentBackend(
        image="example/openclaw@sha256:" + "a" * 64,
        env_passthrough=["SKILLEVAL_TEST_KEY", "SKILLEVAL_TEST_ABSENT"],
        client_factory=lambda: client,
    )
    with backend.prepared(_request()):
        # 值只出现在容器创建参数里，不经过任何 exec 命令行
        assert client.containers.kwargs["environment"] == {
            "SKILLEVAL_TEST_KEY": "super-secret"
        }
    # 指纹只记变量名：换 key 不该改变 config_hash，改变「传哪些变量」才该
    fp = backend.fingerprint()
    assert fp["env_passthrough"] == ["SKILLEVAL_TEST_ABSENT", "SKILLEVAL_TEST_KEY"]
    assert "super-secret" not in repr(fp)


def test_docker缺凭据在跑之前就报不健康(monkeypatch):
    monkeypatch.delenv("SKILLEVAL_TEST_ABSENT", raising=False)
    backend = DockerEnvironmentBackend(
        image="example/openclaw@sha256:" + "a" * 64,
        env_passthrough=["SKILLEVAL_TEST_ABSENT"],
        client_factory=lambda: _FakeClient(),
    )
    health = backend.healthcheck()
    assert not health.healthy
    assert "SKILLEVAL_TEST_ABSENT" in health.detail


@requires_docker
def test_docker真容器里的skill只读挂载与workspace回传(monkeypatch):
    """真起一个容器，验证 full 模式赖以成立的三件事。

    fake client 只能证明「参数拼对了」，证明不了 Docker 真按这个语义执行。
    artifact 全靠 workspace 双向可见，只读 mount 全靠 Docker 真的拒写 ——
    这两条错了，产物命中率会静默变成假数字。
    """
    from contracts import SkillMeta

    monkeypatch.setenv("SKILLEVAL_CONTAINER_KEY", "injected-value")
    backend = create_environment(
        "docker", image=_pinned_image(), network="disabled",
        env_passthrough=["IN_CONTAINER=SKILLEVAL_CONTAINER_KEY"],
    )
    skill_dir = Path(__file__).resolve().parents[1] / "subjects/deliverable-pack/v1"
    request = InvocationRequest(
        request_id="itest-1", case_id="itest", repeat_index=0, prompt="x",
        skill_mode="full", model={"id": "itest"},
        skills=[SkillMeta(skill_id="deliverable-pack", name="deliverable-pack",
                          description="itest", source_path=str(skill_dir / "SKILL.md"),
                          content_hash="sha256:0")],
    )

    def sh(container_id: str, script: str) -> str:
        return subprocess.run(["docker", "exec", container_id, "sh", "-lc", script],
                              capture_output=True, text=True).stdout.strip()

    with backend.prepared(request) as prepared:
        env = prepared.environment
        cid = env.container_id
        assert "SKILL.md" in sh(cid, "ls /skills/deliverable-pack/")
        # 只读 mount 必须真的拒写，否则 eval 会污染 skill 源
        assert sh(cid, "touch /skills/deliverable-pack/X 2>/dev/null || echo refused") == "refused"
        # 容器写 → 宿主机看得到，这是 artifact diff 的前提
        sh(cid, "mkdir -p /workspace/out && echo made > /workspace/out/r.txt")
        assert (Path(env.host_workspace) / "out/r.txt").read_text().strip() == "made"
        # 凭据按改名注入，宿主机的原名不进容器
        assert sh(cid, 'echo "$IN_CONTAINER"') == "injected-value"
        assert sh(cid, 'echo "${SKILLEVAL_CONTAINER_KEY:-unset}"') == "unset"
        host_workspace = env.host_workspace

    assert not subprocess.run(["docker", "ps", "-aq", "--filter", f"id={cid}"],
                              capture_output=True, text=True).stdout.strip()
    assert not Path(host_workspace).exists()
