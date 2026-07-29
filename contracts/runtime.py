"""N5/N7：Runtime 的输入输出契约（AGENTS.md §6.2、§6.3、§17.1）。

这是 Runtime 与 Evaluator 之间的唯一接口。**Evaluator 不得依赖任何 runtime 内部对象**
（§3.3）—— 换 runtime 不需要改 evaluator，靠的就是这两个模型。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import RoutingContext
from .skill import SkillMeta

SkillMode = Literal["none", "routing_only", "full"]
RunStatus = Literal[
    "success", "failed", "timeout", "denied", "budget_exceeded", "skipped"
]

# 失败的四类归属（AGENTS.md ★★★ ⑥「错误分类」）。混在一起统计，会把
# 「评测系统崩了」误读成「skill 不行」—— 那是最贵的一种误判。
#   task     模型跑完了但没做对（输出无法解析、模型自己拒绝执行）
#   runtime  runtime 进程层挂了（CLI 退出码非 0、找不到可执行文件、子进程超时）
#   network  网络/模型 API 层挂了（连不上、认证失败、限流）
#   harness  我们自己的评测系统挂了（suite 非法、契约校验失败、编排层 bug）
ErrorKind = Literal["task", "runtime", "network", "harness"]
# A coarse ErrorKind is used for score denominators; this optional subkind
# preserves the actionable provider diagnosis without turning quota exhaustion
# into a task failure.  Keep it deliberately small and stable for reports.
ErrorSubkind = Literal[
    "network_dns",
    "network_connectivity",
    "network_timeout",
    "provider_authentication",
    "provider_quota_exhausted",
    "provider_rate_limited",
]
NetworkMode = Literal["disabled", "mock", "allowlist", "full"]


class ResolvedEnvironment(BaseModel):
    """Environment Backend 布置后的只读执行信息。

    host 路径供 harness 收集 artifact；runtime 路径供容器内 agent 使用。两者不能
    混成一个，否则 Docker mount 后产生的绝对路径会污染 RunResult。
    """

    model_config = ConfigDict(extra="forbid")

    backend: str
    host_workspace: str | None = None
    runtime_workspace: str | None = None
    host_skill_dirs: list[str] = Field(default_factory=list)
    runtime_skill_dirs: list[str] = Field(default_factory=list)
    container_id: str | None = None
    # Runtime 把它当普通进程前缀使用，不需要知道背后是 docker exec、ssh 还是别的后端。
    command_prefix: list[str] = Field(default_factory=list)
    network_mode: NetworkMode = "full"
    fingerprint: dict[str, Any] = Field(default_factory=dict)


class InvocationRequest(BaseModel):
    """一次运行请求。实际业务运行与 eval 共用同一结构（§3.2）。"""
    model_config = ConfigDict(extra="forbid")

    request_id: str
    case_id: str
    repeat_index: int
    turn_index: int = Field(default=1, ge=1)

    prompt: str
    context: RoutingContext = Field(default_factory=RoutingContext)
    skills: list[SkillMeta] = Field(default_factory=list)
    skill_mode: SkillMode = "routing_only"
    # 本次要物化进 workspace 的只读输入文件（§11.4）。编排层填**宿主机绝对路径**，
    # Environment Backend 负责复制与只读化；Runtime 只看到 workspace 里的同名文件。
    input_files: list[str] = Field(default_factory=list)

    model: dict[str, Any] = Field(default_factory=dict)   # suite 里的一个 model 条目
    session_id: str | None = None                          # 多轮：同 case 内复用
    timeout_seconds: int = 300
    # 由 Environment Backend 运行时填写，dataset/suite 不直接构造。
    environment: ResolvedEnvironment | None = None


class ToolCall(BaseModel):
    """一次 tool 调用（§6.3 tool_calls）。

    各 runtime 暴露的粒度不一样：OpenClaw 只给聚合的 toolSummary（用了哪些 tool、
    共几次、失败几次），拿不到逐次的参数与返回。**给不出来的字段就留空，不要瞎编** ——
    `count` 为 None 表示"这个 runtime 只报告了用过该 tool，没报次数"。
    """
    model_config = ConfigDict(extra="forbid")

    name: str
    count: int | None = None
    failures: int | None = None
    arguments: dict[str, Any] | None = None    # 逐次粒度的 runtime 才填
    result_summary: str | None = None


TEXT_EXCERPT_LIMIT = 4000
"""单个产物进 `Artifact.text_excerpt` 的字符上限。

