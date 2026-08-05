"""P2：从 skill metadata + 业务验收标准生成可人工审核的路由题草稿。

这是薄胶水，不是新的生成框架：模型调用复用 LiteLLM，结构化校验复用 Pydantic，
suite 复用现有严格契约。脚本只写 draft，绝不自动启动 eval。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from contracts import (
    VERSION_DIR,
    CaseType,
    RoutingCase,
    RoutingSuite,
    SkillMeta,
    build_catalog,
    diff_case_sets,
    discover_skills,
    load_cases,
    load_skills,
    validate_case_set,
    version_order,
)
from workflows.litellm_support import quiet_completion

ROOT = Path(__file__).parent.parent
GENERATOR_VERSION = "p2-v0.4"
SYSTEM_PROMPT = """你是 Agent Skill 路由评测题生成器。
只基于给定的 skill metadata 和业务验收标准生成真实用户请求。
输出必须是单个 JSON 对象：{"cases": [...], "review_notes": [...], "rejection_notes": [...]}。
不要输出 Markdown，不要声称生成的 gold 已经人工确认，不要执行测试。"""

# 交叉复审用的是**另一个提问方向**：问「哪些 skill 该激活」，不问「这题该不该拒答」。
# 同一个问题问第二遍只会得到同一个答案，那不是复审。
REJ_REVIEW_SYSTEM_PROMPT = """你是 Agent Skill 路由 gold 的独立复审器。
给你一个 skill catalog 和若干真实用户请求，逐条判断 catalog 里**哪些** skill 应当被激活。
输出必须是单个 JSON 对象：{"verdicts": [{"case_id": ..., "should_activate": [...], "why": ...}]}。
只用 catalog 里出现过的 skill_id；不要输出 Markdown，不要输出隐藏思维链。"""
REPAIR_SYSTEM_PROMPT = """你是 Agent Skill 路由评测题 JSON 修复器。
根据原始生成约束和机器校验错误，修复候选 JSON；不得改变业务目标，不得发明 catalog 外的
skill_id。输出必须是完整的单个 JSON 对象：{"cases": [...], "review_notes": [...],
"rejection_notes": [...]}。不要输出 Markdown。"""


@dataclass(frozen=True)
class GenerationAttempt:
    number: int
    kind: str
    raw: str
    validation_error: str | None = None


class GenerationFailure(ValueError):
    """All paid generation responses failed validation; retain them for repair."""

    def __init__(
        self,
        attempts: list[GenerationAttempt],
        prompt: str,
        recovery_dir: Path | None = None,
    ):
        self.attempts = tuple(attempts)
        self.prompt = prompt
        self.recovery_dir = recovery_dir
        detail = attempts[-1].validation_error if attempts else "unknown generation failure"
        recovery = f"\n已保留可人工修复的失败生成结果：{recovery_dir}" if recovery_dir else ""
        super().__init__(f"生成结果在结构化修复后仍未通过校验：{detail}{recovery}")


class RejectionNote(BaseModel):
    """每道 rej 题必须交代：catalog 里**每一个** skill 为什么都不该激活。

    踩过：`--include-neighbors` 时生成器站在「目标 skill 该不该激活」的视角出 rej 题，
    产出了「Word 字体统一改微软雅黑 + 自动目录」gold=∅ 这种题 —— 而 catalog 里
    明明有 docx。用户若照单全收，模型答对反而被记成误激活，false_activation 结构性虚高，
    整批结论是反的。这个东西代码判不了（它是语义问题），所以强制模型逐题写理由，
    并把理由顶到 REVIEW.md 最前面，让人审有东西可审。
    """
    model_config = ConfigDict(extra="forbid")

    case_id: str
    why_not: str


class GeneratedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[RoutingCase] = Field(min_length=1)
    review_notes: list[str] = Field(default_factory=list)
    rejection_notes: list[RejectionNote] = Field(default_factory=list)


# AUTHORING §2 / AGENTS.md §25.2：tags 只认这四个枚举，自由文本没法聚合。
_CANONICAL_TAG = {
    "pos": "positive",
    "amb": "ambiguous",
    "rej": "no-skill",
    "multi": "multi-skill",
}


def normalize_tags(cases: list[RoutingCase]) -> None:
    """把生成器随手写的中文自由标签换成规范枚举（就地改）。"""
    for case in cases:
        canonical = _CANONICAL_TAG.get(case.case_type)
        if canonical:
            case.tags = [canonical]


def require_rejection_rationale(batch: GeneratedBatch) -> None:
    """rej 题必须逐题说明「catalog 里没有一个 skill 该激活」。"""
    rej_ids = [case.id for case in batch.cases if case.case_type == "rej"]
    explained = {note.case_id for note in batch.rejection_notes}
    missing = [case_id for case_id in rej_ids if case_id not in explained]
    if missing:
        raise ValueError(
            f"这些 rej 题没有给出「为什么 catalog 里没有一个 skill 该激活」：{missing}\n"
            "  → 生成器必须为每道 rej 题写 rejection_notes；缺理由的 rej gold 不可信。"
        )


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _relative_or_absolute(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def resolve_skill_source(
    path: str | Path, *, include_neighbors: bool = False
) -> tuple[Path, list[SkillMeta], list[str]]:
    """Resolve a target without silently importing every personal skill.

    Passing ``~/.codex/skills/topnews`` normally means “evaluate topnews”, not
    “turn unrelated adjacent skills into multi-skill gold labels”.  Neighbours
    remain available through an explicit opt-in for boundary testing.
    """
    source = Path(path).expanduser().resolve()
    if source.is_file() and source.name == "SKILL.md":
        source = source.parent

    # 指到 skill 目录（subjects/pdf）而它只有一版时，那一版就是答案；有多版就必须
    # 指名 —— 出题依据的是 metadata，拿错版本会直接生成错的 gold，不能替用户猜。
    if not (source / "SKILL.md").is_file():
        versions = sorted(
            (d for d in source.glob("v*")
             if VERSION_DIR.fullmatch(d.name) and (d / "SKILL.md").is_file()),
            key=lambda d: version_order(d.name),
        )
        if len(versions) > 1:
            raise ValueError(
                f"{source} 有多个版本：{[d.name for d in versions]} —— "
                f"请指名要为哪一版出题，例如 {source / versions[-1].name}"
            )
        if versions:
            source = versions[0]

    if (source / "SKILL.md").is_file():
        version = source.name if VERSION_DIR.fullmatch(source.name) else ""
        catalog_root = source.parent.parent if version else source.parent
        target = sorted(
            skill_id
            for skill_id, by_version in discover_skills(catalog_root).items()
            for meta in by_version.values()
            if Path(meta.source_path).resolve().parent == source
        )
        if not target:
            raise ValueError(f"无法从 {source / 'SKILL.md'} 解析目标 skill")
        # 目标 skill 钉在用户点名的那一版；邻居仍取各自最新。
        all_skills = load_skills(
            catalog_root, versions={skill_id: version for skill_id in target} if version else None
        )
        skills = all_skills if include_neighbors else [
            skill for skill in all_skills if skill.skill_id in target
        ]
        return catalog_root, skills, target

    skills = load_skills(source)
    if not skills:
        raise ValueError(f"{source} 下没有找到 <skill-id>/<vN>/SKILL.md")
    return source, skills, [skill.skill_id for skill in skills]


def require_routing_metadata(skills: list[SkillMeta], target_skill_ids: list[str]) -> None:
    """Do not ask a model to invent gold from a metadata-free target skill."""
    by_id = {skill.skill_id: skill for skill in skills}
    missing = [skill_id for skill_id in target_skill_ids if not by_id[skill_id].description]
    if missing:
        raise ValueError(
            f"目标 skill 缺少 routing metadata（description）：{missing}\n"
            "  → 在 SKILL.md 的 YAML frontmatter 补 name/description/triggers/exclusions，"
            "或先用一份 metadata overlay；否则生成器无法据此产出可信的路由 gold。"
        )


def required_case_types(skills: list[SkillMeta]) -> tuple[CaseType, ...]:
    """单 skill catalog 没有真实 multi 场景；其余默认四类齐全。"""
    if len(skills) < 2:
        return ("pos", "amb", "rej")
    return ("pos", "amb", "rej", "multi")


def expected_case_mix(
    case_count: int, required_types: tuple[CaseType, ...]
) -> dict[CaseType, int]:
    """Return an exact, reviewable mix instead of a vague "balanced" wish."""
    if case_count < len(required_types):
        raise ValueError(f"case_count={case_count} 小于必需题型数 {len(required_types)}")
    weights = (
        {"pos": 0.4, "amb": 0.3, "rej": 0.2, "multi": 0.1}
        if "multi" in required_types
        else {"pos": 0.4, "amb": 0.3, "rej": 0.3}
    )
    # Largest remainder with a floor of one per required type. Stable tuple
    # order resolves exact ties.
    raw = {case_type: case_count * weights[case_type] for case_type in required_types}
    result: dict[CaseType, int] = {
        case_type: max(1, int(raw[case_type])) for case_type in required_types
    }
    unassigned = case_count - sum(result.values())
    order = sorted(
        required_types,
        key=lambda case_type: (-(raw[case_type] % 1), required_types.index(case_type)),
    )
    for case_type in order[:unassigned]:
        result[case_type] += 1
    return result


def require_case_mix(cases: list[RoutingCase], expected: dict[CaseType, int]) -> None:
    actual = Counter(case.case_type for case in cases)
    actual_mix = {case_type: actual.get(case_type, 0) for case_type in expected}
    if actual_mix != expected:
        raise ValueError(
            f"题型配比不符合生成契约：期望 {expected}，实际 {actual_mix}。"
            "不能用每类至少一道冒充 balanced 数据集。"
        )


def build_generation_prompt(
    *,
    skills: list[SkillMeta],
    target_skill_ids: list[str],
    acceptance: str,
    case_count: int,
    required_types: tuple[CaseType, ...],
) -> str:
    mix = expected_case_mix(case_count, required_types)
    mix_text = json.dumps(mix, ensure_ascii=False)
    return f"""[目标 skill]
{json.dumps(target_skill_ids, ensure_ascii=False)}

