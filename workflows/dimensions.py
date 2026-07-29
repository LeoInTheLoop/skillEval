"""标准语义评估维度：每个维度 = 一句判定问题 + 评分锚点 + 需要哪些输入。

**为什么是内置 rubric 而不是装 DeepEval / autoevals**（§29 规则 25 的复用审计结论）：

* DeepEval 的 agent 指标里最对口的两个 —— `TaskCompletionMetric`、`ToolCorrectnessMetric`
  —— skillEval **已经有确定性实现**（`score_full.py` 的 `done` 和 `tool_hit`）。
  把它们换成 LLM 判定是倒退，违反 §3.4「能用代码判定的绝不交给 LLM」和 §29 规则 12。
  剩下的 `PlanQuality`/`StepEfficiency` 需要完整 trace，而 `RunResult` 只有聚合的
  `toolSummary`，喂不进去 —— 为用不上的指标背一个带 CLI/telemetry/云端集成的重依赖不划算。
* autoevals 的 `LLMClassifier`（prompt_template + choice_scores）机制很好，本模块的
  结构就是照它来的；但它绑 OpenAI SDK，而 judge 在本项目里必须走独立的 `JUDGE_*`
  通道（见 `grade.py`）。装了它等于仓库里并存两条判分调用路径。

所以复用的是**维度定义与 rubric**（那才是真正沉淀下来的东西），不是那几行调用代码。
每个维度的 `source` 字段注明了它抄自哪里，换库或对表时照着查。

**这里的分数是 0–1 连续分，不是 pass/fail。** 语义质量本来就有程度之分，
二元判定会把「基本做到了但漏了一项」和「完全没做」压成同一个数字。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 一个维度需要的输入。缺了必需输入 → 该维度对该题记 N/A，**不是 0 分**。
DimensionInput = Literal["prompt", "output", "reference", "artifacts"]


class Dimension(BaseModel):
    """一把尺子的定义。改动它等于改判定标准，所以 `version` 必须跟着动。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str                       # 报表里的中文名
    question: str                    # 问 judge 的那句话
    rubric: dict[str, str]           # 分数锚点：{"1.0": "...", "0.5": "...", "0.0": "..."}
    requires: tuple[DimensionInput, ...]
    source: str                      # 这个定义抄自哪 —— 换库/对表时照着查
    version: str = "v1"


