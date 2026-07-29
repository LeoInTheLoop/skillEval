"""suite.tools must be an enforced OpenClaw policy, not scoring-only metadata."""
from __future__ import annotations

import json
import subprocess
import threading
from contextlib import contextmanager

import pytest

from adapters.runtimes.openclaw import OpenClawRuntimeAdapter
from contracts import InvocationRequest


def _runtime(monkeypatch) -> OpenClawRuntimeAdapter:
    monkeypatch.setattr(
        "adapters.runtimes.openclaw.shutil.which", lambda *_args, **_kwargs: None
    )
    return OpenClawRuntimeAdapter(bin="openclaw-not-installed-for-unit-test")


def _fake_config(runtime, monkeypatch, initial=None):
    state = dict(initial or {})
    events = []

    def fake_config(_request, *args):
        action, key = args[:2]
        if action == "get":
            if key not in state:
                return subprocess.CompletedProcess(
                    args, 1, stdout=f"Config path not found: {key}", stderr=""
                )
            return subprocess.CompletedProcess(args, 0, stdout=state[key], stderr="")
        if action == "set":
            state[key] = args[2]
            events.append(("set", key, args[2]))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if action == "unset":
            state.pop(key, None)
            events.append(("unset", key, None))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(runtime, "_config", fake_config)
    return state, events


def _request(tools, request_id="r1"):
    return InvocationRequest(
        request_id=request_id,
        case_id="c1",
        repeat_index=0,
        prompt="p",
        skill_mode="full",
        allowed_tools=tools,
    )


def test_nonempty_suite_tools_becomes_exact_allowlist_and_restores(monkeypatch):
    runtime = _runtime(monkeypatch)
    state, events = _fake_config(
        runtime, monkeypatch, {"tools.allow": '["existing-tool"]'}
    )

    with runtime._tool_policy(_request(["read", "write"])):
        assert json.loads(state["tools.allow"]) == ["read", "write"]

    assert json.loads(state["tools.allow"]) == ["existing-tool"]
    assert events == [
        ("set", "tools.allow", '["read", "write"]'),
        ("set", "tools.allow", '["existing-tool"]'),
    ]


def test_empty_suite_tools_denies_every_tool_and_restores_on_exception(monkeypatch):
    runtime = _runtime(monkeypatch)
    state, events = _fake_config(
        runtime, monkeypatch, {"tools.deny": '["exec"]'}
    )

    with pytest.raises(RuntimeError, match="boom"):
        with runtime._tool_policy(_request([])):
            assert json.loads(state["tools.deny"]) == ["*"]
            raise RuntimeError("boom")

    assert json.loads(state["tools.deny"]) == ["exec"]
    assert events == [
        ("set", "tools.deny", '["*"]'),
        ("set", "tools.deny", '["exec"]'),
    ]


def test_policy_write_failure_is_fail_closed(monkeypatch):
    runtime = _runtime(monkeypatch)

    def failing_config(_request, *args):
        if args[0] == "get":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="permission denied")

    monkeypatch.setattr(runtime, "_config", failing_config)

    with pytest.raises(RuntimeError, match="拒绝在未落实权限"):
        with runtime._tool_policy(_request(["read"])):
            pytest.fail("policy failure must stop before the agent runs")


def test_local_profile_policy_is_held_for_the_whole_request(monkeypatch):
    runtime = _runtime(monkeypatch)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    @contextmanager
    def fake_policy(request):
        if request.request_id == "first":
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        yield

    monkeypatch.setattr(runtime, "_tool_policy", fake_policy)

    def prepare(request):
        with runtime.prepared(request):
            pass

    first_request = _request(["read"], "first")
    second_request = _request(["write"], "second")
    first_request.skill_mode = "routing_only"
    second_request.skill_mode = "routing_only"
    first = threading.Thread(target=prepare, args=(first_request,))
    second = threading.Thread(target=prepare, args=(second_request,))
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()

    # The second request cannot even apply its policy until the first request
    # has completed and restored the shared local profile.
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()


def test_healthcheck_validates_config_without_calling_agent(monkeypatch):
    calls = []

    monkeypatch.setattr(
        "adapters.runtimes.openclaw.shutil.which",
        lambda *_args, **_kwargs: "/usr/local/bin/openclaw",
    )

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[-1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, stdout="OpenClaw test\n", stderr="")
        if cmd[-2:] == ["config", "validate"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Config valid\n", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    monkeypatch.setattr("adapters.runtimes.openclaw.subprocess.run", fake_run)
    runtime = OpenClawRuntimeAdapter(bin="openclaw", profile="skilleval")

    health = runtime.healthcheck()

    assert health.healthy
    assert "未发送模型请求" in (health.detail or "")
    assert not any("agent" in command for command in calls)
