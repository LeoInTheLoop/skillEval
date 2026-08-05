from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from adapters.runtimes.openclaw import OpenClawRuntimeAdapter
from evaluators import available
from evaluators.base import EvaluationContext
from evaluators.trajectory import (
    TrajectoryEvaluator,
    merge_trajectory_metrics,
    score_argument_assertions,
    score_structured,
)
from contracts import (
    ARGUMENT_ASSERTION_SCHEMA_VERSION,
    ARGUMENT_CORRECTNESS_RUBRIC_VERSION,
    RoutingCase,
    SuiteTrajectorySpec,
    TrajectoryArgumentExpectation,
    TrajectoryEvent,
)


def _transcript(*entries: dict) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in entries)


def test_trajectory_event_requires_tool_name_for_tool_events():
    with pytest.raises(ValueError, match="tool_call/tool_result"):
        TrajectoryEvent(step_index=1, event_type="tool_call", name="search")


def test_hybrid_metrics_keep_deterministic_scores_and_use_judge_only_for_na():
    deterministic = {"argument_correctness": 0.0, "order_correctness": None}
    judge = {"argument_correctness": 1.0, "order_correctness": 0.75}
    assert merge_trajectory_metrics(deterministic, judge, "hybrid") == {
        "argument_correctness": 0.0,
        "order_correctness": 0.75,
    }
    assert merge_trajectory_metrics(deterministic, judge, "judge") == judge
    assert merge_trajectory_metrics(deterministic, judge, "deterministic") == deterministic


@pytest.mark.parametrize("matcher", [
    {"required": True},
    {"forbidden": True},
    {"equals": "out/a.md"},
    {"in": ["csv", "json"]},
    {"matches": r"^out/.+\.md$"},
])
def test_argument_expectation_supports_one_strict_matcher(matcher):
    assertion = TrajectoryArgumentExpectation.model_validate({
        "tool": "write", "path": "options.format", **matcher,
    })
    dumped = assertion.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert set(dumped) == {"tool", "path", next(iter(matcher))}


def test_argument_expectation_rejects_ambiguous_or_unsafe_gold_without_echoing_secret():
    with pytest.raises(ValidationError, match="只能声明一个 matcher"):
        TrajectoryArgumentExpectation(
            tool="write", path="path", required=True, equals="out/a.md",
        )

    secret = "sk-must-not-appear-in-the-error"
    with pytest.raises(ValidationError) as caught:
        TrajectoryArgumentExpectation(
            tool="request", path="api_key", equals=secret,
        )
    assert "secret 参数 gold" in str(caught.value)
    assert secret not in str(caught.value)

    with pytest.raises(ValidationError, match="完整用户内容"):
        TrajectoryArgumentExpectation(
            tool="write", path="content", equals="用户原文",
        )
    with pytest.raises(ValidationError, match="gold 值过长"):
        TrajectoryArgumentExpectation(
            tool="write", path="title", equals="x" * 201,
        )

    with pytest.raises(ValidationError, match="合法正则"):
        TrajectoryArgumentExpectation(tool="write", path="path", matches="[")


def _argument_run(*calls: tuple[str, str, dict | None]) -> dict:
    return {
        "trajectory": [
            {
                "step_index": index,
                "event_type": "tool_call",
                "name": tool,
                "tool_name": tool,
                "call_id": call_id,
                "arguments": arguments,
                "status": "started",
                "evidence_level": "exact",
            }
            for index, (call_id, tool, arguments) in enumerate(calls, 1)
        ]
    }


def test_argument_correctness_scores_path_matchers_across_multiple_calls_with_refs():
    run = _argument_run(
        ("c1", "write", {"path": "out/a.csv", "options": {"format": "csv"}}),
        ("c2", "write", {"path": "out/a.md", "options": {"format": "markdown"}}),
    )
    assertions = [
        {"tool": "write", "path": "path", "equals": "out/a.md"},
        {"tool": "write", "path": "options.format", "in": ["csv", "json"]},
        {"tool": "write", "path": "path", "matches": r"^out/.+\.csv$"},
        {"tool": "write", "path": "options", "required": True},
        {"tool": "write", "path": "unsafe", "forbidden": True},
    ]

    score, details = score_argument_assertions(run, assertions)

    assert score == 1.0
    assert all(item["status"] == "passed" for item in details)
    assert details[0]["evidence_refs"] == [
        "call_id=c1", "step_index=1", "call_id=c2", "step_index=2",
    ]
    assert details[1]["assertion"]["in"] == ["csv", "json"]


def test_argument_correctness_distinguishes_wrong_missing_tool_and_missing_evidence():
    wrong = _argument_run(("c1", "write", {"path": "out/wrong.md"}))
    gold = [{"tool": "write", "path": "path", "equals": "out/right.md"}]
    assert score_argument_assertions(wrong, gold)[0] == 0.0

    missing_tool = _argument_run(("c1", "read", {"path": "out/right.md"}))
    assert score_argument_assertions(missing_tool, gold)[0] == 0.0

    coarse = {
        "trajectory": [{
            "step_index": 1, "event_type": "tool_call", "name": "write",
            "tool_name": "write", "evidence_level": "coarse",
        }]
    }
    assert score_argument_assertions(coarse, gold)[0] is None

    absent_arguments = _argument_run(("c1", "write", None))
    score, details = score_argument_assertions(absent_arguments, gold)
    assert score is None and details[0]["status"] == "insufficient_evidence"


def test_redacted_or_truncated_argument_is_na_and_never_echoed_as_mismatch_evidence():
    run = _argument_run(
        ("c1", "write", {"path": "out/a…[truncated at 5 of 20 chars]"}),
    )
    gold = [{"tool": "write", "path": "path", "equals": "out/a.md"}]
    score, details = score_argument_assertions(run, gold)
    assert score is None
    assert "out/a" not in details[0]["evidence"]


def test_trajectory_evaluator_emits_argument_evidence_and_versioned_rubric():
    case = RoutingCase.model_validate({
        "id": "demo-pos-01",
        "prompt": "write",
        "expect_trajectory": {
            "argument_assertions": [
                {"tool": "write", "path": "path", "equals": "out/a.md"},
            ],
        },
    })
    suite_trajectory = SuiteTrajectorySpec(enabled=True, mode="deterministic")
    suite = {"scoring": {"trajectory": suite_trajectory.model_dump(mode="json")}}
    report = TrajectoryEvaluator().evaluate(EvaluationContext(
        suite=suite,
        snapshot={"suite": suite},
        cases={case.id: case},
        runs=[{"case_id": case.id, "repeat_index": 0, "turn_index": 1,
               **_argument_run(("c1", "write", {"path": "out/a.md"}))}],
    ))

    assert report["metrics"]["argument_correctness"] == 1.0
    assert report["structured"][0]["argument_assertions"][0]["score"] == 1.0
    assert report["versions"] == {
        "trajectory": "trajectory-v1",
        "argument_assertion_schema": ARGUMENT_ASSERTION_SCHEMA_VERSION,
        "argument_correctness_rubric": ARGUMENT_CORRECTNESS_RUBRIC_VERSION,
    }
    assert suite["scoring"]["trajectory"]["argument_schema_version"] == (
        ARGUMENT_ASSERTION_SCHEMA_VERSION
    )


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