# ⚠️ 改任何一条 rubric 都会改变判定结果。改了就 bump 那个维度的 version，
# 否则新旧分数会在同一个名字下混着比（和 §5① 是同一类错误）。
STANDARD_DIMENSIONS: dict[str, Dimension] = {
    "faithfulness": Dimension(
        id="faithfulness",
        label="忠实度（有没有编造）",
        question="输出里陈述的事实，是否都能在用户请求提供的信息中找到依据？",
        rubric={
            "1.0": "所有事实性陈述都有据可查，没有任何用户没提供过的具体数字、名称或结论",
            "0.5": "主体有据可查，但有个别细节无法从输入追溯（可能是合理补全，也可能是编造）",
            "0.0": "出现了明确编造：输入里没有的数字/实体/事实被当作确定信息陈述",
        },
        requires=("prompt", "output"),
        source="RAGAS faithfulness / DeepEval FaithfulnessMetric",
    ),
    "completeness": Dimension(
        id="completeness",
        label="完整度（要求都做了吗）",
        question="用户明确要求的每一项，输出是否都覆盖到了？",
        rubric={
            "1.0": "用户提出的每一项要求都有对应的处理，没有遗漏",
            "0.5": "主要要求做到了，但漏掉了明确提出的次要项",
            "0.0": "遗漏了用户明确要求的核心内容",
        },
        requires=("prompt", "output"),
        source="AGENTS.md §21.6「完整性」；对应 DeepEval TaskCompletion 的语义面",
    ),
    "relevancy": Dimension(
        id="relevancy",
        label="切题度（有没有跑偏）",
        question="输出是否直接回应用户的请求，没有答非所问或大段无关内容？",
        rubric={
            "1.0": "全部内容都服务于用户的请求",
            "0.5": "回应了请求，但夹带了明显无关的展开或冗余",
            "0.0": "答非所问，或主要篇幅在处理用户没问的东西",
        },
        requires=("prompt", "output"),
        source="RAGAS answer_relevancy / DeepEval AnswerRelevancyMetric",
    ),
    "instruction_following": Dimension(
        id="instruction_following",
        label="指令遵循（约束守住了吗）",
        question="用户提出的格式、范围、禁止项等**显式约束**，输出是否都遵守了？",
        rubric={
            "1.0": "所有显式约束都遵守了",
            "0.5": "遵守了大部分，违反了个别非关键约束",
            "0.0": "违反了明确的禁止项或核心格式要求",
        },
        requires=("prompt", "output"),
        source="G-Eval 自定义 criteria 的常见项；对应 OpenAI Evals 的 instruction following",
    ),
    "correctness": Dimension(
        id="correctness",
        label="正确性（与参考答案比）",
        question="输出与参考答案在事实上是否一致？表述可以不同，事实不能冲突。",
        rubric={
            "1.0": "与参考答案事实一致（措辞/详略不同不扣分）",
            "0.5": "部分一致，存在参考答案未涵盖或与之出入的内容，但无直接冲突",
            "0.0": "与参考答案存在事实冲突",
        },
        # 没写 reference 的题，这个维度记 N/A —— 没有参考答案就无从判对错
        requires=("prompt", "output", "reference"),
        source="autoevals Factuality",
    ),
    "conciseness": Dimension(
        id="conciseness",
        label="简洁度（有没有灌水）",
        question="输出是否在把事情说清楚的前提下没有冗余？",
        rubric={
            "1.0": "信息密度高，没有重复或套话",
            "0.5": "可以更紧凑，有一些重复表述或不必要的铺垫",
            "0.0": "大量冗余、重复或空话，核心信息被稀释",
        },
        requires=("prompt", "output"),
        source="G-Eval coherence/fluency 的实用变体",
    ),
}

DEFAULT_DIMENSIONS = ("faithfulness", "completeness", "relevancy")


class DimensionScore(BaseModel):
    """一个维度在一次运行上的判定结果。"""

    model_config = ConfigDict(extra="forbid")

    dimension: str
    score: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1)


def resolve(ids: list[str] | tuple[str, ...]) -> list[Dimension]:
    """按 id 取维度定义；不认识的 id 直接报错，不静默跳过。

    静默跳过会让 suite 里拼错一个维度名变成「这个维度没测」，
    而报表上看起来只是少了一行 —— 又是「静默变空」那一类（HANDOFF §6）。
    """
    unknown = [i for i in ids if i not in STANDARD_DIMENSIONS]
    if unknown:
        raise ValueError(
            f"未知的评估维度：{unknown}；可用的是 {sorted(STANDARD_DIMENSIONS)}"
        )
    return [STANDARD_DIMENSIONS[i] for i in ids]


def applicable(dimension: Dimension, *, has_reference: bool, has_artifacts: bool) -> bool:
    """这道题够不够条件判这个维度。不够就记 N/A，不是 0 分。"""
    if "reference" in dimension.requires and not has_reference:
        return False
    if "artifacts" in dimension.requires and not has_artifacts:
        return False
    return True


def fingerprint(dimensions: list[Dimension]) -> dict[str, str]:
    """维度及其版本 —— 进判定产物，让「这批分用的哪版 rubric」可追溯。"""
    return {d.id: d.version for d in dimensions}


def render(dimensions: list[Dimension]) -> str:
    """渲染成 judge prompt 里的那一段。"""
    blocks = []
    for d in dimensions:
        anchors = "\n".join(f"    {score} = {desc}" for score, desc in sorted(
            d.rubric.items(), reverse=True))
        blocks.append(f"- {d.id}（{d.label}）\n  判定：{d.question}\n  评分锚点：\n{anchors}")
    return "\n".join(blocks)
