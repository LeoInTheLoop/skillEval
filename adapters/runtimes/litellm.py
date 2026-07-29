"""LiteLLM runtime：纯 metadata 推理，不执行 skill、不调 tool。

对应 AGENTS.md §18.1 Routing Eval。路由**刻意不接 OpenClaw**（§4.1）——
它只需要 skill 元数据 + 一次模型调用，走 agent 框架纯属浪费。
"""
from __future__ import annotations

import json
import os

from contracts import (
    InvocationRequest,
    RunResult,
    RuntimeCapabilities,
    RuntimeHealth,
)

from adapters.routing_inputs import create_routing_input
from workflows.litellm_support import quiet_completion

from . import register
from .base import BaseRuntimeAdapter


def _completion(litellm, **kwargs):
    """Call LiteLLM without repeating its support banner for every failed run."""
    return quiet_completion(litellm, **kwargs)


def parse_selection(text: str, valid: set[str]) -> tuple[list[str], str | None]:
    """容错解析：剥离 ``` 包裹，截出第一个 {...}，过滤掉不存在的 skill_id。"""
    t = text.strip().strip("`")
    if t.lower().startswith("json"):
        t = t[4:]
    lb, rb = t.find("{"), t.rfind("}")
    if lb != -1 and rb != -1:
        t = t[lb : rb + 1]
    data = json.loads(t)
    sel = [s for s in data.get("selected_skills", []) if s in valid]
    return sel, (data.get("reasoning") or None)


@register("litellm")
class LiteLLMRuntimeAdapter(BaseRuntimeAdapter):
    """一次 completion 调用完成路由判定。密钥按 suite 里的 *_env 变量名从环境取。"""

    def __init__(self, routing_input: dict | None = None):
        spec = routing_input or {"strategy": "direct", "options": {}}
        self.routing_input = create_routing_input(
            spec.get("strategy", "direct"), **(spec.get("options") or {})
        )

    def _run_impl(self, request: InvocationRequest) -> RunResult:
        import litellm  # 懒加载：--mock 跑不需要装

        m = request.model
        resp = _completion(
            litellm,
            model=m["model"],
            api_base=os.environ.get(m.get("api_base_env", "")) or None,
            api_key=os.environ.get(m.get("api_key_env", "")) or None,
            messages=self.routing_input.build_messages(request),
            response_format={"type": "json_object"},
            timeout=request.timeout_seconds,
            **m.get("params", {}),
        )
        text = resp.choices[0].message.content or ""
        try:
            sel, reason = parse_selection(text, {s.skill_id for s in request.skills})
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            # 调用本身成功了，是模型没按格式回 —— 这是 task 失败，不是系统故障。
            # 让它冒到 base 的兜底会被归成 harness，等于把模型的锅记到评测系统头上。
            return RunResult(
                case_id=request.case_id, repeat_index=request.repeat_index,
                model=str(m.get("id", m["model"])),
                status="failed", raw_output=text,
                error=f"模型输出无法解析为路由 JSON：{e!r}", error_kind="task",
            )

        usage = {}
        if getattr(resp, "usage", None):
            usage = {"input_tokens": resp.usage.prompt_tokens,
                     "output_tokens": resp.usage.completion_tokens}
        return RunResult(
            case_id=request.case_id, repeat_index=request.repeat_index,
            model=str(m.get("id", m["model"])),
            selected_skills=sel, reasoning=reason, raw_output=text, usage=usage,
        )

    def healthcheck(self, environment=None) -> RuntimeHealth:
        # environment 用不上：litellm 在**编排进程里**发 HTTP 请求，不进容器。
        try:
            import litellm  # noqa: F401
        except ImportError:
            return RuntimeHealth(healthy=False, runtime=self.name,
                                 detail="litellm 未安装：.venv/bin/pip install litellm")
        return RuntimeHealth(healthy=True, runtime=self.name)

    def capabilities(self) -> RuntimeCapabilities:
        # 纯 metadata 推理：给不了 tool、没有 workspace、不管网络
        return RuntimeCapabilities(runtime=self.name, skill_modes=["none", "routing_only"])

    def fingerprint(self) -> dict:
        return {"routing_input": self.routing_input.fingerprint()}
