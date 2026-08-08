"""Full eval 的断言逻辑、错误分类、以及「单 case 挂了不中断整批」。"""
from __future__ import annotations

import json
import subprocess

import pandas as pd
import pytest

from adapters.runtimes.base import (
    BaseRuntimeAdapter,
    classify_error,
    classify_error_subkind,
    classify_error_text_subkind,
)
from contracts import RoutingCase, RunResult, RuntimeCapabilities
from workflows.matrix import build_matrix
from workflows.run_routing import run_one
from workflows.score_full import aggregate, artifact_hit, score_run


def _art(path, size=10, mime="text/csv"):
    return {"path": path, "sha256": "sha256:x", "size_bytes": size, "mime_type": mime}


# --- 产物断言：存在 + 非空 + MIME ---

def test_产物命中要求存在且非空且_mime_对得上():
    arts = [_art("out/q3.csv")]
    assert artifact_hit("out/q3.csv", arts)
    assert artifact_hit("out/*.csv", arts)
    assert not artifact_hit("out/other.csv", arts)


def test_空文件不算产出():
    assert not artifact_hit("out/q3.csv", [_art("out/q3.csv", size=0)])


def test_mime_不符不算命中():
    # 名字叫 .csv 但 runtime 报的是 markdown —— 这正是 MIME 维度要抓的
    assert not artifact_hit("out/q3.csv", [_art("out/q3.csv", mime="text/markdown")])


# --- 逐题打分 ---

_CASE = RoutingCase(
    id="deliverable-pack-pos-01", prompt="p", expected_skills=["deliverable-pack"],
    expect_artifacts=["out/q3.csv", "out/q3.md"], expect_tools=["write"],
)
_CATALOG = {"deliverable-pack"}


def _run(**kw):
    base = {"case_id": _CASE.id, "repeat_index": 0, "ok": True,
            "loaded_skills": ["deliverable-pack", "openclaw-bundled-thing"],
            "tool_calls": [{"name": "write"}],
            "artifacts": [_art("out/q3.csv"), _art("out/q3.md", mime="text/markdown")]}
    return {**base, **kw}


def test_全部断言通过算任务完成():
    r = score_run(_run(), _CASE, _CATALOG)
    assert r["done"] and r["skill_injected"]
    assert (r["art_hits"], r["art_total"]) == (2, 2)
    assert (r["tool_hits"], r["tool_total"]) == (1, 1)


def test_少一个产物就不算完成():
    r = score_run(_run(artifacts=[_art("out/q3.csv")]), _CASE, _CATALOG)
    assert not r["done"] and r["art_hits"] == 1


def test_没调到声明的_tool_就不算完成():
    r = score_run(_run(tool_calls=[{"name": "read"}]), _CASE, _CATALOG)
    assert not r["done"] and r["tool_hits"] == 0


def test_注入体检只跟我们自己的目录比():
    """loaded_skills 里混着 OpenClaw 自带的十几个，不剔掉就永远判不出注入生效没有。"""
    r = score_run(_run(loaded_skills=["deliverable-pack"] + [f"bundled-{i}" for i in range(18)]),
                  _CASE, _CATALOG)
    assert r["skill_injected"]
    assert not score_run(_run(loaded_skills=["bundled-0"]), _CASE, _CATALOG)["skill_injected"]


def test_拒答题禁止产出文件():
    """留空 expect_artifacts 只是 N/A；要判「不该落文件」得显式 forbid_artifacts。"""
    rej = RoutingCase(id="none-rej-01", prompt="p", expected_skills=[], forbid_artifacts=True)
    assert score_run(_run(artifacts=[], tool_calls=[]), rej, _CATALOG)["done"]
    assert not score_run(_run(), rej, _CATALOG)["done"]   # 留了文件 → 失败


def test_注入体检不进任务完成度():
    """它只说明目录可见，不代表模型用了它 —— 拒答题里也会显示已注入。"""
    rej = RoutingCase(id="none-rej-01", prompt="p", expected_skills=[], forbid_artifacts=True)
    r = score_run(_run(artifacts=[], tool_calls=[]), rej, _CATALOG)
    assert r["skill_injected"] and r["done"]


# --- 维度聚合：N/A 不能变成 0 ---

def test_没有题声明的维度记_NA_而不是零():
    rows = [score_run(_run(), RoutingCase(id="x-pos-01", prompt="p"), _CATALOG)]
    scores = aggregate(pd.DataFrame(rows))
    assert scores["artifact_hit"] is None      # 不是 0.0
    assert scores["tool_hit"] is None


