"""语义断言判定：case 的 `expect_assertions` → judge 模型逐条判 pass/fail + 证据。

结构参考 Anthropic skill-creator 的 `agents/grader.md`，产物字段名 `text`/`passed`/
`evidence` 与它逐字一致 —— 那边的 eval-viewer 依赖这三个确切名字，将来接它的 viewer
不用再做一层映射。

**judge 与被测模型完全解耦**（这是本模块存在的主要理由）：
  * 被测模型来自 suite 的 `models[]`，是实验对象；
  * judge 来自 suite 的 `scoring.judge`，是量具，默认读**独立的 `JUDGE_*` 环境变量**，
    可以是另一个 provider、另一个 key、另一个端点。
  * 同一批 runs.jsonl 可以换 judge 反复评：产物按 judge id 分文件，互不覆盖。

**判分不可复现，所以必须留痕。** 换 judge 就是换尺子，分数跟着变。scores.json 里
记 judge id/model/prompt_hash，compare_runs.py 把 judge 差异当污染项报（§5③）——
拿 qwen 判的分和 glm 判的分直接比 delta，是没有意义的。

用法：
  python -m workflows.grade --dir outputs/xxx                      # judge 从 suite 读
  python -m workflows.grade --dir outputs/xxx --judge-id glm5 --judge-model openai/glm-5.1
  python -m workflows.grade --dir outputs/xxx --dry-run            # 只打印将外发什么，不调用
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from workflows import dimensions as dims
from workflows.litellm_support import quiet_completion
from contracts import FullEvalTurn, RoutingCase, RunResult, SuiteJudgeSpec, load_cases

ROOT = Path(__file__).parent.parent
GRADER_VERSION = "p4-v0.3"

SYSTEM_PROMPT = """你是 Agent Skill 评测的判定器，负责两类判定：
1) 断言（assertions）：逐条判断是否成立，输出 passed 布尔值；
2) 维度（dimensions）：按给定的评分锚点打 0–1 连续分。

通用规则：
- 只返回 JSON；每一项都必须给出取自原文的证据，不得编造。
- 断言判定只有证据能证明**任务真的完成了**才算 passed；表面合规（文件名对但内容空、
  声称做了但看不到痕迹）一律判 failed。证据不足以判定时判 failed 并说明缺什么。
- 维度打分严格对照给出的评分锚点，可取锚点之间的中间值；不要凭印象给整数。
  证据不足以支撑高分时给低分并在 evidence 里说明缺什么。"""


class Expectation(BaseModel):
    """一条断言的判定结果。字段名与 skill-creator 的 grading.json 对齐，不要改。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    passed: bool
    evidence: str = Field(min_length=1)


class _JudgedBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expectations: list[Expectation] = Field(default_factory=list)
    dimensions: list[dims.DimensionScore] = Field(default_factory=list)


class RunGrading(BaseModel):
    """一次运行的判定结果。断言是二元的，维度是 0–1 连续分，两者分开记。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    repeat_index: int
    turn_index: int = 1
    expectations: list[Expectation]
    passed: int
    failed: int
    total: int
    pass_rate: float | None            # 该题没写断言 → None（N/A），不是 0
    dimensions: list[dims.DimensionScore] = Field(default_factory=list)
    # 这题因为缺参考答案等原因判不了的维度，明确记下来 —— 否则分母悄悄变小
    skipped_dimensions: list[str] = Field(default_factory=list)


class JudgeInfo(BaseModel):
    """这批分是谁判的、用的哪版 rubric。缺了它，两批分就分不清尺子换没换。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    model: str
    api_base_env: str
    api_key_env: str
    params: dict[str, Any] = Field(default_factory=dict)
    system_prompt_hash: str
    # 维度 id → rubric 版本。改了 rubric 却不 bump version，
    # 新旧分数会在同一个维度名下混着比（和 §5① 同一类错误）
    dimensions: dict[str, str] = Field(default_factory=dict)


