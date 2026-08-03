"""通用 LLM-as-judge 调用层。

这里放的是「怎么调用和解析量具」，不是某个评估维度的业务规则。
不同 evaluator 可以传不同的 system prompt 和 Pydantic 输出模型，但模型配置、
JSON 响应格式和解析路径保持一致。
"""
from __future__ import annotations

import os
from typing import Any, Callable

from pydantic import BaseModel

from contracts import SuiteJudgeSpec
from workflows.litellm_support import quiet_completion

Completion = Callable[..., str]


def extract_json(text: str) -> str:
    """从纯 JSON 或 markdown code fence 中提取一个 JSON object。"""
    value = text.strip().strip("`")
    if value.lower().startswith("json"):
        value = value[4:].lstrip()
    left, right = value.find("{"), value.rfind("}")
    if left == -1 or right == -1:
        raise ValueError("judge 输出里没有 JSON object")
    return value[left : right + 1]


def call_litellm(
    *,
    model: str,
    api_base_env: str,
    api_key_env: str,
    params: dict[str, Any],
    prompt: str,
    system_prompt: str,
) -> str:
    """通过 LiteLLM 调用一个 JSON judge。

    system prompt 由调用方传入：普通语义 judge 和 trajectory judge 的评分规则
    不同，但都必须经过同一条 JSON 调用路径。
    """
    import litellm

    response = quiet_completion(
        litellm,
        model=model,
        api_base=os.environ.get(api_base_env) or None,
        api_key=os.environ.get(api_key_env) or None,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        timeout=300,
        **params,
    )
    return response.choices[0].message.content or ""


class LLMJudge:
    """可复用的 LLM-as-judge。

    维度只负责定义 prompt/rubric；本类负责调用模型和把 JSON 校验成指定的
    Pydantic schema。这样新增一个普通维度不需要新增一个 Judge 子类。
    ``completion`` 可注入测试替身，也可替换成其他 provider adapter。
    """

    def __init__(
        self,
        spec: SuiteJudgeSpec,
        *,
        system_prompt: str,
        completion: Completion = call_litellm,
    ) -> None:
        self.spec = spec
        self.system_prompt = system_prompt
        self.completion = completion

    def call(self, prompt: str) -> str:
        return self.completion(
            model=self.spec.model,
            api_base_env=self.spec.api_base_env,
            api_key_env=self.spec.api_key_env,
            params=self.spec.params,
            prompt=prompt,
            system_prompt=self.system_prompt,
        )

    def judge(self, prompt: str, output_model: type[BaseModel]) -> BaseModel:
        """调用并校验 JSON；schema 错误直接抛出，由 batch 层记录为 judge failure。"""
        raw = self.call(prompt)
        return output_model.model_validate_json(extract_json(raw))