[可用 skill catalog（只有 metadata）]
{build_catalog(skills)}

[业务目标与验收标准]
{acceptance.strip()}

[生成约束]
- 一共 {case_count} 道；题型配比：{mix_text}
- id 格式 {{scope}}-{{pos|amb|rej|multi}}-{{两位序号}}；scope 必须逐字等于
  expected_skills（多个用 + 连接），expected_skills=[] 时 scope 必须是 none，禁止缩写
- pos/amb 至少一个 expected skill；rej 的 expected_skills=[]；multi 至少两个不同 skill
- expected_skills 只能使用 catalog 里的真实 skill_id
- prompt 用真实、自然的用户语言，不出现 skill_id，不照抄 triggers
- amb 必须落在两个 skill 的真实边界；rej 要贴边但确实不该激活任何 skill
- ⚠️ rej 的判据是「**上面整个 catalog 里没有任何一个 skill 该被激活**」，
  不是「目标 skill 不该被激活」。一个任务只要落在 catalog 里**任何**一个 skill
  的能力范围内（哪怕不是目标 skill），它就不是 rej 题 —— 应当出成 amb 或 multi，
  gold 写那个真正该激活的 skill
- rejection_notes：**每道 rej 题各一条**，逐条说明 catalog 里每个相关 skill
  为什么都不适用；写不出理由的题请换一道
