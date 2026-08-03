"""测试用例契约与加载器（AGENTS.md §25.2）。

命名规范与写题规则见 evals/AUTHORING.md。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .context import RoutingContext
from .trajectory import TrajectoryExpectation

Severity = Literal["low", "medium", "high", "critical"]
CaseType = Literal["pos", "amb", "rej", "multi"]
_CASE_ID = re.compile(
    r"^(?P<scope>[A-Za-z0-9_.+-]+)-(?P<type>pos|amb|rej|multi)-(?P<seq>\d{2,})$"
)


class FullEvalTurn(BaseModel):
    """Full-eval 的一个后续轮次。

    顶层 ``RoutingCase`` 仍表示第 1 轮，``turns`` 只保存第 2 轮起的增量。
    这样历史单轮 JSONL 不需要迁移，多轮题也不会把同一组字段维护两遍。
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    files: list[str] = Field(default_factory=list)
    expect_artifacts: list[str] = Field(default_factory=list)
    expect_tools: list[str] = Field(default_factory=list)
    forbid_artifacts: bool = False
    expect_assertions: list[str] = Field(default_factory=list)
    reference: str | None = None
    # 本轮结束时 workspace 中必须仍可见的路径。与 expect_artifacts 不同：
    # 后者只看本轮新增/修改，前者也能检查上一轮留下但本轮未改的文件。
    expect_workspace_files: list[str] = Field(default_factory=list)
    # 该轮问题故意省略前文细节，必须依赖 session 历史才能完成。
    # 确定性指标按本轮 task completion 计算，不让 LLM 自评“我记住了”。
    requires_context: bool = False
    expect_trajectory: TrajectoryExpectation | None = None


class RoutingCase(BaseModel):
    """一个测试用例。expected_skills 为空 = 期望模型返回 No-Skill。

    routing 与 full 共用这一个契约：full 只是多填了两个断言字段（§21.2 确定性断言优先）。
    没有为 full 单开一个 class/loader —— 那会立刻带来两套加载、两套校验、两套坏题检查。
    """
    model_config = ConfigDict(extra="forbid")

    id: str
    prompt: str
    # 生产路由不能只看最后一句：这里可带 role、长上下文、历史对话和 tool/MCP 目录。
    context: RoutingContext = Field(default_factory=RoutingContext)
    expected_skills: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    severity: Severity = "medium"

    # --- 输入素材（AGENTS.md §11.4）---
    # 相对仓库根的路径，素材放 evals/fixtures/。Environment Backend 会在
    # prepared() 里把它们**只读**物化进 workspace，模型看到的是 workspace 下的同名文件。
    # 没有这个字段之前，「给助手一份 .docx 文字稿」这类题只能把内容整段内联进 prompt，
    # 跟真实用法差得很远。声明了但文件不存在 → plan/run 前直接拦下。
    files: list[str] = Field(default_factory=list)

    # --- full eval 的断言；routing 题留空即可（留空 = 该维度对本题 N/A）---
    # 相对 workspace 的路径 glob，如 "out/q3-sales.csv"、"out/*.md"。
    # 期望 MIME 由后缀推出，不再单独写一遍（见 score_full.py 的 ponytail 注释）。
    expect_artifacts: list[str] = Field(default_factory=list)
    # 必须调到的 tool 名，如 ["write"]。runtime 报告不了 tool 就整体记 N/A。
    expect_tools: list[str] = Field(default_factory=list)
    # 拒答题专用：这题**不该留下任何文件**。
    # 不能靠 expect_artifacts 留空表达 —— 留空的语义是「该维度 N/A」，
    # 两者混用会让「没让你落文件」和「没在看这个维度」变成同一个数字。
    forbid_artifacts: bool = False

    # --- 语义断言：交给 judge 模型判（grade.py），确定性断言判不了的才写这里 ---
    # 一条一句可判真假的话，如 "报告引用了输入 CSV 里的真实数字，没有编造"。
    # 能用 expect_artifacts / expect_tools 表达的**一律别写这里** —— 那些是代码
    # 能确定的事，交给模型判只会引入抖动和成本（§21.2 确定性断言优先）。
    expect_assertions: list[str] = Field(default_factory=list)
    # 参考答案。只有 `correctness` 维度需要它（比对事实是否冲突）；
    # 不写的题该维度记 **N/A**，不是 0 分 —— 没有参考答案就无从判对错。
    reference: str | None = None
    # 多轮 full eval：顶层字段是 turn 1，这里从 turn 2 开始。
    turns: list[FullEvalTurn] = Field(default_factory=list)
    expect_workspace_files: list[str] = Field(default_factory=list)
    requires_context: bool = False
    expect_trajectory: TrajectoryExpectation | None = None

    @property
    def case_type(self) -> str:
        """从 id 取题型段：pdf-pos-01 → pos（AUTHORING.md §1.2）。"""
        parts = self.id.split("-")
        return parts[-2] if len(parts) >= 3 else "?"

    @property
    def turn_count(self) -> int:
        return 1 + len(self.turns)

    def resolved_turn(self, turn_index: int) -> FullEvalTurn:
        """按 1-based turn index 返回统一的轮次视图。"""
        if turn_index < 1 or turn_index > self.turn_count:
            raise IndexError(
                f"{self.id}: turn_index={turn_index} 超界；共有 {self.turn_count} 轮"
            )
        if turn_index > 1:
            return self.turns[turn_index - 2]
        return FullEvalTurn(
            prompt=self.prompt,
            files=self.files,
            expect_artifacts=self.expect_artifacts,
            expect_tools=self.expect_tools,
            forbid_artifacts=self.forbid_artifacts,
            expect_assertions=self.expect_assertions,
            reference=self.reference,
            expect_workspace_files=self.expect_workspace_files,
            requires_context=self.requires_context,
            expect_trajectory=self.expect_trajectory,
        )

    @property
    def all_files(self) -> list[str]:
        return [*self.files, *(raw for turn in self.turns for raw in turn.files)]


