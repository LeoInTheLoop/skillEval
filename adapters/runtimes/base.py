"""N5：Runtime Adapter 接口（AGENTS.md §17.1）。

上层（编排、评测）只认这个 Protocol，**不得引用任何 runtime 的内部对象**（§17.4）。
换 runtime 不需要改 evaluator。

Adapter 只负责「跑」，不负责「评分」（§17.3）。
"""
from __future__ import annotations

import subprocess
from contextlib import AbstractContextManager, contextmanager
from typing import Iterator, Protocol, runtime_checkable

from contracts import (
    ErrorKind,
    ErrorSubkind,
    InvocationRequest,
    RunResult,
    RuntimeCapabilities,
    RuntimeHealth,
)

# 这些模块抛出来的异常一律算「模型 API / 网络层」的锅，不是我们的。
# 按**来源模块**而不是类名，是因为 litellm 会把连不上上游包装成 `InternalServerError`
# —— 类名里一个网络词都没有。只认类名的话，一次断网会被记成「评测系统崩了」，
# 那是这套分类最想避免的误判。用字符串判断是为了不 import litellm（装不装都要能归类）。
_API_MODULES = ("litellm", "openai", "anthropic", "httpx", "httpcore", "requests", "urllib3", "aiohttp")

# 兜底：模块不认识时再看类名和错误信息里有没有网络词
_NETWORK_HINTS = ("connection", "network", "unreachable", "ratelimit", "rate_limit",
                  "authentication", "apierror", "apistatus", "apitimeout", "ssl", "dns",
                  "timed out", "refused")

_QUOTA_HINTS = ("quota", "insufficient balance", "insufficient credits", "billing hard limit",
                # 阿里云百炼欠费的措辞不带 "quota" 这个词，之前会被误判成普通网络问题
                # （HANDOFF ★ 更新 16 / docker-t1）。
                "account is in good standing", "overdue payment", "arrearage")
_AUTH_HINTS = ("authentication", "unauthorized", "invalid api key", "invalid_api_key", "forbidden")
_DNS_HINTS = ("dns", "name or service not known", "nodename nor servname", "getaddrinfo")


def classify_error(exc: BaseException) -> ErrorKind:
    """把异常归到四类之一（AGENTS.md ★★★ ⑥）。

    顺序有讲究，三处容易归错：
      * `subprocess.TimeoutExpired` 类名里带 timeout，但那是 CLI 卡死，不是网络 → runtime
      * `ConnectionError` 是 `OSError` 的子类，先判网络再判 OSError，否则全落成 runtime
      * litellm 的 `InternalServerError` 其实是连不上上游 —— 只能靠来源模块认出来
    """
    if isinstance(exc, subprocess.SubprocessError):
        return "runtime"
    module = (type(exc).__module__ or "").split(".")[0].lower()
    if module in _API_MODULES:
        return "network"
    blob = f"{type(exc).__name__} {exc}".lower()
    if isinstance(exc, ConnectionError) or any(k in blob for k in _NETWORK_HINTS):
        return "network"
    if isinstance(exc, (OSError, TimeoutError)):
        return "runtime"
    return "harness"


def _subkind_from_text(blob: str) -> ErrorSubkind:
    if any(hint in blob for hint in _QUOTA_HINTS):
        return "provider_quota_exhausted"
    if any(hint in blob for hint in _AUTH_HINTS):
        return "provider_authentication"
    if any(hint in blob for hint in _DNS_HINTS):
        return "network_dns"
    if "timeout" in blob or "timed out" in blob:
        return "network_timeout"
    if "rate" in blob and "limit" in blob:
        return "provider_rate_limited"
    return "network_connectivity"


def classify_error_subkind(exc: BaseException, kind: ErrorKind | None = None) -> ErrorSubkind | None:
    """Refine model/API failures for remediation while preserving ErrorKind.

    Provider quota exhaustion and transient rate limiting are both API-layer
    failures, but users need different next actions.  The raw provider error
    remains in RunResult.error; this is only the stable reporting label.
    """
    if (kind or classify_error(exc)) != "network":
        return None
    return _subkind_from_text(f"{type(exc).__name__} {exc}".lower())


