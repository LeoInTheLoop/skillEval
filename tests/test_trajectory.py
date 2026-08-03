from __future__ import annotations

import json

from adapters.runtimes.openclaw import OpenClawRuntimeAdapter
from evaluators import available
from evaluators.trajectory import score_structured
from contracts import TrajectoryEvent


def _transcript(*entries: dict) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)


def test_trajectory_event_requires_tool_name_for_tool_events():
    import pytest

    with pytest.raises(ValueError, match="tool_call/tool_result"):
        TrajectoryEvent(step_index=1, event_type="tool_call", name="search")


def test_structured_trajectory_uses_coarse_tool_evidence_but_does_not_invent_order():
    run = {
        "tool_calls": [{"name": "write"}],
        "trajectory": [{
            "step_index": 1, "event_type": "tool_call", "name": "write",
            "tool_name": "write", "evidence_level": "coarse",
        }],
        "artifacts": [{"path": "out/result.md", "size_bytes": 10}],
    }
    expectation = {
        "required_tools": ["write"],
        "required_order": ["read", "write"],
        "required_state_change": True,
    }
    scores = score_structured(run, expectation)
    assert scores["tool_selection"] == 1.0
    assert scores["state_persistence"] == 1.0
    assert scores["order_correctness"] is None
    assert scores["argument_correctness"] is None


def test_openclaw_session_transcript_yields_exact_arguments_and_order():
    """会话 JSONL 是 exact 证据的来源；参数要脱敏限长，配对要靠 call_id。"""
    transcript = _transcript(
        {"type": "session", "id": "s1"},
        {"type": "message", "message": {"role": "user", "content": "go"}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "call-1", "name": "read",
             "arguments": {"path": "docs/a.md"}},
        ]}},
        {"type": "message", "message": {
            "role": "toolResult", "toolCallId": "call-1", "toolName": "read",
            "isError": False, "timestamp": 1785360598609}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "call-2", "name": "write",
             "arguments": {"path": "out/a.md", "content": "x" * 900,
                           "api_key": "sk-real-secret"}},
        ]}},
        {"type": "message", "message": {
            "role": "toolResult", "toolCallId": "call-2", "toolName": "write",
            "isError": True, "timestamp": 1785360604948}},
    )
    events = OpenClawRuntimeAdapter._transcript_tool_events(transcript)

    assert [(e.event_type, e.tool_name, e.call_id) for e in events] == [
        ("tool_call", "read", "call-1"), ("tool_result", "read", "call-1"),
        ("tool_call", "write", "call-2"), ("tool_result", "write", "call-2"),
    ]
    assert [e.step_index for e in events] == [1, 2, 3, 4]
    assert all(e.evidence_level == "exact" for e in events)
    assert events[0].arguments == {"path": "docs/a.md"}
    assert events[2].arguments["api_key"] == "<redacted>"
    assert "sk-real-secret" not in json.dumps(
        [e.model_dump(mode="json") for e in events], ensure_ascii=False)
    assert len(events[2].arguments["content"]) < 900
    assert events[1].status == "success" and events[3].status == "failed"


def test_exact_events_turn_order_correctness_from_na_into_a_score():
    run = {
        "tool_calls": [{"name": "read"}, {"name": "write"}],
        "trajectory": [e.model_dump(mode="json") for e in
                       OpenClawRuntimeAdapter._transcript_tool_events(_transcript(
                           {"type": "message", "message": {"role": "assistant", "content": [
                               {"type": "toolCall", "id": "c1", "name": "read",
                                "arguments": {"path": "docs/a.md"}}]}},
                           {"type": "message", "message": {"role": "assistant", "content": [
                               {"type": "toolCall", "id": "c2", "name": "write",
                                "arguments": {"path": "out/a.md"}}]}},
                       ))],
    }
    scores = score_structured(run, {"required_order": ["read", "write"]})
    assert scores["order_correctness"] == 1.0
    # 顺序反了要扣分，而不是照样满分
    assert score_structured(run, {"required_order": ["write", "read"]})[
        "order_correctness"] == 0.5


def _write_then_probe(*, probe_back: bool) -> dict:
    entries = [
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "c1", "name": "write",
             # 正文里提到了另一个产物的路径：这不能被算成"读回过它"
             "arguments": {"path": "/ws/out/a.csv",
                           "content": "见 out/b.md"}}]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "c1",
                                        "toolName": "write", "isError": False}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "c2", "name": "write",
             "arguments": {"path": "/ws/out/b.md", "content": "…"}}]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "c2",
                                        "toolName": "write", "isError": False}},
    ]
    if probe_back:
        entries += [
            {"type": "message", "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "c3", "name": "read",
                 "arguments": {"path": "/ws/out/a.csv"}}]}},
            {"type": "message", "message": {"role": "toolResult", "toolCallId": "c3",
                                            "toolName": "read", "isError": False}},
        ]
    return {
        "artifacts": [{"path": "out/a.csv"}, {"path": "out/b.md"}],
        "trajectory": [e.model_dump(mode="json") for e in
                       OpenClawRuntimeAdapter._transcript_tool_events(_transcript(*entries))],
    }


def test_verification_needs_the_case_to_declare_its_probe_tools():
    """不声明 probe 就保持 N/A —— evaluator 不猜哪个 tool 算验证。"""
    run = _write_then_probe(probe_back=True)
    assert score_structured(run, {"required_verification": True})["verification_rate"] is None
    assert score_structured(run, {"required_verification": True,
                                  "verification_tools": ["read"]})["verification_rate"] == 0.5


def test_verification_counts_readback_not_a_path_mentioned_inside_content():
    """写完就收工 = 0；写完读回 = 命中。正文里出现路径字符串不算读回。"""
    assert score_structured(_write_then_probe(probe_back=False), {
        "required_verification": True, "verification_tools": ["read"]})["verification_rate"] == 0.0
    verified = score_structured(_write_then_probe(probe_back=True), {
        "required_verification": True, "verification_tools": ["read"]})["verification_rate"]
    assert verified == 0.5  # a.csv 读回了，b.md 没有 —— 只被 a.csv 的正文提过一次


def test_coarse_trajectory_keeps_verification_na_even_with_probe_tools():
    run = {"artifacts": [{"path": "out/a.csv"}],
           "trajectory": [{"step_index": 1, "event_type": "tool_call", "name": "write",
                           "tool_name": "write", "evidence_level": "coarse"}]}
    assert score_structured(run, {"required_verification": True,
                                  "verification_tools": ["read"]})["verification_rate"] is None


def test_unparsable_transcript_keeps_na_instead_of_faking_events():
    assert OpenClawRuntimeAdapter._transcript_tool_events("not json\n{}\n") == []


def test_evaluator_factory_has_four_report_layers():
    assert {"outcome", "trajectory", "reliability", "efficiency"} <= set(available())