def load_cases(path: str | Path) -> list[RoutingCase]:
    """读 JSONL，跳过空行和 # 注释行。非法行直接抛 ValidationError，不静默跳过。"""
    cases: list[RoutingCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(RoutingCase.model_validate_json(line))
    return cases


def dataset_review_status(path: str | Path) -> str | None:
    """Return an optional human-review marker from a JSONL comment header.

    Generated datasets start as ``DRAFT`` and must not accidentally become a
    paid experiment merely because their suite happens to validate.  Hand
    authored historical datasets do not need this marker, so ``None`` remains
    runnable.  The deliberately tiny format keeps a manual approval auditable
    in a normal git diff::

        # review_status: APPROVED — reviewer/date/reason
    """
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            break
        marker = "review_status:"
        if marker in line:
            return line.split(marker, 1)[1].strip().split(maxsplit=1)[0].upper()
    return None


def require_approved_dataset(path: str | Path) -> None:
    """Refuse to execute a generated draft before a human approves its gold."""
    status = dataset_review_status(path)
    if status == "DRAFT":
        raise ValueError(
            f"测试集仍是 DRAFT：{path}\n"
            "  → 先人工审核 REVIEW.md 与 dataset.jsonl 的 gold；确认后将头部改为 "
            "`# review_status: APPROVED — 审核人/日期/依据`，再运行。"
        )


class CaseSetValidationError(ValueError):
    """生成集的跨题错误；一次列全，避免修一条、重跑、再撞下一条。"""

    def __init__(self, issues: Iterable[str]):
        self.issues = tuple(issues)
        super().__init__("测试集校验失败：\n- " + "\n- ".join(self.issues))


def validate_case_set(
    cases: list[RoutingCase],
    *,
    skill_ids: Iterable[str],
    required_types: Iterable[CaseType] = ("pos", "amb", "rej"),
    max_cases: int | None = None,
) -> list[RoutingCase]:
    """校验生成集的跨题约束（P2）。

    `RoutingCase` 负责单行 Schema；这里负责只有看完整批次才能判断的错误：
    重复、gold 冲突、未知 skill 和题型配比。历史人工集不会在 `load_cases()` 时
    被这套新规则追溯拒绝；生成器必须显式调用本函数，先拦坏题再写文件。
    """
    issues: list[str] = []
    valid_skills = set(skill_ids)
    required = tuple(dict.fromkeys(required_types))

    if not cases:
        issues.append("至少要有 1 道题")
    if max_cases is not None and len(cases) > max_cases:
        issues.append(f"开发期最多 {max_cases} 道题，实际 {len(cases)} 道")

    duplicate_ids = sorted(
        case_id for case_id, count in Counter(case.id for case in cases).items() if count > 1
    )
    if duplicate_ids:
        issues.append(f"case id 重复：{duplicate_ids}")

    prompts: dict[str, RoutingCase] = {}
    for case in cases:
        normalized_prompt = " ".join(case.prompt.split()).casefold()
        previous = prompts.get(normalized_prompt)
        if previous:
            if previous.expected_skills != case.expected_skills:
                issues.append(
                    f"同 prompt 不同 gold：{previous.id}={previous.expected_skills}，"
                    f"{case.id}={case.expected_skills}"
                )
            else:
                issues.append(f"prompt 重复：{previous.id} 与 {case.id}")
        else:
            prompts[normalized_prompt] = case

        id_match = _CASE_ID.fullmatch(case.id)
        if not id_match:
            issues.append(f"{case.id}: id 必须符合 {{scope}}-{{pos|amb|rej|multi}}-{{seq}}")
        else:
            id_scope = set(id_match.group("scope").split("+"))
            gold_scope = set(case.expected_skills) or {"none"}
            if id_scope != gold_scope:
                issues.append(
                    f"{case.id}: id scope={sorted(id_scope)} 与 "
                    f"expected_skills scope={sorted(gold_scope)} 不一致"
                )

        duplicated_gold = sorted(
            skill_id
            for skill_id, count in Counter(case.expected_skills).items()
            if count > 1
        )
        if duplicated_gold:
            issues.append(f"{case.id}: expected_skills 重复：{duplicated_gold}")

        unknown = sorted(set(case.expected_skills) - valid_skills)
        if unknown:
            issues.append(f"{case.id}: gold 指向不存在的 skill：{unknown}")

        if case.case_type == "rej" and case.expected_skills:
            issues.append(f"{case.id}: rej 题的 expected_skills 必须为空")
        elif case.case_type in {"pos", "amb"} and not case.expected_skills:
            issues.append(f"{case.id}: {case.case_type} 题必须至少有一个 expected skill")
        elif case.case_type == "multi" and len(set(case.expected_skills)) < 2:
            issues.append(f"{case.id}: multi 题必须至少有两个不同 expected skill")

    present_types = {case.case_type for case in cases}
    missing_types = [case_type for case_type in required if case_type not in present_types]
    if missing_types:
        issues.append(f"题型配比缺类：{missing_types}；现有：{sorted(present_types)}")

    if issues:
        raise CaseSetValidationError(issues)
    return cases


def diff_case_sets(
    reference: list[RoutingCase],
    candidate: list[RoutingCase],
) -> list[dict[str, object]]:
    """按 case id 解释两套题的 prompt/gold 分歧，供 P2 一致性审计。"""
    before = {case.id: case for case in reference}
    after = {case.id: case for case in candidate}
    rows: list[dict[str, object]] = []
    for case_id in sorted(before.keys() | after.keys()):
        left, right = before.get(case_id), after.get(case_id)
        if left is None:
            rows.append({"case_id": case_id, "kind": "added"})
        elif right is None:
            rows.append({"case_id": case_id, "kind": "missing"})
        else:
            prompt_changed = left.prompt != right.prompt
            gold_changed = left.expected_skills != right.expected_skills
            if prompt_changed or gold_changed:
                rows.append(
                    {
                        "case_id": case_id,
                        "kind": (
                            "prompt+gold"
                            if prompt_changed and gold_changed
                            else "prompt"
                            if prompt_changed
                            else "gold"
                        ),
                        "reference_gold": left.expected_skills,
                        "candidate_gold": right.expected_skills,
                    }
                )
    return rows