- rej 只用任务意图体现“不该激活”，禁止写“不需要画图/不要用某 skill/只返回文字”
  之类直接泄漏正确路由的否定提示
- 同 prompt 不得重复；不要用同义改写凑数量
- 字段只允许 id,prompt,expected_skills,tags,severity
- severity 只允许 low、medium、high、critical，禁止 easy/hard 等其他枚举
- review_notes 简述最需要人工复核的 amb/rej gold；不要输出隐藏思维链
- rejection_notes 每项字段只有 case_id, why_not
"""


def build_repair_prompt(*, generation_prompt: str, raw: str, error: Exception) -> str:
    return f"""[原始生成约束]
{generation_prompt}

[未通过校验的候选输出]
{raw}

[机器校验错误]
{type(error).__name__}: {error}

[修复要求]
- 返回完整批次，不要只返回 diff
- 严格满足题数、精确题型配比、ID scope、真实 skill_id 和 rejection_notes 约束
- review_notes 必须是字符串数组；rejection_notes 必须是对象数组
"""


def _extract_json(text: str) -> str:
    value = text.strip().strip("`")
    if value.lower().startswith("json"):
        value = value[4:].lstrip()
    left, right = value.find("{"), value.rfind("}")
    if left == -1 or right == -1:
        raise ValueError("模型输出里没有 JSON object")
    return value[left : right + 1]


def call_litellm(
    *,
    model: str,
    api_base_env: str,
    api_key_env: str,
    params: dict[str, object],
    prompt: str,
    system: str = SYSTEM_PROMPT,
) -> str:
    import litellm

    response = quiet_completion(
        litellm,
        model=model,
        api_base=os.environ.get(api_base_env) or None,
        api_key=os.environ.get(api_key_env) or None,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        timeout=300,
        **params,
    )
    return response.choices[0].message.content or ""


def generate_batch(
    *,
    skills: list[SkillMeta],
    target_skill_ids: list[str],
    acceptance: str,
    case_count: int,
    model: str,
    api_base_env: str,
    api_key_env: str,
    params: dict[str, object],
    completion: Callable[..., str] = call_litellm,
    attempt_sink: list[GenerationAttempt] | None = None,
) -> tuple[GeneratedBatch, str, tuple[CaseType, ...]]:
    required_types = required_case_types(skills)
    prompt = build_generation_prompt(
        skills=skills,
        target_skill_ids=target_skill_ids,
        acceptance=acceptance,
        case_count=case_count,
        required_types=required_types,
    )
    attempts: list[GenerationAttempt] = []
    current_prompt = prompt
    current_system = SYSTEM_PROMPT
    for number, kind in ((1, "generate"), (2, "repair")):
        call_kwargs = {
            "model": model,
            "api_base_env": api_base_env,
            "api_key_env": api_key_env,
            "params": params,
            "prompt": current_prompt,
        }
        # Preserve the established injected-completion signature on the first
        # call; the repair call must explicitly identify its different role.
        if number > 1:
            call_kwargs["system"] = current_system
        try:
            raw = completion(**call_kwargs)
        except Exception as error:
            # A first-call provider failure has no candidate to save or repair.
            # If the conditional repair call fails, keep the already paid first
            # response and record why automated recovery stopped.
            if number == 1:
                raise
            attempt = GenerationAttempt(
                number=number,
                kind=kind,
                raw="",
                validation_error=_one_line(
                    f"provider error during repair: {type(error).__name__}: {error}", 2000
                ),
            )
            attempts.append(attempt)
            if attempt_sink is not None:
                attempt_sink.append(attempt)
            raise GenerationFailure(attempts, prompt) from error
        try:
            batch = GeneratedBatch.model_validate_json(_extract_json(raw))
            validate_case_set(
                batch.cases,
                skill_ids=(skill.skill_id for skill in skills),
                required_types=required_types,
                max_cases=30,
            )
            normalize_tags(batch.cases)
            if len(skills) > 1:
                require_rejection_rationale(batch)
            if len(batch.cases) != case_count:
                raise ValueError(
                    f"要求生成 {case_count} 道，模型实际返回 {len(batch.cases)} 道"
                )
            require_case_mix(batch.cases, expected_case_mix(case_count, required_types))
        except Exception as error:  # schema + complete-batch contract, repaired once
            attempt = GenerationAttempt(
                number=number,
                kind=kind,
                raw=raw,
                validation_error=_one_line(f"{type(error).__name__}: {error}", 2000),
            )
            attempts.append(attempt)
            if attempt_sink is not None:
                attempt_sink.append(attempt)
            if number == 1:
                current_prompt = build_repair_prompt(
                    generation_prompt=prompt,
                    raw=raw,
                    error=error,
                )
                current_system = REPAIR_SYSTEM_PROMPT
                continue
            raise GenerationFailure(attempts, prompt) from error
        attempt = GenerationAttempt(number=number, kind=kind, raw=raw)
        attempts.append(attempt)
        if attempt_sink is not None:
            attempt_sink.append(attempt)
        return batch, prompt, required_types
    raise AssertionError("unreachable")


def _one_line(text: str, limit: int = 240) -> str:
    """任何要进 `#` 注释头的文本都得先过这里，不然一个换行就废掉整个 header。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class RejVerdict(BaseModel):
    """复审器对一道 rej 题的独立判定。`should_activate` 非空 = 它不同意 gold=∅。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    should_activate: list[str] = Field(default_factory=list)
    why: str


class RejReviewBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdicts: list[RejVerdict] = Field(default_factory=list)


class RejReview(BaseModel):
    """一次 rej 交叉复审的结果 —— 包括「没跑成」也要留痕，不许静默变空。"""

    model_config = ConfigDict(extra="forbid")

    model: str
    reviewed: int = 0
    verdicts: list[RejVerdict] = Field(default_factory=list)
    error: str | None = None

    @property
    def disputed(self) -> list[RejVerdict]:
        return [verdict for verdict in self.verdicts if verdict.should_activate]

    def summary(self) -> str:
        """进 dataset header 的一行 —— 题集自己带着这批 rej gold 被谁看过。"""
        if self.error:
            return _one_line(f"FAILED（{self.reviewed} 道未复审）: {self.error}")
        if not self.reviewed:
            return "SKIPPED（这批没有 rej 题）"
        disputed = ",".join(verdict.case_id for verdict in self.disputed)
        return _one_line(
            f"{self.model} | 复审 {self.reviewed} 道 | 争议 {len(self.disputed)} 道"
            + (f"：{disputed}" if disputed else "")
        )


def build_rej_review_prompt(*, skills: list[SkillMeta], cases: list[RoutingCase]) -> str:
    """盲判：只给 catalog 和题面，**不给 gold、不给生成时写的 why_not**。

    给了就是让同一个模型确认自己刚才的答案 —— 那种「复审」永远同意，一点信息量都没有。
    换个提问方向（「哪些 skill 该激活」而不是「这题该不该拒答」）才有可能翻案。
    """
    return f"""[可用 skill catalog（只有 metadata）]
{build_catalog(skills)}