class JudgeFailure(BaseModel):
    """A judge/API/parser failure kept separate from task or assertion failure."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    repeat_index: int
    turn_index: int = 1
    error: str


class GradingReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grader_version: str
    run_dir: str
    config_hash: str | None
    judge: JudgeInfo
    generated_at: str
    graded: list[RunGrading]
    judge_failures: list[JudgeFailure] = Field(default_factory=list)
    n_skipped_nothing_to_judge: int
    n_skipped_system_failure: int
    n_skipped_prior_turn: int = 0
    passed: int
    failed: int
    total: int
    pass_rate: float | None
    # 维度 id → 跨所有运行的平均分。某维度所有题都判不了 → 该键不出现（N/A）
    dimension_means: dict[str, float] = Field(default_factory=dict)


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _resolve(snapshot: dict, key: str) -> Path:
    path = Path(snapshot["suite"][key]).expanduser()
    return path if path.is_absolute() else ROOT / path


def resolve_judge(snapshot: dict, args: argparse.Namespace) -> SuiteJudgeSpec:
    """suite 的 judge 配置 + CLI 覆盖。CLI 优先，方便同一批 run 换尺子重评。

    只给 --judge-model 不给 --judge-id 会被拒：换了模型却沿用旧 id，产物会**覆盖**
    上一个 judge 的结果，而文件名看不出区别 —— 那是最难发现的一类数据损坏。
    """
    configured = (snapshot.get("suite", {}).get("scoring") or {}).get("judge")
    overrides = {
        k: v
        for k, v in {
            "id": args.judge_id,
            "model": args.judge_model,
            "api_base_env": args.judge_api_base_env,
            "api_key_env": args.judge_api_key_env,
            "dimensions": args.dimensions,
        }.items()
        if v
    }
    if not configured and not overrides:
        raise SystemExit(
            "这个 suite 没配 scoring.judge，也没给 --judge-model。\n"
            "  → 在 suite 里加 judge 一节，或用 --judge-id/--judge-model 临时指定。"
        )
    if overrides.get("model") and not overrides.get("id") and not configured:
        raise SystemExit("--judge-model 必须搭配 --judge-id：换了尺子就得换名字")
    if not configured and not overrides.get("model"):
        raise SystemExit(
            "只给了 judge 的部分参数，但没有 model。judge 是量具，必须指名道姓。\n"
            "  → 补 --judge-model，或在 suite 里写完整的 scoring.judge。"
        )
    if configured and overrides.get("model") and not overrides.get("id"):
        raise SystemExit(
            f"--judge-model 覆盖了 suite 里的 judge（id={configured.get('id')}），"
            "必须同时给 --judge-id，否则新结果会覆盖旧 judge 的产物"
        )
    return SuiteJudgeSpec.model_validate({**(configured or {}), **overrides})


def require_judge_credentials(judge: "SuiteJudgeSpec") -> None:
    """跑之前先确认量具接得上电，别外发到一半才发现 key 是空的。

    默认的 `JUDGE_*` 是**独立变量**，用意是别让人不知不觉拿被测模型判自己的卷子。
    但 .env.example 里给了它们、真实 .env 里往往没有 —— 照 README 跑 judge 必然失败，
    而报错只会是 provider 那边一句难懂的认证错误。
    """
    missing = [
        name for name in (judge.api_base_env, judge.api_key_env)
        if name and not os.environ.get(name)
    ]
    if not missing:
        return
    available = sorted(
        name for name in os.environ
        if name.endswith("_API_KEY") and os.environ.get(name)
    )
    raise SystemExit(
        f"judge（id={judge.id} model={judge.model}）的凭据环境变量是空的：{missing}\n"
        "  → 在 .env 里补上它们；或者把 suite 的 scoring.judge.api_base_env /\n"
        "    api_key_env 指到一组已有变量。judge 与被测模型**共用端点没关系，"
        "共用同一个模型才有关系**（考生不能改自己的卷子）——\n"
        "    所以指到同一个 provider、换一个 model 即可。\n"
        f"  当前 .env 里已配好的 key 变量：{available or '(一个都没有)'}"
    )


INPUT_FILE_LIMIT = 6000


def _input_files_section(case: RoutingCase | FullEvalTurn) -> str | None:
    """case 声明的输入素材原文（§11.4 的 `files`）。

    **不给它，"有没有编造"这类断言就是在瞎判。** 实测过一次：题面给了一份会议文字稿，
    模型把稿子里真实出现的责任人写进了纪要，judge 因为看不到文字稿、只能拿产物里的
    引文反推，判定这个人名是"完全不存在的实体"——一次彻底的假失败。假失败喂给
    改进环节，就会照着一个不存在的问题去改 skill。
    """
    if not case.files:
        return None
    blocks = []
    for raw in case.files:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            blocks.append(f"--- {path.name} ---\n(读不出来或是二进制，内容不可见)")
            continue
        if len(text) > INPUT_FILE_LIMIT:
            text = text[:INPUT_FILE_LIMIT] + f"\n…[truncated at {INPUT_FILE_LIMIT} chars]"
        blocks.append(f"--- {path.name} ---\n{text}")
    return ("[本题提供给模型的输入文件——判定「有没有编造」「有没有漏」以这里为准]\n"
            + "\n\n".join(blocks))


def build_grading_prompt(
    case: RoutingCase,
    run: RunResult,
    active_dims: list[dims.Dimension] | None = None,
    history: list[dict[str, str]] | None = None,
) -> str:
    """judge 看到的全部输入。断言与维度**合并成一次调用**（§24 只有必要结果进 Judge）。

    产物带**文本内容前缀**（`Artifact.text_excerpt`，截断到 `TEXT_EXCERPT_LIMIT`）——
    workspace 跑完即删，采集时不留一份，"报告里的数字是不是编的"这类断言就只能判
    "证据不足"。二进制产物（docx/png/…）仍然只有元数据，这条边界照旧写进 prompt：
    judge 在证据不足时必须判 failed / 给低分，不许猜一个高分。
    """
    active_dims = active_dims or []
    artifacts = [
        {
            "path": a.path,
            "size_bytes": a.size_bytes,
            "mime_type": a.mime_type,
            "sha256": a.sha256[:16],
            "content": a.text_excerpt if a.text_excerpt is not None else "(二进制或未采集，内容不可见)",
        }
        for a in run.artifacts
    ]
    tools = [{"name": t.name, "count": t.count, "failures": t.failures} for t in run.tool_calls]
    output = run.final_answer or run.raw_output or run.error or ""

    turn = case.resolved_turn(run.turn_index)
    parts: list[str] = []
    if history:
        parts.append(
            "[此前对话——用于判断本轮是否保持上下文]\n"
            + json.dumps(history, ensure_ascii=False, indent=2)
        )
    parts.append(f"[本轮用户请求 · turn {run.turn_index}]\n{turn.prompt}")
    inputs = _input_files_section(turn)
    if inputs:
        parts.append(inputs)
    if turn.reference:
        parts.append(f"[参考答案——只用于 correctness 维度]\n{turn.reference}")
    if turn.expect_assertions:
        parts.append("[待判定的断言]\n"
                     + json.dumps(turn.expect_assertions, ensure_ascii=False, indent=2))
    if active_dims:
        parts.append("[待打分的维度]\n" + dims.render(active_dims))
    parts += [
        f"[本次运行的模型输出]\n{output}",
        "[本次运行产生的文件——content 是文本产物的内容前缀，可能被截断；"
        "二进制产物只有元数据]\n"
        + json.dumps(artifacts, ensure_ascii=False, indent=2),
        "[本次运行调用的 tool]\n" + json.dumps(tools, ensure_ascii=False, indent=2),
    ]

    rules = ['- 输出对象格式：{"expectations":[...], "dimensions":[...]}']
    if turn.expect_assertions:
        rules += [
            "- expectations 每项字段只有 text,passed,evidence",
            "- text 必须与「待判定的断言」逐字相同，顺序一致，不得增删",
        ]
    else:
        rules.append("- 本题没有断言，expectations 返回空数组 []")
    if active_dims:
        rules += [
            "- dimensions 每项字段只有 dimension,score,evidence",
            f"- dimension 必须恰好是这几个且顺序一致："
            f"{[d.id for d in active_dims]}，不得增删",
            "- score 是 0 到 1 之间的小数，严格对照该维度的评分锚点，可取中间值",
        ]
    else:
        rules.append("- 本题没有维度，dimensions 返回空数组 []")
    rules += [
        "- evidence 必须逐字摘自上面的模型输出或文件清单（含产物 content）；"
        "给低分/判 failed 时说明缺什么证据",
        "- 产物 content 是可信的产物原文，判定优先看它，不要因为它来自文件就打折扣",
        "- content 标注了 truncated 或写着「内容不可见」时，凡是需要读到那部分内容才能"
        "确认的，一律判 failed / 给低分并注明「产物内容不可见」",
    ]
    parts.append("[输出约束]\n" + "\n".join(rules))
    return "\n\n".join(parts)


def call_litellm(
    *,
    model: str,
    api_base_env: str,
    api_key_env: str,
    params: dict[str, Any],
    prompt: str,
) -> str:
    import litellm

    response = quiet_completion(
        litellm,
        model=model,
        api_base=os.environ.get(api_base_env) or None,
        api_key=os.environ.get(api_key_env) or None,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        timeout=300,
        **params,
    )
    return response.choices[0].message.content or ""


def _extract_json(text: str) -> str:
    value = text.strip().strip("`")
    if value.lower().startswith("json"):
        value = value[4:].lstrip()
    left, right = value.find("{"), value.rfind("}")
    if left == -1 or right == -1:
        raise ValueError("judge 输出里没有 JSON object")
    return value[left : right + 1]


def validate_expectations(
    judged: list[Expectation],
    asserted: list[str],
    *,
    case_id: str,
) -> list[Expectation]:
    """judge 必须逐条判、不漏不加不改写。

    不做这层校验，judge 少判一条就会**静默抬高**通过率 —— 分母缩小了没人看得见。
    这正是 HANDOFF §6 里「静默变空」那一整类坑的变体。
    """
    got, want = [e.text for e in judged], list(asserted)
    if got != want:
        raise ValueError(
            f"{case_id}: judge 返回的断言与输入不一致（必须逐字同序）\n"
            f"  输入：{want}\n  返回：{got}"
        )
    return judged


def validate_dimensions(
    judged: list[dims.DimensionScore],
    expected: list[dims.Dimension],
    *,
    case_id: str,
) -> list[dims.DimensionScore]:
    """judge 必须把要求的维度全打分、不漏不加。

    漏掉一个维度会让该维度的分母**静默变小** —— 报表上看起来只是这题没测，
    实际是平均分被悄悄改写了（HANDOFF §6「静默变空」那一类）。
    """
    got, want = [d.dimension for d in judged], [d.id for d in expected]
    if got != want:
        raise ValueError(
            f"{case_id}: judge 返回的维度与请求不一致（必须同序不增删）\n"
            f"  请求：{want}\n  返回：{got}"
        )
    return judged


def grade_run(
    case: RoutingCase,
    run: RunResult,
    *,
    judge: SuiteJudgeSpec,
    active_dims: list[dims.Dimension] | None = None,
    completion: Callable[..., str] = call_litellm,
    history: list[dict[str, str]] | None = None,
) -> RunGrading:
    """判一次运行。断言与维度合并成一次模型调用。"""
    # 这题够不够条件判每个维度（比如 correctness 需要参考答案）。
    # 判不了的记进 skipped_dimensions，不送给 judge，也不按 0 分算。
    turn = case.resolved_turn(run.turn_index)
    wanted = active_dims or []
    applicable = [
        d for d in wanted
        if dims.applicable(d, has_reference=bool(turn.reference),
                           has_artifacts=bool(run.artifacts))
    ]
    skipped = [d.id for d in wanted if d not in applicable]

    raw = completion(
        model=judge.model,
        api_base_env=judge.api_base_env,
        api_key_env=judge.api_key_env,
        params=judge.params,
        prompt=build_grading_prompt(case, run, applicable, history),
    )
    batch = _JudgedBatch.model_validate_json(_extract_json(raw))
    expectations = validate_expectations(
        batch.expectations,
        turn.expect_assertions,
        case_id=f"{case.id}.t{run.turn_index}",
    )
    scored = validate_dimensions(batch.dimensions, applicable, case_id=case.id)

    passed = sum(e.passed for e in expectations)
    total = len(expectations)
    return RunGrading(
        case_id=case.id,
        repeat_index=run.repeat_index,
        turn_index=run.turn_index,
        expectations=expectations,
        passed=passed,
        failed=total - passed,
        total=total,
        # 没写断言的题记 N/A，不是 0 分 —— 0 分的意思是「判了，全没过」
        pass_rate=round(passed / total, 4) if total else None,
        dimensions=scored,
        skipped_dimensions=skipped,
    )


# 系统故障不送去判：模型根本没跑，判它「没完成任务」是把评测系统的问题
# 记到 skill 头上（与 score_full.py 的 _SYSTEM_FAILURES 同一条原则）。
_SYSTEM_FAILURES = ("runtime", "network", "harness")


def grade_run_dir(
    run_dir: Path,
    *,
    judge: SuiteJudgeSpec,
    completion: Callable[..., str] = call_litellm,
    progress: Callable[[int, int, str, str, int, str | None], None] | None = None,
) -> GradingReport:
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    cases = {c.id: c for c in load_cases(_resolve(snapshot, "dataset"))}
    active_dims = dims.resolve(judge.dimensions)

    graded: list[RunGrading] = []
    judge_failures: list[JudgeFailure] = []
    skipped_nothing = skipped_system = skipped_prior = 0
    candidates: list[tuple[RoutingCase, RunResult, list[dict[str, str]]]] = []
    histories: dict[tuple[str, int], list[dict[str, str]]] = {}
    for line in (run_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        run = RunResult.model_validate_json(line)
        case = cases[run.case_id]
        turn = case.resolved_turn(run.turn_index)
        key = (run.case_id, run.repeat_index)
        history = list(histories.get(key, []))
        if run.status == "skipped":
            skipped_prior += 1
            continue
        # 既没断言又没维度可判 → 这次运行没有任何东西要送给 judge，省一次调用
        if not turn.expect_assertions and not active_dims:
            skipped_nothing += 1
        elif run.error_kind in _SYSTEM_FAILURES:
            skipped_system += 1
        else:
            candidates.append((case, run, history))
        histories.setdefault(key, []).extend([
            {"role": "user", "content": turn.prompt},
            {
                "role": "assistant",
                "content": run.final_answer or run.raw_output or run.error or "",
            },
        ])

    for index, (case, run, history) in enumerate(candidates, start=1):
        if progress:
            progress(index - 1, len(candidates), "running", case.id, run.repeat_index, None)
        try:
            graded.append(grade_run(case, run, judge=judge,
                                    active_dims=active_dims, completion=completion,
                                    history=history))
        except Exception as error:  # noqa: BLE001 -- must preserve the rest of a batch
            judge_failures.append(JudgeFailure(
                case_id=case.id, repeat_index=run.repeat_index,
                turn_index=run.turn_index, error=repr(error)
            ))
            if progress:
                progress(index, len(candidates), "failed", case.id, run.repeat_index, repr(error))
        else:
            if progress:
                progress(index, len(candidates), "ok", case.id, run.repeat_index, None)

    passed = sum(g.passed for g in graded)
    total = sum(g.total for g in graded)

    # 维度均值：只对**真的判了**的运行取平均。判不了的题不进分母，
    # 所以「10 题里只有 3 题有参考答案」不会把 correctness 拉低。
    by_dim: dict[str, list[float]] = {}
    for g in graded:
        for score in g.dimensions:
            by_dim.setdefault(score.dimension, []).append(score.score)

    return GradingReport(
        grader_version=GRADER_VERSION,
        run_dir=run_dir.name,
        config_hash=snapshot.get("config_hash"),
        judge=JudgeInfo(
            id=judge.id,
            model=judge.model,
            api_base_env=judge.api_base_env,
            api_key_env=judge.api_key_env,
            params=judge.params,
            system_prompt_hash=_sha(SYSTEM_PROMPT),
            dimensions=dims.fingerprint(active_dims),
        ),
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        graded=graded,
        judge_failures=judge_failures,
        n_skipped_nothing_to_judge=skipped_nothing,
        n_skipped_system_failure=skipped_system,
        n_skipped_prior_turn=skipped_prior,
        passed=passed,
        failed=total - passed,
        total=total,
        pass_rate=round(passed / total, 4) if total else None,
        dimension_means={k: round(sum(v) / len(v), 4) for k, v in sorted(by_dim.items())},
    )


def grading_path(run_dir: Path, judge_id: str) -> Path:
    """按 judge id 分文件：换 judge 重评不会覆盖上一次的结果。"""
    return run_dir / f"grading.{judge_id}.json"


def grading_partial_path(run_dir: Path, judge_id: str) -> Path:
    """A recoverable progress marker; never mistaken for a finished grading report."""
    return run_dir / f"grading.{judge_id}.partial.json"


def load_grading(run_dir: Path, snapshot: dict, judge_id: str | None) -> dict | None:
    """给两个 score_*.py 读判定产物用。判不了就返回 None（N/A），**绝不退化成 0**。

    judge_id 的解析顺序：CLI > suite 的 scoring.judge.id。
    目录里躺着多个 judge 的结果却没指定用哪个时**直接报错，不随手挑一个** ——
    否则「这批分是谁判的」就成了 glob 顺序的函数，正是 §5① 要防的事。
    """
    scoring = (snapshot.get("suite", {}) or {}).get("scoring") or {}
    wanted = judge_id or (scoring.get("judge") or {}).get("id")
    available = sorted(p.name.split(".")[1] for p in run_dir.glob("grading.*.json"))
    if not wanted:
        if not available:
            return None
        raise SystemExit(
            f"{run_dir.name} 下有 {len(available)} 份判定结果 {available}，但 suite 没配 "
            f"scoring.judge —— 用 --judge-id 明确指定用哪把尺子的分"
        )
    path = grading_path(run_dir, wanted)
    if not path.exists():
        if available:
            print(f"  ⚠️ 没有 grading.{wanted}.json；目录里有的是 {available}"
                  f"（assertion 维度记 N/A，不按 0 处理）")
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="用 judge 模型判定 case 的 expect_assertions；judge 与被测模型解耦"
    )
    # 不用 required=True：--list-dimensions 是纯查询，不该逼人先编一个 --dir
    parser.add_argument("--dir", help="run 目录（除 --list-dimensions 外必填）")
    parser.add_argument("--judge-id", help="覆盖 suite 的 judge.id（换模型必须换 id）")
    parser.add_argument("--judge-model", help="覆盖 suite 的 judge.model")
    parser.add_argument("--judge-api-base-env", help="覆盖 judge 的 base url 环境变量名")
    parser.add_argument("--judge-api-key-env", help="覆盖 judge 的 api key 环境变量名")
    parser.add_argument("--dimensions", nargs="+", metavar="DIM",
                        help=f"覆盖 suite 的评估维度。可用：{sorted(dims.STANDARD_DIMENSIONS)}")
    parser.add_argument("--list-dimensions", action="store_true",
                        help="打印所有内置维度的定义与评分锚点后退出")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要外发的内容与 judge 配置，不调用模型")
    parser.add_argument("--force", action="store_true", help="允许覆盖同 judge id 的既有结果")
    args = parser.parse_args()

    if args.list_dimensions:
        for d in dims.STANDARD_DIMENSIONS.values():
            print(f"\n{d.id}  ({d.label})  [{d.version}]")
            print(f"  判定：{d.question}")
            print(f"  需要：{', '.join(d.requires)}")
            print(f"  来源：{d.source}")
            for score, desc in sorted(d.rubric.items(), reverse=True):
                print(f"    {score} = {desc}")
        print(f"\n默认启用：{list(dims.DEFAULT_DIMENSIONS)}")
        return

    if not args.dir:
        parser.error("--dir 必填（除非用 --list-dimensions）")

    load_dotenv(ROOT / ".env")
    run_dir = Path(args.dir).resolve()
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))

    # 「这个 run 根本不适用 judge」必须排在「judge 没配好」前面。
    # 反过来的话，用户先被要求去配 judge，照做之后才被告知路由 run 压根不能判 ——
    # 第一条消息把人往错方向支了一步（AGENTS.md §29.28）。
    if snapshot.get("suite", {}).get("skills", {}).get("mode") == "routing_only":
        raise SystemExit(
            "这是一次 routing-only 运行，输出只有 skill 选择和路由理由，"
            "不是对用户任务的最终回答；通用语义维度（relevancy / completeness 等）"
            "在这里没有有效语义 —— 配不配 judge 都一样。\n"
            "  → 路由正确性请运行 score_routing.py 的确定性指标（Top-1 / 拒答率 /"
            "误激活 / 混淆矩阵）；语义 Judge 请用于 skills.mode: full 的 run，"
            "或等待专门的 routing-reasoning rubric。"
        )

    judge = resolve_judge(snapshot, args)
    require_judge_credentials(judge)
    active_dims = dims.resolve(judge.dimensions)

    print(f"run dir: {run_dir.name}")
    print(f"judge:   id={judge.id} model={judge.model} "
          f"base={judge.api_base_env} key={judge.api_key_env} params={judge.params}")
    print(f"维度:    {[d.id for d in active_dims] or '（未启用，只判 assertions）'}")

    cases = {c.id: c for c in load_cases(_resolve(snapshot, "dataset"))}
    n_assert_cases = sum(1 for c in cases.values() if c.expect_assertions)
    if not n_assert_cases and not active_dims:
        raise SystemExit(
            "没有任何可判定的东西：数据集里没有题写 expect_assertions，suite 也没启用维度。\n"
            f"  → 给题补 expect_assertions（{snapshot['suite']['dataset']}），\n"
            f"     或用 --dimensions {' '.join(dims.DEFAULT_DIMENSIONS)} 启用标准维度\n"
            "     （维度是通用的，不需要逐题写东西）"
        )

    if args.dry_run:
        runs = [RunResult.model_validate_json(l)
                for l in (run_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()
                if l.strip()]
        target = [r for r in runs
                  if (cases[r.case_id].expect_assertions or active_dims)
                  and r.error_kind not in _SYSTEM_FAILURES]
        print(f"\n将外发 {len(target)} 次运行 × judge 各一次调用。"
              f"（{n_assert_cases}/{len(cases)} 道题有断言；"
              f"{len(active_dims)} 个维度对所有题生效）")
        if target:
            case = cases[target[0].case_id]
            usable = [d for d in active_dims
                      if dims.applicable(d, has_reference=bool(case.reference),
                                         has_artifacts=bool(target[0].artifacts))]
            sample = build_grading_prompt(case, target[0], usable)
            print(f"\n--- 第一条 prompt 预览（system prompt hash {_sha(SYSTEM_PROMPT)}）---")
            print(sample[:2000] + ("\n…（截断）" if len(sample) > 2000 else ""))
        print("\n--dry-run：没有调用任何模型，没有写任何文件。")
        return

    out = grading_path(run_dir, judge.id)
    if out.exists() and not args.force:
        raise SystemExit(f"已存在 {out.name}；换个 --judge-id 或加 --force 覆盖")

    partial = grading_partial_path(run_dir, judge.id)

    def progress(index: int, total: int, status: str, case_id: str,
                 repeat_index: int, error: str | None) -> None:
        payload = {
            "judge_id": judge.id,
            "completed": index,
            "total": total,
            "status": status,
            "case_id": case_id,
            "repeat_index": repeat_index,
            "error": error,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        suffix = f" — {error}" if error else ""
        print(f"[{index}/{total}] {case_id} r{repeat_index}: {status}{suffix}", flush=True)

    report = grade_run_dir(run_dir, judge=judge, progress=progress)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if partial.exists():
        partial.unlink()

    print(f"\n判定 {len(report.graded)} 次运行")
    if report.n_skipped_nothing_to_judge:
        print(f"  跳过 {report.n_skipped_nothing_to_judge} 次（这题没断言、也没启用维度）")
    if report.n_skipped_system_failure:
        print(f"  跳过 {report.n_skipped_system_failure} 次（系统故障，不算 skill 头上）")
    if report.judge_failures:
        print(f"  ⚠️ judge 失败 {len(report.judge_failures)} 次；已记录在结果中，"
              "其余运行继续完成（judge 失败不计入 task/gate）")
    if report.total:
        print(f"\n  assertion_pass_rate  {report.pass_rate:.1%}  （{report.total} 条断言）")
    if report.dimension_means:
        print("\n维度分（0–1，越高越好）：")
        for name, mean in report.dimension_means.items():
            label = dims.STANDARD_DIMENSIONS[name].label
            n = sum(1 for g in report.graded for s in g.dimensions if s.dimension == name)
            print(f"  {name:<22} {mean:.2f}   {label}（{n} 次运行）")
        skipped = sorted({s for g in report.graded for s in g.skipped_dimensions})
        if skipped:
            print(f"  ⚠️ 有题判不了这些维度（缺参考答案等），已排除出分母：{skipped}")
    print(f"\n{out}")
    print("→ 跑 score_full.py / score_routing.py 把这些分并进报表")


if __name__ == "__main__":
    main()