def test_产物命中率按断言条数而不是按题数():
    rows = [score_run(_run(artifacts=[_art("out/q3.csv")]), _CASE, _CATALOG)]
    assert aggregate(pd.DataFrame(rows))["artifact_hit"] == pytest.approx(0.5)


# --- 错误分类（AGENTS.md ★★★ ⑥ 的四类）---

class _Boom(Exception):
    pass


class _APIConnectionError(Exception):
    pass


@pytest.mark.parametrize("exc,kind", [
    (_APIConnectionError("upstream down"), "network"),   # 网络：按类名认，不用 import litellm
    (ConnectionResetError("reset"), "network"),          # 是 OSError 子类，别落成 runtime
    (subprocess.TimeoutExpired("openclaw", 1), "runtime"),  # 类名带 timeout，但是 CLI 卡死
    (FileNotFoundError("openclaw"), "runtime"),
    (_Boom("我们自己的 bug"), "harness"),
])
def test_异常归类(exc, kind):
    assert classify_error(exc) == kind


def test_litellm_把断网包装成_InternalServerError_也要认成网络():
    """实测踩到的：类名里一个网络词都没有，只认类名会把断网记成 harness。"""
    exc = type("InternalServerError", (Exception,), {"__module__": "litellm.exceptions"})(
        "InternalServerError - OpenAIException - Connection error.")
    assert classify_error(exc) == "network"


@pytest.mark.parametrize("message,subkind", [
    ("provider free quota has been exhausted", "provider_quota_exhausted"),
    ("[Errno 8] nodename nor servname provided, or not known", "network_dns"),
    ("429 rate limit", "provider_rate_limited"),
    ("invalid API key", "provider_authentication"),
    # 阿里云百炼欠费的措辞（docker-t1 的真实错误文案），HANDOFF ★ 更新 16
    ("Access denied, please make sure your account is in good standing.",
     "provider_quota_exhausted"),
    ('{"type":"Arrearage"}', "provider_quota_exhausted"),
])
def test_模型网络错误保留可操作的子分类(message, subkind):
    exc = type("ProviderError", (Exception,), {"__module__": "litellm.exceptions"})(message)
    assert classify_error(exc) == "network"
    assert classify_error_subkind(exc) == subkind


def test_历史run没有存过exception也能从原始错误文本重分类子类():
    """OpenClaw CLI 失败从不抛异常，subkind 只能从落盘的 error 文本重建（不改 runs.jsonl）。"""
    text = ("openclaw exit=1: ...reason=auth next=none detail=400 Access denied, please "
            "make sure your account is in good standing. For details, see: "
            "https://www.alibabacloud.com/help/en/model-studio/error-code#overdue-payment")
    assert classify_error_text_subkind(text, "network") == "provider_quota_exhausted"
    assert classify_error_text_subkind(text, "runtime") is None


def test_评测系统自己的_bug_不许被误判成网络():
    """反向保护：模块归类不能宽到把我们自己的 bug 也吞进 network。"""
    assert classify_error(KeyError("case_id")) == "harness"
    assert classify_error(ValueError("gold 指向不存在的 skill")) == "harness"


def test_有_error_没归类时默认算评测系统的锅():
    r = RunResult(case_id="c", repeat_index=0, model="m", status="failed", error="boom")
    assert r.error_kind == "harness"


def test_模型不按格式回答算_task_失败而不是系统故障(monkeypatch):
    """调用成功、模型答错 —— 这是被测对象的锅，不能记成评测系统故障。"""
    import sys
    import types

    from contracts import InvocationRequest

    fake = types.ModuleType("litellm")
    fake.completion = lambda **kw: types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="我选 pdf 吧"))],
        usage=None)
    monkeypatch.setitem(sys.modules, "litellm", fake)

    from adapters.runtimes.litellm import LiteLLMRuntimeAdapter
    r = LiteLLMRuntimeAdapter().run(InvocationRequest(
        request_id="r", case_id="c", repeat_index=0, prompt="p",
        model={"id": "m", "model": "fake/model"}))
    assert r.status == "failed" and r.error_kind == "task"


