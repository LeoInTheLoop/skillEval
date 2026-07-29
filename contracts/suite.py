"""N2：路由实验 Suite 的严格配置契约（AGENTS.md §8）。

YAML 只是存储格式；进入执行层前必须先变成这个契约。这样字段拼错、类型漂移、
重复模型和明文 secret 会在任何 runtime/model 调用之前失败。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .runtime import NetworkMode, SkillMode
from .skill import VERSION_DIR

_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
_SKILL_ID = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
# 环境变量名，可选 `=宿主机变量名` 改名后缀
_ENV_PASSTHROUGH = r"^[A-Za-z_][A-Za-z0-9_]*(=[A-Za-z_][A-Za-z0-9_]*)?$"

# 镜像必须按内容寻址固定，浮动 tag 会让同一个 config_hash 跑出不同结果。
# 两种都算固定：registry 拉下来的 `name@sha256:...`，以及本地 build 的裸 image ID
# （`docker image inspect --format '{{.Id}}'`）。只认前者的话，本地 build 的镜像
# 永远进不了 suite —— 而没 push 到 registry 之前，本地 ID 就是它唯一的固定名字。
PINNED_IMAGE = re.compile(r"(?:^|@)sha256:[0-9a-f]{64}$")
_GATE_CONDITION = r"^(>=|<=)\s+(0(?:\.\d+)?|1(?:\.0+)?)$"
_SENSITIVE_PARTS = ("api_key", "secret", "password", "access_token", "credential")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class SuiteModelSpec(_StrictModel):
    """一个模型轴条目。Secret 字段只保存环境变量名。"""

    id: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)
    api_base_env: str | None = Field(default=None, pattern=_ENV_NAME)
    api_key_env: str | None = Field(default=None, pattern=_ENV_NAME)
    params: dict[str, Any] = Field(default_factory=dict)


class SuiteSkillSpec(_StrictModel):
    dir: str = Field(min_length=1)
    # 这套实验**归谁**，与 include（模型看见谁）严格分开。No-Skill 基线也要
    # 写目标 skill，即使它被故意拿出 catalog；归档、Viewer、历史索引都只读
    # 这个显式字段，不需要从文件名猜。
    target: list[Annotated[str, Field(pattern=_SKILL_ID)]] = Field(min_length=1)
    # 按 skill_id 钉版本，如 `{meeting-and-brief: v2}`。没钉的取该 skill 目录里
    # 版本号最大的一版。V1/V2 对照就是两份 suite 只差这一处（AGENTS.md §7.3b）：
    # catalog 组成完全相同，变的只有目标 skill 取哪一版，delta 因此可归因。
    versions: dict[str, str] = Field(default_factory=dict)
    # Empty = every skill in dir; non-empty = a deliberate routing catalog.
    # This matters when evaluating one freshly installed skill in a personal
    # skills directory that contains unrelated skills.
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    mode: SkillMode = "routing_only"
    cfg: str = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_excludes(self) -> "SuiteSkillSpec":
        if len(self.target) != len(set(self.target)):
            raise ValueError("skills.target 不能包含重复 skill id")
        if len(self.include) != len(set(self.include)):
            raise ValueError("skills.include 不能包含重复 skill id")
        if len(self.exclude) != len(set(self.exclude)):
            raise ValueError("skills.exclude 不能包含重复 skill id")
        overlap = sorted(set(self.include) & set(self.exclude))
        if overlap:
            raise ValueError(f"skills.include 与 skills.exclude 不能重叠：{overlap}")
        bad = sorted(v for v in self.versions.values() if not VERSION_DIR.fullmatch(v))
        if bad:
            raise ValueError(
                f"skills.versions 的值必须是版本目录名（v1、v2、v1.1）：{bad}"
            )
        excluded = sorted(set(self.versions) & set(self.exclude))
        if excluded:
            raise ValueError(
                f"skills.versions 钉了版本又被 exclude 掉：{excluded} —— "
                "这条钉版本不会有任何效果，删掉一处"
            )
        return self


class SuiteEnvironmentSpec(_StrictModel):
    """Environment Backend 配置；runtime 只消费 prepared request。"""

    backend: str = Field(default="local", min_length=1)
    image: str | None = None
    network: NetworkMode = "full"
    cpus: float | None = Field(default=None, gt=0)
    memory: str | None = None
    # 只写**变量名**，值从 .env / 宿主环境取，和 models[].api_key_env 同一个约定。
    # 容器默认是干净的，agent 要的凭据必须显式点名才进去。
    # 写 `NAME` 或 `容器里的名字=宿主机的名字`。
    env_passthrough: list[Annotated[str, Field(pattern=_ENV_PASSTHROUGH)]] = Field(
        default_factory=list
    )
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _docker_image_is_reproducible(self) -> "SuiteEnvironmentSpec":
        if self.backend == "docker":
            if not self.image:
                raise ValueError("docker environment 必须声明 image")
            if not PINNED_IMAGE.search(self.image):
                raise ValueError(
                    "docker image 必须按内容寻址固定："
                    "registry 镜像写 name@sha256:<64位>，本地 build 的写 image ID "
                    "（docker image inspect --format '{{.Id}}'）"
                )
        return self


class SuiteRoutingInputSpec(_StrictModel):
    """routing-only 给模型看什么；由 routing input 工厂按 strategy 创建。"""

    strategy: str = Field(default="direct", min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)


class SuitePipelineSpec(_StrictModel):
    """Execution intent recorded with every archive.

    The current pipeline intentionally has no skill-writing stage.  Marking a
    suite ``evaluate_only`` makes that guard explicit: it can run a V1/V2/V3
    already selected by ``skills`` but can never create, edit, or promote one.
    ``iteration`` is a user-owned round label; inference repeats are configured
    separately by top-level ``repeats``.
    """

    mode: Literal["evaluate_only"] = "evaluate_only"
    iteration: int = Field(default=1, ge=1)


class SuiteJudgeSpec(_StrictModel):
    """判分模型（grade.py 用）。**与 models[] 的被测模型完全解耦。**

    为什么单开一节而不是复用 SuiteModelSpec：被测模型是「实验对象」，judge 是
    「量具」。两者换的理由不同、换的频率不同，凭据也常常不在一个 provider ——
    默认就指向独立的 `JUDGE_*` 环境变量，想跟被测模型共用端点必须显式写出来。

    `id` 会进评分产物文件名和 scores.json。换了 judge 却不换 id，就会出现
    「同一个名字下两批不可比的分」—— 这是本项目最贵的一类错误（§5①）。
    """

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    model: str = Field(min_length=1)
    # 这两个存的是**环境变量名**，不是值，填什么都行（和 models[] 同一机制）。
    # 默认指向独立的 JUDGE_* 只是「默认分开」；要跟被测模型共用端点，
    # 显式写 DASHSCOPE_BASE_URL 即可。反过来默认共用，就会有人不知不觉
    # 拿被测模型判自己的卷子。
    api_base_env: str = Field(default="JUDGE_BASE_URL", pattern=_ENV_NAME)
    api_key_env: str = Field(default="JUDGE_API_KEY", pattern=_ENV_NAME)
    params: dict[str, Any] = Field(default_factory=dict)
    # 启用哪些标准评估维度（见 dimensions.py）。空 = 只判 case 自己写的
    # expect_assertions，不跑维度。维度名拼错会在**运行前**被拒，不会静默少一行。
    dimensions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_dimensions(self) -> "SuiteJudgeSpec":
        from workflows.dimensions import STANDARD_DIMENSIONS

        unknown = [d for d in self.dimensions if d not in STANDARD_DIMENSIONS]
        if unknown:
            raise ValueError(
                f"未知的评估维度 {unknown}；可用：{sorted(STANDARD_DIMENSIONS)}"
            )
        if len(self.dimensions) != len(set(self.dimensions)):
            raise ValueError("scoring.judge.dimensions 不能重复")
        return self


class SuiteScoringSpec(_StrictModel):
    metrics: list[str] = Field(min_length=1)
    gate: dict[str, str] = Field(default_factory=dict)
    # 不写 = 不做语义判定，只跑确定性断言。默认关闭是有意的：
    # 每开一次就是一次真实外发 + 一笔钱 + 一个不完全可复现的数字。
    judge: SuiteJudgeSpec | None = None

    @model_validator(mode="after")
    def _valid_gate(self) -> "SuiteScoringSpec":
        invalid = {
            metric: condition
            for metric, condition in self.gate.items()
            if not re.fullmatch(_GATE_CONDITION, condition)
        }
        if invalid:
            raise ValueError(f"gate 条件必须是 '>= 0..1' 或 '<= 0..1'，非法项：{invalid}")
        if len(self.metrics) != len(set(self.metrics)):
            raise ValueError("scoring.metrics 不能重复")
        return self


def _find_inline_secret(value: Any, path: str = "suite") -> str | None:
    """返回第一个疑似明文 secret 的字段路径；`*_env` / `*_id` 是合法引用。"""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            sensitive = any(part in normalized for part in _SENSITIVE_PARTS)
            reference = normalized.endswith("_env") or normalized.endswith("_id")
            child_path = f"{path}.{key}"
            if sensitive and not reference:
                return child_path
            found = _find_inline_secret(child, child_path)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_inline_secret(child, f"{path}[{index}]")
            if found:
                return found
    return None


class RoutingSuite(_StrictModel):
    """当前 routing walking-skeleton 的完整声明式配置。"""

    suite_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    suite_version: str = Field(min_length=1)
    description: str = ""
    dataset: str = Field(min_length=1)

    runtime: str = Field(min_length=1)
    runtime_options: dict[str, Any] = Field(default_factory=dict)
    routing_input: SuiteRoutingInputSpec = Field(default_factory=SuiteRoutingInputSpec)
    pipeline: SuitePipelineSpec = Field(default_factory=SuitePipelineSpec)
    environment: SuiteEnvironmentSpec = Field(default_factory=SuiteEnvironmentSpec)
    skills: SuiteSkillSpec
    models: list[SuiteModelSpec] = Field(min_length=1)
    tools: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)

    repeats: int = Field(default=3, ge=1)
    # 并发单位是独立 conversation（model × case × repeat）；同一 conversation
    # 内的 turn 永远串行，保证 session/history/workspace 顺序。
    parallelism: int = Field(default=1, ge=1, le=64)
    timeout_seconds: int = Field(default=300, ge=1)
    scoring: SuiteScoringSpec

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "RoutingSuite":
        model_ids = [model.id for model in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("models.id 必须唯一")
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("tools 不能重复")
        if self.skills.mode == "routing_only" and self.tools:
            raise ValueError("routing_only 模式不能声明 tools")
        if self.runtime == "litellm":
            missing = [model.id for model in self.models if model.model is None]
            if missing:
                raise ValueError(f"litellm 的 model 条目必须声明 model：{missing}")

        secret_path = _find_inline_secret(self.model_dump(mode="python"))
        if secret_path:
            raise ValueError(
                f"{secret_path} 疑似明文 secret；suite 只能保存 *_env 环境变量名或 *_id"
            )
        return self

    def canonical_dict(self) -> dict[str, Any]:
        """用于执行、快照和 hash 的规范化字典；相同语义得到相同表示。"""
        return self.model_dump(mode="python")


def load_suite(path: str | Path) -> RoutingSuite:
    """读取并严格校验 YAML。空文件/非 mapping 也交给 Pydantic 结构化报错。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RoutingSuite.model_validate(data)


def format_suite_validation_error(path: str | Path, error: ValidationError) -> str:
    """Turn a strict Pydantic traceback into an actionable CLI message."""
    lines = [f"suite 配置无效：{Path(path)}"]
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "根对象"
        hint = ""
        if location == "suite_version" and issue["type"] == "string_type":
            hint = "；YAML 会把 0.1 读成数字，请写成 `suite_version: \"0.1\"`"
        lines.append(f"  - {location}: {issue['msg']}{hint}")
    return "\n".join(lines)