[待判定的用户请求]
{json.dumps([{"case_id": case.id, "prompt": case.prompt} for case in cases],
            ensure_ascii=False, indent=2)}

[判定要求]
- 逐条判断：这个请求落在上面**任何**一个 skill 的能力范围内吗？
- should_activate 写该激活的 skill_id（可以多个）；一个都不该激活时写 []
- 只能使用 catalog 里出现过的 skill_id，不要发明新的
- why 一句话说明判据：是哪个 skill 的哪部分能力覆盖了它，或为什么全都不覆盖
- 每个 case_id 各一条，不要漏也不要多
"""


def check_verdicts(
    batch: RejReviewBatch, cases: list[RoutingCase], skill_ids: set[str]
) -> None:
    """程序侧校形状：一题一条、skill_id 真实存在。语义谁对谁错留给人审。"""
    want = {case.id for case in cases}
    got = {verdict.case_id for verdict in batch.verdicts}
    if want != got:
        raise ValueError(
            f"复审结果与待判题不一致：缺 {sorted(want - got)}，多 {sorted(got - want)}"
        )
    unknown = sorted(
        {skill for verdict in batch.verdicts for skill in verdict.should_activate}
        - skill_ids
    )
    if unknown:
        raise ValueError(f"复审器给出了 catalog 里没有的 skill_id：{unknown}")


def review_rejections(
    *,
    skills: list[SkillMeta],
    cases: list[RoutingCase],
    model: str,
    api_base_env: str,
    api_key_env: str,
    params: dict[str, object],
    completion: Callable[..., str] = call_litellm,
) -> RejReview:
    """第二个 LLM 节点：盲判每道 rej 题，与 gold=∅ 对不上的顶到 REVIEW.md。

    为什么值得多花一次调用：rej gold 判错不是少一道题的事 —— 模型答对反而被记成误激活，
    `false_activation` 结构性虚高，整批结论是反过来的（见 `RejectionNote` 的踩坑记录）。
    生成器自己写的 `why_not` 是自证，程序又判不了语义，所以这里放一双独立的眼睛。

    **它只标注，不阻断。** 谁对谁错是语义问题，代码判不了；草稿本来就停在人审门，
    复审的作用是让人知道该先看哪几道题。复审自己失败也照样返回（带 `error`）——
    不能让量具挂掉就把已经付过费的一整批题扔了，但必须写进报告，不许静默算作「无争议」。
    """
    rejections = [case for case in cases if case.case_type == "rej"]
    if not rejections:
        return RejReview(model=model)
    prompt = build_rej_review_prompt(skills=skills, cases=rejections)
    try:
        raw = completion(
            model=model,
            api_base_env=api_base_env,
            api_key_env=api_key_env,
            params=params,
            prompt=prompt,
            system=REJ_REVIEW_SYSTEM_PROMPT,
        )
        batch = RejReviewBatch.model_validate_json(_extract_json(raw))
        check_verdicts(batch, rejections, {skill.skill_id for skill in skills})
    except Exception as error:  # noqa: BLE001 —— 复审是量具，挂了也不能丢掉整批题
        return RejReview(
            model=model,
            reviewed=len(rejections),
            # 压成单行：这行会进 dataset 的 `#` 注释头，pydantic 的多行报错会把
            # 后面的 `# review_status: DRAFT` 挤到注释区外面 —— DRAFT 门当场失效。
            error=_one_line(f"{type(error).__name__}: {error}"),
        )
    return RejReview(model=model, reviewed=len(rejections), verdicts=batch.verdicts)


def build_suite_draft(
    *,
    catalog_root: Path,
    dataset_path: Path,
    scope: str,
    model_id: str,
    model: str,
    api_base_env: str,
    api_key_env: str,
    params: dict[str, object],
    target_skill_ids: list[str],
    include_skill_ids: list[str],
) -> RoutingSuite:
    data = {
        "suite_id": f"generated_{scope}",
        "suite_version": "0.1-draft",
        "description": "P2 自动生成草稿；人工审核并改为正式版本后才能运行",
        "dataset": _relative_or_absolute(dataset_path),
        "runtime": "litellm",
        "skills": {
            "dir": _relative_or_absolute(catalog_root),
            "target": target_skill_ids,
            "include": include_skill_ids,
            "mode": "routing_only",
            "cfg": "v1-draft",
        },
        "models": [
            {
                "id": model_id,
                "model": model,
                "api_base_env": api_base_env,
                "api_key_env": api_key_env,
                "params": params,
            }
        ],
        "tools": [],
        "repeats": 3,
        "timeout_seconds": 300,
        "scoring": {
            "metrics": [
                "exact_set_match",
                "top1",
                "multi_exact",
                "no_skill_rejection",
                "false_activation",
                "by_type",
                "per_skill_prf",
                "confusion_matrix",
            ],
            "gate": {
                "exact_set_match": ">= 0.80",
                "no_skill_rejection": ">= 0.80",
                "false_activation": "<= 0.20",
            },
        },
    }
    return RoutingSuite.model_validate(data)


def draft_dataset_name(scope: str) -> str:
    """草稿也要带 scope —— 文件名会原样变成 output 目录名的第一维。

    踩过：草稿固定叫 `dataset.jsonl`，直接跑就得到 `outputs/dataset__qwen__v1-draft/`，
    AUTHORING §1.4 的四维命名第一维直接作废。
    """
    return f"routing_{scope}_v0.1-draft.jsonl"


def _write_attempt_files(directory: Path, attempts: list[GenerationAttempt]) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    records = []
    for attempt in attempts:
        filename = f"attempt-{attempt.number:02d}-{attempt.kind}.raw.txt"
        (directory / filename).write_text(attempt.raw, encoding="utf-8")
        candidate_name = None
        try:
            candidate = json.loads(_extract_json(attempt.raw))
        except (ValueError, json.JSONDecodeError):
            pass
        else:
            candidate_name = f"attempt-{attempt.number:02d}-{attempt.kind}.candidate.json"
            (directory / candidate_name).write_text(
                json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        records.append({
            "number": attempt.number,
            "kind": attempt.kind,
            "raw_file": filename,
            "raw_sha256": "sha256:" + hashlib.sha256(attempt.raw.encode()).hexdigest(),
            "candidate_file": candidate_name,
            "validation_error": attempt.validation_error,
        })
    (directory / "manifest.json").write_text(
        json.dumps(
            {"generator_version": GENERATOR_VERSION, "attempts": records},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def preserve_failed_generation(
    *, output_dir: Path, failure: GenerationFailure, model: str
) -> Path:
    """Keep invalid paid responses in a non-runnable, human-editable bundle."""
    root = output_dir / "generation_failures"
    stem = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    destination = root / stem
    suffix = 1
    while destination.exists():
        suffix += 1
        destination = root / f"{stem}-{suffix}"
    _write_attempt_files(destination, list(failure.attempts))
    (destination / "RECOVER.md").write_text(
        "# Generation failed validation\n\n"
        f"Model: `{model}`  \nGenerator: `{GENERATOR_VERSION}`\n\n"
        "No runnable dataset or suite was created. The paid model responses are retained as "
        "`*.raw.txt`; when JSON extraction succeeded, the adjacent `*.candidate.json` is the "
        "best starting point for manual repair. Review the exact validation errors in "
        "`manifest.json`. You may either edit a candidate into a separately named dataset, or "
        "rerun the same init command: this failure bundle does not block reuse of the existing "
        "skill snapshot and will not be overwritten. Never mark this bundle APPROVED without "
        "human gold review.\n",
        encoding="utf-8",
    )
    return destination


def _render_rej_review(review: RejReview | None) -> list[str]:
    """复审那一段。三种状态都要显式印出来 —— 没跑、跑挂了、跑了没争议不是一回事。"""
    if review is None:
        return ["## ⚠️ 未做 rej 交叉复审（--skip-rej-review）", "",
                "> 这批 rej gold 只有生成器自己的说法，人审时请逐题自己核。", ""]
    if review.error:
        return [f"## ⚠️ rej 交叉复审未完成：{review.error}", "",
                "> 复审器挂了，不是「没有争议」。这批 rej gold 请逐题自己核。", ""]
    if not review.reviewed:
        return []
    if not review.disputed:
        return [f"## ✅ rej 交叉复审：{review.reviewed} 道全部同意 gold=∅"
                f"（复审模型 {review.model}，盲判，未看 gold）", ""]
    return [
        f"## ❌ 先解决这些 · 交叉复审认为 {len(review.disputed)}/{review.reviewed} "
        f"道 rej 题其实该激活 skill",
        "",
        "> 复审只看 catalog 和题面，没看 gold。它点名了具体 skill，说明 `gold=∅` 很可能是错的：",
        "> 这种题会让模型答对反被记成误激活，`false_activation` 结构性虚高，整批结论反过来。",
        "> 处理方式二选一：把 gold 改成它点名的 skill（题型也跟着从 rej 改成 amb/multi），或换一道题。",
        "",
        *[f"- **{verdict.case_id}** → 复审认为该激活 "
          f"{'、'.join(f'`{skill}`' for skill in verdict.should_activate)}：{verdict.why}"
          for verdict in review.disputed],
        "",
    ]


def write_draft(
    *,
    output_dir: Path,
    batch: GeneratedBatch,
    suite: RoutingSuite,
    prompt: str,
    acceptance: str,
    skills: list[SkillMeta],
    model: str,
    params: dict[str, object],
    scope: str,
    reference: Path | None = None,
    rej_review: RejReview | None = None,
    generation_attempts: list[GenerationAttempt] | None = None,
) -> tuple[Path, Path]:
    dataset_path = output_dir / draft_dataset_name(scope)
    suite_path = output_dir / "suite.yaml"
    reserved = [dataset_path, suite_path, output_dir / "REVIEW.md"]
    if generation_attempts:
        reserved.append(output_dir / "generation")
    if reference:
        reserved.append(output_dir / "case-diff.json")
    collisions = [path for path in reserved if path.exists()]
    if collisions:
        raise FileExistsError(f"拒绝覆盖已有草稿：{collisions}")

    output_dir.mkdir(parents=True, exist_ok=True)
    if generation_attempts:
        _write_attempt_files(output_dir / "generation", generation_attempts)
    header = [
        f"# generator.version: {GENERATOR_VERSION}",
        f"# generator.model: {model}",
        f"# generator.params: {json.dumps(params, ensure_ascii=False, sort_keys=True)}",
        f"# generator.prompt_hash: {_sha(SYSTEM_PROMPT + chr(10) + prompt)}",
        f"# source.acceptance_hash: {_sha(acceptance)}",
        "# source.skill_hashes: "
        + json.dumps(
            {skill.skill_id: skill.content_hash for skill in skills},
            ensure_ascii=False,
            sort_keys=True,
        ),
        f"# generated_at: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        # 题集自己带着「这批 rej gold 被第二双眼睛看过没有」——
        # 只写在 REVIEW.md 里的话，题集一被复制到正式目录就丢了。
        "# rejection_review: "
        + (rej_review.summary() if rej_review else "SKIPPED（未启用交叉复审）"),
        "# review_status: DRAFT — gold 未经人工确认，禁止直接运行",
    ]
    case_lines = [
        json.dumps(
            case.model_dump(
                include={"id", "prompt", "expected_skills", "tags", "severity"}
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for case in batch.cases
    ]
    dataset_path.write_text("\n".join(header + case_lines) + "\n", encoding="utf-8")
    suite_path.write_text(
        yaml.safe_dump(suite.canonical_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # rej 的理由顶到最前面：它是最容易出错、也最贵的一类 gold（错了会让
    # false_activation 结构性虚高，整批结论反过来）。有争议的排在理由之前 ——
    # 生成器的自证摆在独立复审的反对意见前面，等于先给人看被告的陈述。
    review_lines = ["# 人工审核备注", "", *_render_rej_review(rej_review)]
    if batch.rejection_notes:
        review_lines += [
            "## 必审 · 每道 rej 为什么 catalog 里没有一个 skill 该激活",
            "",
            "> 只要这道题落在 catalog 里**任何**一个 skill 的能力范围内，它就不该是 rej。",
            "",
            *[f"- **{note.case_id}**：{note.why_not}" for note in batch.rejection_notes],
            "",
        ]
    review_lines += ["## 模型自述的高风险 gold", "", *[f"- {note}" for note in batch.review_notes]]
    (output_dir / "REVIEW.md").write_text("\n".join(review_lines) + "\n", encoding="utf-8")

    if reference:
        diff = diff_case_sets(load_cases(reference), batch.cases)
        (output_dir / "case-diff.json").write_text(
            json.dumps(diff, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return dataset_path, suite_path


def _slug(target_skill_ids: list[str]) -> str:
    raw = target_skill_ids[0] if len(target_skill_ids) == 1 else "all"
    return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)


def generate_case_draft(
    *,
    catalog_root: Path,
    skills: list[SkillMeta],
    target_skill_ids: list[str],
    acceptance: str,
    count: int,
    model_id: str,
    model: str,
    api_base_env: str,
    api_key_env: str,
    output_dir: Path | None = None,
    reference: Path | None = None,
    completion: Callable[..., str] | None = None,
    review_rej: bool = True,
) -> tuple[Path, Path, tuple[CaseType, ...]]:
    """Generate and persist one review-gated draft from already resolved inputs.

    Both the standalone CLI and ``pipeline init`` call this function.  Keeping
    resolution/import outside makes the high-level onboarding flow reuse the
    exact same generator contract without emulating its argparse state.
    """
    require_routing_metadata(skills, target_skill_ids)
    required_types = required_case_types(skills)
    if count < len(required_types):
        raise ValueError(
            f"count={count} 不足以覆盖必需题型 {list(required_types)}；"
            f"请至少生成 {len(required_types)} 道"
        )
    scope = _slug(target_skill_ids)
    resolved_output = (
        output_dir
        if output_dir is not None
        else ROOT / "evals" / "drafts" / scope
    ).resolve()
    collisions = [
        path
        for path in (
            resolved_output / draft_dataset_name(scope),
            resolved_output / "suite.yaml",
            resolved_output / "REVIEW.md",
            resolved_output / "generation",
        )
        if path.exists()
    ]
    if collisions:
        raise FileExistsError(f"拒绝覆盖已有草稿：{collisions}")
    params: dict[str, object] = {"temperature": 0}
    generation_attempts: list[GenerationAttempt] = []
    try:
        batch, prompt, required_types = generate_batch(
            skills=skills,
            target_skill_ids=target_skill_ids,
            acceptance=acceptance,
            case_count=count,
            model=model,
            api_base_env=api_base_env,
            api_key_env=api_key_env,
            params=params,
            completion=completion or call_litellm,
            attempt_sink=generation_attempts,
        )
    except GenerationFailure as error:
        recovery = preserve_failed_generation(
            output_dir=resolved_output,
            failure=error,
            model=model,
        )
        raise GenerationFailure(
            list(error.attempts), error.prompt, recovery_dir=recovery
        ) from error
    # 第二次调用：拿同一批 rej 题去盲判一遍。放在写文件前，好让结论直接进 REVIEW.md。
    rej_review = (
        review_rejections(
            skills=skills,
            cases=batch.cases,
            model=model,
            api_base_env=api_base_env,
            api_key_env=api_key_env,
            params=params,
            completion=completion or call_litellm,
        )
        if review_rej
        else None
    )
    if rej_review and rej_review.error:
        print(f"⚠️ rej 交叉复审未完成：{rej_review.error}\n"
              "  草稿照常写出，但这批 rej gold 没有第二双眼睛看过。")
    elif rej_review and rej_review.disputed:
        print(f"❌ 交叉复审对 {len(rej_review.disputed)}/{rej_review.reviewed} 道 rej 题有异议："
              f"{[verdict.case_id for verdict in rej_review.disputed]}\n"
              "  它们的 gold=∅ 很可能是错的；REVIEW.md 顶部有逐题理由，先解决再批准。")
    dataset_path = resolved_output / draft_dataset_name(scope)
    suite = build_suite_draft(
        catalog_root=catalog_root,
        dataset_path=dataset_path,
        scope=scope,
        model_id=model_id,
        model=model,
        api_base_env=api_base_env,
        api_key_env=api_key_env,
        params=params,
        target_skill_ids=target_skill_ids,
        include_skill_ids=[skill.skill_id for skill in skills],
    )
    dataset_path, suite_path = write_draft(
        output_dir=resolved_output,
        batch=batch,
        suite=suite,
        prompt=prompt,
        acceptance=acceptance,
        skills=skills,
        model=model,
        params=params,
        scope=scope,
        reference=reference,
        rej_review=rej_review,
        generation_attempts=generation_attempts,
    )
    return dataset_path, suite_path, required_types


def main() -> None:
    parser = argparse.ArgumentParser(
        description="skill metadata + 验收标准 → dataset/suite 草稿（生成后停在人审门）"
    )
    parser.add_argument("--skill-dir", required=True, help="catalog 根目录或目标 skill 目录")
    acceptance_group = parser.add_mutually_exclusive_group(required=True)
    acceptance_group.add_argument("--acceptance", help="业务目标与验收标准文本")
    acceptance_group.add_argument("--acceptance-file", help="业务目标与验收标准文件")
    parser.add_argument("--output-dir", help="默认 evals/drafts/<scope>")
    parser.add_argument("--reference", help="可选人工基准集；输出按 case_id 的 prompt/gold 差异")
    parser.add_argument(
        "--count", type=int, default=10, choices=range(3, 31),
        help="生成 3–30 道草稿；默认 10 题用于快速验证，30 题适合人工审核后的扩展试跑",
    )
    parser.add_argument(
        "--include-neighbors", action="store_true",
        help="将相邻 skill 纳入 catalog，用于 amb/multi；默认只评估目标 skill",
    )
    parser.add_argument(
        "--skip-rej-review", action="store_true",
        help="跳过 rej gold 的盲判交叉复审（省一次模型调用；REVIEW.md 会标明没做）",
    )
    parser.add_argument("--model-id", default="qwen3.7-max-2026-05-17")
    parser.add_argument("--model", default="openai/qwen3.7-max-2026-05-17")
    parser.add_argument("--api-base-env", default="DASHSCOPE_BASE_URL")
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    acceptance = (
        Path(args.acceptance_file).read_text(encoding="utf-8")
        if args.acceptance_file
        else args.acceptance
    )
    assert acceptance is not None
    catalog_root, skills, target_skill_ids = resolve_skill_source(
        args.skill_dir, include_neighbors=args.include_neighbors
    )
    require_routing_metadata(skills, target_skill_ids)
    required_types = required_case_types(skills)
    if args.count < len(required_types):
        mode_hint = (
            "；当前开启了 --include-neighbors，生成器至少需要覆盖 "
            "pos/amb/rej/multi 四类"
            if args.include_neighbors and "multi" in required_types
            else ""
        )
        parser.error(
            f"--count={args.count} 不足以覆盖必需题型 {list(required_types)}"
            f"{mode_hint}；请把 --count 至少设为 {len(required_types)}"
        )
    dataset_path, suite_path, required_types = generate_case_draft(
        catalog_root=catalog_root,
        skills=skills,
        target_skill_ids=target_skill_ids,
        acceptance=acceptance,
        count=args.count,
        model_id=args.model_id,
        model=args.model,
        api_base_env=args.api_base_env,
        api_key_env=args.api_key_env,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        reference=Path(args.reference).resolve() if args.reference else None,
        # 显式传：默认值在 def 时就绑死了，测试替换模块属性会完全不生效 ——
        # 那样 CLI 冒烟测试会真的去打 API（而不是在 mock 上跑）。
        completion=call_litellm,
        review_rej=not args.skip_rej_review,
    )
    print(
        f"已生成 DRAFT：{args.count} cases，题型要求={list(required_types)}\n"
        f"  dataset: {dataset_path}\n  suite:   {suite_path}\n"
        "已停止：请先审核 REVIEW.md 与 git diff；确认 gold 后再移动到正式目录运行。"
    )


if __name__ == "__main__":
    main()
