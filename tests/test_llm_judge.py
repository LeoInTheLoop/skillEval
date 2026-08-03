"""Shared LLM-as-judge adapter contract."""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from contracts import SuiteJudgeSpec
from judges import LLMJudge


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: float


def test_llm_judge_passes_shared_config_and_system_prompt_to_completion():
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return json.dumps({"score": 0.8})

    result = LLMJudge(
        SuiteJudgeSpec(id="judge-v1", model="qwen"),
        system_prompt="你是测试 judge",
        completion=fake,
    ).judge("请评分", _Output)

    assert result.score == 0.8
    assert seen["model"] == "qwen"
    assert seen["prompt"] == "请评分"
    assert seen["system_prompt"] == "你是测试 judge"


def test_llm_judge_rejects_output_that_does_not_match_schema():
    def fake(**_kwargs):
        return json.dumps({"score": "not-a-number"})

    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LLMJudge(
            SuiteJudgeSpec(id="judge-v1", model="qwen"),
            system_prompt="judge",
            completion=fake,
        ).judge("请评分", _Output)