judge 的输入是要花钱的，一个 run 可能落十几个产物；截断到前 4000 字符足够判
"人名/日期/数字对不对"这类断言，又不会让 prompt 爆掉。判不了的部分由 judge 按
"证据不足"处理，规则写在 `grade.build_grading_prompt` 里。
"""


class Artifact(BaseModel):
    """Run 产出的文件（§11.4）。

    必须记 SHA-256、大小、MIME —— 否则没法判定"产物对不对"，也没法在回归里比对。
    路径统一存**相对 workspace 的路径**，绝对路径换台机器就失效了。

    `text_excerpt` 是文本类产物的**内容前缀**：workspace 跑完即删，不在这里留一份，
    judge 就只能看见文件名和大小，"报告里的数字是不是编的"这类断言只能判"证据不足"
    （§24）。只对文本类填、且截断到 `TEXT_EXCERPT_LIMIT`，二进制产物恒为 None。
    """
    model_config = ConfigDict(extra="forbid")

    path: str                                  # 相对 workspace
    sha256: str
    size_bytes: int
    mime_type: str | None = None
    change: Literal["created", "modified"] = "created"
    text_excerpt: str | None = None            # 文本类产物的内容前缀；截断时以 truncated 标记结尾


class RunResult(BaseModel):
    """归一化的运行结果。所有 runtime 都必须返回这个，不许各返回各的。

    字段名与骨架期的 RoutingRun 保持兼容（case_id / selected_skills / ok / ...），
    这样 score_routing.py 读旧 runs.jsonl 也不会断。
    """
    model_config = ConfigDict(extra="forbid")

    case_id: str
    repeat_index: int
    model: str
    request_id: str | None = None
    session_id: str | None = None
    turn_index: int = Field(default=1, ge=1)

    status: RunStatus = "success"
    selected_skills: list[str] = Field(default_factory=list)
    loaded_skills: list[str] = Field(default_factory=list)   # full 模式才非空
    reasoning: str | None = None
    final_answer: str | None = None
    raw_output: str | None = None
    error: str | None = None
    # error 非空时必须给出归属，否则四类失败会被混成一个数字（见 ErrorKind）
    error_kind: ErrorKind | None = None
    error_subkind: ErrorSubkind | None = None
    # 前一轮失败导致本轮没有执行。它不是 task/system failure，不进入轮次分母。
    skip_reason: str | None = None

    # --- full eval 才有内容；routing-only 恒为空（§18.1 明令不得调 tool）---
    tool_calls: list[ToolCall] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    # 本轮结束时 workspace 里仍存在的全部业务文件路径。artifact 只表示本轮变化，
    # 无法证明上一轮未修改的文件还在；多轮文件延续指标读取这里。
    workspace_files: list[str] = Field(default_factory=list)

    runtime_name: str = "unknown"
    runtime_version: str | None = None
    duration_ms: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    # runtime 内部实际用的模型（OpenClaw 自己决定，不在 suite 里）。
    # 不记下来，事后就分不清那批结果到底跑的什么模型。
    resolved_model: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_derived_ok(cls, data: Any) -> Any:
        """Accept persisted runs that include the derived ``ok`` compatibility flag."""
        if isinstance(data, dict) and "ok" in data:
            data = dict(data)
            data.pop("ok", None)
        return data

    @model_validator(mode="after")
    def _error_must_be_classified(self) -> "RunResult":
        """有 error 却没归类，就默认算「我们自己的系统挂了」。

        不留 None 的空档：未归类的失败会在统计里变成一个查不出来源的数字。
        默认取 harness 而不是 task，是因为宁可先怀疑评测系统，也不要把
        自己的 bug 记到被测 skill 头上。
        """
        if self.error and self.error_kind is None:
            self.error_kind = "harness"
        return self

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def model_dump_json(self, **kw) -> str:  # noqa: D102 — 落盘时把 ok 一起写出去
        import json
        data = self.model_dump(mode="json")
        data["ok"] = self.ok
        return json.dumps(data, ensure_ascii=False, **kw)


class RuntimeHealth(BaseModel):
    """healthcheck() 的返回。不健康时 detail 必须能直接告诉人怎么修。"""
    model_config = ConfigDict(extra="forbid")

    healthy: bool
    runtime: str
    version: str | None = None
    detail: str = ""


class RuntimeCapabilities(BaseModel):
    """runtime 支持哪些能力。编排层据此拒绝不合法的 suite，而不是跑一半才炸。"""
    model_config = ConfigDict(extra="forbid")

    runtime: str
    skill_modes: list[SkillMode] = Field(default_factory=list)
    tools: bool = False
    multi_turn: bool = False
    workspace: bool = False
    network_control: bool = False