def classify_error_text_subkind(text: str, kind: ErrorKind) -> ErrorSubkind | None:
    """Same hint matching as classify_error_subkind, for errors that never went
    through a raised Python exception (e.g. OpenClaw CLI stderr, §17.3)."""
    if kind != "network":
        return None
    return _subkind_from_text(text.lower())


@runtime_checkable
class RuntimeAdapter(Protocol):
    """所有 runtime 的统一接口。实际业务运行与 eval 共用它（§3.2）。"""

    name: str

    def run(self, request: InvocationRequest) -> RunResult:
        """跑一次。**不许抛异常** —— 失败也要返回 status != success 的 RunResult，
        否则一个 case 炸了会拖垮整批（§17.4「Runtime 异常转换为标准错误」）。"""
        ...

    def healthcheck(self, environment=None) -> RuntimeHealth:
        """能不能用。不健康时 detail 要能直接告诉人怎么修。

        传了 ``environment`` 就必须在**那个环境里**探。容器化跑的时候，宿主机装没装
        这个 runtime 完全无所谓 —— 探宿主机会同时给出两种假答案：宿主机没装报不健康
        （其实容器里有），宿主机装了报健康（其实容器里的凭据没配）。
        """
        ...

    def capabilities(self) -> RuntimeCapabilities:
        """支持哪些能力。编排层据此提前拒绝不合法的 suite。"""
        ...

    def fingerprint(self) -> dict:
        """adapter 内部**会改变结果**、但不在 suite 里的东西（如 system prompt、CLI 路径）。

        编排层把它并进 config_hash。没有这一步，改一句 prompt 就能悄悄改变结果
        而指纹不变 —— 那正是「配置即实验」要防的事。
        """
        ...

    def prepared(self, request: InvocationRequest) -> AbstractContextManager[None]:
        """N4 Environment Resolver（AGENTS.md §10）：进入时布置运行环境，**退出时必须还原**。

        skill 注入、文件挂载这类事都在这里做。用 context manager 而不是
        setup/teardown 一对方法，是因为它对异常和提前 return 天然安全 ——
        跑挂了也不会把环境留在半配置状态污染下一次 run（§11.5「运行结束后按策略清理」）。
        """
        ...


class BaseRuntimeAdapter:
    """可选基类：提供 run() 的异常兜底，子类实现 _run_impl 即可。

    不强制继承 —— 只要满足上面的 Protocol 就是合法 adapter。
    """

    name: str = "base"
    version: str | None = None

    def _run_impl(self, request: InvocationRequest) -> RunResult:
        raise NotImplementedError

    @contextmanager
    def prepared(self, request: InvocationRequest) -> Iterator[None]:
        """默认不需要布置环境（litellm/mock 只发一次请求，没有 workspace 概念）。"""
        yield

    def run(self, request: InvocationRequest) -> RunResult:
        import time

        t0 = time.perf_counter()
        try:
            # 环境的布置与还原都归 prepared() 管：跑挂了也不会留残局
            with self.prepared(request):
                result = self._run_impl(request)
        except Exception as e:  # noqa: BLE001 — 契约要求：异常转成标准错误，不外抛
            error_kind = classify_error(e)
            result = RunResult(
                case_id=request.case_id,
                repeat_index=request.repeat_index,
                model=str(request.model.get("id", "unknown")),
                status="failed",
                error=repr(e),
                error_kind=error_kind,
                error_subkind=classify_error_subkind(e, error_kind),
            )
        result.runtime_name = self.name
        result.runtime_version = self.version
        if not result.duration_ms:
            result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    def healthcheck(self, environment=None) -> RuntimeHealth:
        return RuntimeHealth(healthy=True, runtime=self.name, version=self.version)

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(runtime=self.name, skill_modes=["routing_only"])

    def fingerprint(self) -> dict:
        return {}

    @staticmethod
    def _sha(text: str) -> str:
        import hashlib
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