def test_adapter_把异常转成带归类的_RunResult_而不外抛():
    class _Exploding(BaseRuntimeAdapter):
        name = "exploding"

        def _run_impl(self, request):
            raise _APIConnectionError("upstream down")

    from contracts import InvocationRequest
    r = _Exploding().run(InvocationRequest(request_id="r", case_id="c", repeat_index=0,
                                           prompt="p", model={"id": "m"}))
    assert r.status == "failed" and r.error_kind == "network"


def test_litellm失败时不把重复支持横幅混进进度输出(monkeypatch, capsys):
    """原始异常照样落盘；这里只抑制第三方逐请求的噪声。"""
    import sys
    import types

    from contracts import InvocationRequest

    def completion(**_kwargs):
        print("Give Feedback / Get Help", file=sys.stderr)
        raise type("ProviderError", (Exception,), {"__module__": "litellm.exceptions"})(
            "provider free quota has been exhausted"
        )

    fake = types.ModuleType("litellm")
    fake.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", fake)

    from adapters.runtimes.litellm import LiteLLMRuntimeAdapter
    result = LiteLLMRuntimeAdapter().run(InvocationRequest(
        request_id="r", case_id="c", repeat_index=0, prompt="p",
        model={"id": "m", "model": "fake/model"},
    ))

    assert result.error_kind == "network"
    assert result.error_subkind == "provider_quota_exhausted"
    assert "Give Feedback" not in capsys.readouterr().err


def test_session_id_里的加号被收敛掉():
    """OpenClaw 见到 `+` 直接 Invalid session ID —— 而 AUTHORING §1.3 允许它存在。

    真实咬到的是 execution_id 的时区偏移 `+0200`，每题必挂但 healthcheck 探得通。
    """
    from adapters.runtimes.openclaw import _safe_session_id
    assert "+" not in _safe_session_id("skilleval.20260728T131149840141+0200.s.m.c.r0")
    assert _safe_session_id("pdf+xlsx-multi-01") == "pdf-xlsx-multi-01"
    assert _safe_session_id("a.b_c-d1") == "a.b_c-d1"      # 合法字符原样保留


# --- 单 case 失败不中断整批 ---

class _HalfBrokenRuntime:
    """第 2 个任务直接抛异常 —— 违反 adapter 契约，编排层必须自己扛住。"""
    name = "half-broken"
    version = "test"

    def __init__(self):
        self.calls = 0

    def run(self, request):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("这个 case 炸了")
        return RunResult(case_id=request.case_id, repeat_index=request.repeat_index,
                         model=request.model["id"])

    def capabilities(self):
        return RuntimeCapabilities(runtime=self.name, skill_modes=["routing_only"])

    def fingerprint(self):
        return {}


def test_单个_case_抛异常不中断整批(tmp_path, monkeypatch):
    suite = {"suite_id": "s", "suite_version": "1.0", "dataset": "d.jsonl",
             "runtime": "half-broken",
             "skills": {"mode": "routing_only", "cfg": "v1"}, "models": [{"id": "m"}],
             "repeats": 1}
    cases = [RoutingCase(id=f"x-pos-0{i}", prompt="p") for i in range(1, 5)]
    tasks = build_matrix(suite_id="s", cases=cases, models=[{"id": "m"}],
                         repeats=1, execution_id="e1")
    out = tmp_path / "run"
    monkeypatch.setattr("workflows.run_routing.run_dir", lambda *_: out)

    runtime = _HalfBrokenRuntime()
    run_one(suite, {"id": "m"}, [], tasks, runtime, False, "sha256:d")

    persisted = [json.loads(l) for l in
                 (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(persisted) == 4, "炸了一个也必须把 4 条都跑完并落盘"
    assert sum(p["ok"] for p in persisted) == 3
    boom = next(p for p in persisted if not p["ok"])
    assert boom["error_kind"] == "harness"


def test_系统故障不进维度分的分母():
    """runtime/network/harness 的失败留在分母里，会把系统问题算到 skill 头上。"""
    rows = [score_run(_run(), _CASE, _CATALOG),
            score_run(_run(ok=False, error_kind="network", artifacts=[], tool_calls=[]),
                      _CASE, _CATALOG)]
    df = pd.DataFrame(rows)
    kept = df[~df.error_kind.isin(("runtime", "network", "harness"))]
    assert aggregate(df)["task_completion"] == pytest.approx(0.5)   # 混在一起：被拉低
    assert aggregate(kept)["task_completion"] == pytest.approx(1.0)  # 剔除后：真实值
