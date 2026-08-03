from __future__ import annotations

from evaluators import available
from evaluators.trajectory import score_structured
from contracts import TrajectoryEvent


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


def test_evaluator_factory_has_four_report_layers():
    assert {"outcome", "trajectory", "reliability", "efficiency"} <= set(available())
