"""对不可变历史 run 换尺子评分，不重新执行 Agent。

rescore 只读取 ``runs.jsonl``、snapshot、归档 dataset 和其中已有的 artifact 证据。
输出按 grading_id 版本化；旧 grading/scores/report 永不覆盖。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import yaml

from contracts import SuiteJudgeSpec
from evaluators.efficiency import EVALUATOR_VERSION as EFFICIENCY_VERSION
from evaluators.outcome import EVALUATOR_VERSION as OUTCOME_VERSION
from evaluators.reliability import EVALUATOR_VERSION as RELIABILITY_VERSION
from evaluators.trajectory import EVALUATOR_VERSION as TRAJECTORY_VERSION
from workflows import dimensions as dims
from workflows.grade import (
    GRADER_VERSION,
    SYSTEM_PROMPT as OUTPUT_SYSTEM_PROMPT,
    grade_run_dir,
    resolve_judge,
    resolve_run_dataset,
)
from workflows.grade_trajectory import (
    GRADER_VERSION as TRAJECTORY_GRADER_VERSION,
    SYSTEM_PROMPT as TRAJECTORY_SYSTEM_PROMPT,
    grade_run_dir as grade_trajectory_run_dir,
)
from workflows.score_full import SCORE_VERSION as FULL_SCORE_VERSION
from workflows.score_routing import SCORE_VERSION as ROUTING_SCORE_VERSION

RESCORE_VERSION = "rescore-v1"
STAGE_ORDER = ("grade", "trajectory", "score")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_EVALUATOR_VERSIONS = {
    "outcome": OUTCOME_VERSION,
    "trajectory": TRAJECTORY_VERSION,
    "reliability": RELIABILITY_VERSION,
    "efficiency": EFFICIENCY_VERSION,
}
ROOT = Path(__file__).parent.parent


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _sha_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha_text(payload)


def parse_stages(raw: str) -> tuple[str, ...]:
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = requested - set(STAGE_ORDER)
    if unknown:
        raise ValueError(f"未知 rescore stage：{sorted(unknown)}；可用 {list(STAGE_ORDER)}")
    if not requested:
        raise ValueError("rescore stages 不能为空")
    return tuple(stage for stage in STAGE_ORDER if stage in requested)


@dataclass(frozen=True)
class RescorePaths:
    grading: Path | None
    trajectory_grading: Path | None
    scores: Path | None
    report: Path | None
    trajectory: Path | None

    def existing_or_partial(self) -> list[Path]:
        found: list[Path] = []
        for path in self.__dict__.values():
            if path is None:
                continue
            partial = path.with_name(path.name + ".partial")
            if path.exists():
                found.append(path)
            if partial.exists():
                found.append(partial)
        return found

    def as_dict(self) -> dict[str, str | None]:
        return {key: str(value) if value is not None else None
                for key, value in self.__dict__.items()}


@dataclass(frozen=True)
class RescorePlan:
    run_dir: Path
    grading_id: str
    stages: tuple[str, ...]
    skill_mode: str
    judge: SuiteJudgeSpec | None
    paths: RescorePaths
    provenance: dict[str, Any]
    warnings: tuple[str, ...]
    blocked_reasons: tuple[str, ...]

    @property
    def runnable(self) -> bool:
        return not self.blocked_reasons

    @property
    def egress_required(self) -> bool:
        return bool(set(self.stages) & {"grade", "trajectory"})

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_dir": str(self.run_dir),
            "grading_id": self.grading_id,
            "stages": list(self.stages),
            "skill_mode": self.skill_mode,
            "judge": self.judge.model_dump(mode="json") if self.judge else None,
            "paths": self.paths.as_dict(),
            "provenance": self.provenance,
            "egress_required": self.egress_required,
            "warnings": list(self.warnings),
            "blocked_reasons": list(self.blocked_reasons),
            "runnable": self.runnable,
        }


def _resolve_rescore_judge(
    snapshot: dict[str, Any],
    *,
    judge_id: str | None,
    judge_model: str | None,
    judge_api_base_env: str | None,
    judge_api_key_env: str | None,
    dimensions: list[str] | None,
    judge_params: dict[str, Any] | None,
) -> SuiteJudgeSpec:
    args = SimpleNamespace(
        judge_id=judge_id,
        judge_model=judge_model,
        judge_api_base_env=judge_api_base_env,
        judge_api_key_env=judge_api_key_env,
        dimensions=dimensions,
    )
    judge = resolve_judge(snapshot, args)
    if judge_params is not None:
        judge = SuiteJudgeSpec.model_validate({
            **judge.model_dump(mode="python"),
            "params": judge_params,
        })
    return judge


def build_rescore_plan(
    run_dir: str | Path,
    *,
    stages: tuple[str, ...],
    grading_id: str,
    judge_id: str | None = None,
    judge_model: str | None = None,
    judge_api_base_env: str | None = None,
    judge_api_key_env: str | None = None,
    dimensions: list[str] | None = None,
    judge_params: dict[str, Any] | None = None,
) -> RescorePlan:
    directory = Path(run_dir).expanduser().resolve()
    if not _SAFE_ID.fullmatch(grading_id):
        raise ValueError("grading_id 只能包含字母、数字、点、下划线和连字符")
    unknown = set(stages) - set(STAGE_ORDER)
    if unknown or not stages:
        raise ValueError(f"非法 rescore stages：{stages}")
    stages = tuple(stage for stage in STAGE_ORDER if stage in stages)

    blocked: list[str] = []
    warnings: list[str] = []
    snapshot_path = directory / "config.snapshot.yaml"
    runs_path = directory / "runs.jsonl"
    if not snapshot_path.is_file():
        blocked.append(f"缺少 {snapshot_path}")
    if not runs_path.is_file():
        blocked.append(f"缺少 {runs_path}")
    if blocked:
        empty_paths = RescorePaths(None, None, None, None, None)
        return RescorePlan(directory, grading_id, stages, "unknown", None, empty_paths,
                           {}, tuple(warnings), tuple(blocked))

    snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8")) or {}
    suite = snapshot.get("suite") or {}
    skill_mode = (suite.get("skills") or {}).get("mode", "routing_only")
    dataset_path = resolve_run_dataset(directory, snapshot)
    if not dataset_path.is_file():
        blocked.append(f"历史 run 的 dataset 不可用：{dataset_path}")
    elif dataset_path != directory / "inputs" / "dataset.jsonl":
        warnings.append("此历史 run 没有归档 inputs/dataset.jsonl；将读取 snapshot 指向的现存题库")

    judge: SuiteJudgeSpec | None = None
    if set(stages) & {"grade", "trajectory"}:
        if skill_mode == "routing_only" and "grade" in stages:
            blocked.append("routing-only run 不适用输出语义 grade；只运行 score")
        trajectory = ((suite.get("scoring") or {}).get("trajectory") or {})
        if "trajectory" in stages and not trajectory.get("enabled"):
            blocked.append("suite 没有启用 scoring.trajectory，不能运行 trajectory judge")
        try:
            judge = _resolve_rescore_judge(
                snapshot,
                judge_id=judge_id,
                judge_model=judge_model,
                judge_api_base_env=judge_api_base_env,
                judge_api_key_env=judge_api_key_env,
                dimensions=dimensions,
                judge_params=judge_params,
            )
        except (SystemExit, ValueError) as error:
            blocked.append(str(error))
        if judge:
            missing = [name for name in (judge.api_base_env, judge.api_key_env)
                       if name and not os.environ.get(name)]
            if missing:
                blocked.append(f"judge 凭据环境变量为空：{missing}")
            tested_model = (snapshot.get("resolved_model") or {}).get("model")
            if tested_model and tested_model == judge.model:
                warnings.append("执行模型与 judge 模型相同；这是自己评自己，结果只应诊断使用")

    bucket = judge.id if judge else "deterministic"
    paths = RescorePaths(
        grading=(directory / "grading" / bucket / f"{grading_id}.json")
        if "grade" in stages else None,
        trajectory_grading=(directory / "grading" / bucket / f"{grading_id}.trajectory.json")
        if "trajectory" in stages else None,
        scores=(directory / "scores" / f"{grading_id}.json") if "score" in stages else None,
        report=(directory / "reports" / f"{grading_id}.html") if "score" in stages else None,
        trajectory=(directory / "scores" / f"{grading_id}.trajectory.jsonl")
        if "score" in stages else None,
    )
    collisions = paths.existing_or_partial()
    if collisions:
        blocked.append("版本化评分产物已存在，拒绝覆盖：" + ", ".join(map(str, collisions)))

    scoring = suite.get("scoring") or {}
    evaluator_names = scoring.get("evaluators") or list(_EVALUATOR_VERSIONS)
    evaluator_versions = {
        name: _EVALUATOR_VERSIONS.get(name, "unversioned") for name in evaluator_names
    }
    gauge = {
        "rescore_version": RESCORE_VERSION,
        "stages": list(stages),
        "judge": judge.model_dump(mode="json") if judge else None,
        "output_grader": {
            "version": GRADER_VERSION,
            "system_prompt_hash": _sha_text(OUTPUT_SYSTEM_PROMPT),
            "dimensions": dims.fingerprint(dims.resolve(judge.dimensions)) if judge else {},
        } if "grade" in stages else None,
        "trajectory_grader": {
            "version": TRAJECTORY_GRADER_VERSION,
            "system_prompt_hash": _sha_text(TRAJECTORY_SYSTEM_PROMPT),
            "config": scoring.get("trajectory") or {},
        } if "trajectory" in stages else None,
        "score_version": (
            FULL_SCORE_VERSION if skill_mode == "full" else ROUTING_SCORE_VERSION
        ) if "score" in stages else None,
        "evaluators": evaluator_versions,
        "gate": scoring.get("gate") or {},
    }
    source = {
        "source_run_dir": str(directory),
        "source_runs_sha256": sha256_file(runs_path),
        "source_snapshot_sha256": sha256_file(snapshot_path),
        "source_dataset_sha256": sha256_file(dataset_path) if dataset_path.is_file() else None,
        "snapshot_dataset_hash": snapshot.get("dataset_hash"),
        "source_config_hash": snapshot.get("config_hash"),
    }
    provenance = {
        **source,
        "grading_hash": _canonical_hash({"source": source, "gauge": gauge}),
        "gauge": gauge,
    }
    return RescorePlan(
        directory, grading_id, stages, skill_mode, judge, paths, provenance,
        tuple(warnings), tuple(blocked),
    )


def render_rescore_plan(plan: RescorePlan) -> str:
    lines = [
        "RESCORE PLAN — historical facts are read-only",
        f"source run: {plan.run_dir}",
        f"source runs sha256: {plan.provenance.get('source_runs_sha256', 'N/A')}",
        f"grading id/hash: {plan.grading_id} / {plan.provenance.get('grading_hash', 'N/A')}",
        f"stages: {', '.join(plan.stages)}",
        f"judge: {plan.judge.id + ' / ' + plan.judge.model if plan.judge else 'none (deterministic only)'}",
        "outputs:",
    ]
    lines.extend(f"  {name}: {path}" for name, path in plan.paths.as_dict().items() if path)
    lines.append(
        "external data movement: user prompts/model outputs/artifact excerpts/trajectory → judge"
        if plan.egress_required else
        "external data movement: none — deterministic scoring only"
    )
    lines.extend(f"WARNING: {warning}" for warning in plan.warnings)
    lines.extend(f"BLOCKED: {reason}" for reason in plan.blocked_reasons)
    lines.append("RUNNABLE" if plan.runnable else "NOT RUNNABLE")
    if plan.runnable:
        lines.append("No Agent runtime will be called and runs.jsonl will not be modified.")
    return "\n".join(lines)


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _promote_exclusive(partial: Path, final: Path) -> None:
    """把已完成的 partial 以 exclusive-create 落盘，绝不 replace 旧结果。"""
    final.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("rb") as source, final.open("xb") as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            target.write(chunk)
    partial.unlink()


def _default_command_runner(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def execute_rescore(
    plan: RescorePlan,
    *,
    completion: Callable[..., str] | None = None,
    trajectory_completion: Callable[..., str] | None = None,
    command_runner: Callable[[list[str]], None] = _default_command_runner,
) -> dict[str, str]:
    if not plan.runnable:
        raise ValueError("rescore plan 被阻断：" + "; ".join(plan.blocked_reasons))
    before = {
        "runs": sha256_file(plan.run_dir / "runs.jsonl"),
        "snapshot": sha256_file(plan.run_dir / "config.snapshot.yaml"),
        "dataset": plan.provenance["source_dataset_sha256"],
    }
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    provenance = {
        "rescore_version": RESCORE_VERSION,
        "grading_id": plan.grading_id,
        **plan.provenance,
        "generated_at": generated_at,
    }

    if "grade" in plan.stages:
        assert plan.paths.grading is not None and plan.judge is not None
        kwargs: dict[str, Any] = {"judge": plan.judge}
        if completion is not None:
            kwargs["completion"] = completion
        report = grade_run_dir(plan.run_dir, **kwargs).model_dump(mode="json")
        report["rescore"] = provenance
        _write_json_exclusive(plan.paths.grading, report)

    if "trajectory" in plan.stages:
        assert plan.paths.trajectory_grading is not None and plan.judge is not None
        kwargs = {"judge": plan.judge}
        if trajectory_completion is not None:
            kwargs["completion"] = trajectory_completion
        report = grade_trajectory_run_dir(plan.run_dir, **kwargs)
        report["rescore"] = provenance
        _write_json_exclusive(plan.paths.trajectory_grading, report)

    if "score" in plan.stages:
        assert plan.paths.scores and plan.paths.report and plan.paths.trajectory
        score_partial = plan.paths.scores.with_name(plan.paths.scores.name + ".partial")
        report_partial = plan.paths.report.with_name(plan.paths.report.name + ".partial")
        trajectory_partial = plan.paths.trajectory.with_name(plan.paths.trajectory.name + ".partial")
        scorer = "workflows.score_full" if plan.skill_mode == "full" else "workflows.score_routing"
        command = [
            sys.executable, "-m", scorer,
            "--dir", str(plan.run_dir),
            "--scores-output", str(score_partial),
            "--html", str(report_partial),
            "--trajectory-output", str(trajectory_partial),
        ]
        if plan.paths.grading:
            command += ["--grading-file", str(plan.paths.grading)]
        else:
            command.append("--no-grading")
        if plan.paths.trajectory_grading:
            command += ["--trajectory-grading-file", str(plan.paths.trajectory_grading)]
        else:
            command.append("--no-trajectory-grading")
        command_runner(command)
        if not score_partial.is_file() or not report_partial.is_file():
            raise RuntimeError("deterministic scorer 未生成预期的 scores/report 产物")
        scores = json.loads(score_partial.read_text(encoding="utf-8"))
        scores["rescore"] = provenance
        score_partial.write_text(json.dumps(scores, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        report_html = report_partial.read_text(encoding="utf-8")
        report_partial.write_text(
            "<div style='border:2px solid #2563eb;padding:10px'>"
            f"<b>RESCORE {plan.grading_id}</b> — same execution facts; grading hash "
            f"<code>{provenance['grading_hash']}</code>; source runs "
            f"<code>{provenance['source_runs_sha256']}</code>.</div>\n" + report_html,
            encoding="utf-8",
        )
        _promote_exclusive(score_partial, plan.paths.scores)
        _promote_exclusive(report_partial, plan.paths.report)
        if trajectory_partial.exists():
            _promote_exclusive(trajectory_partial, plan.paths.trajectory)

    dataset_path = resolve_run_dataset(
        plan.run_dir,
        yaml.safe_load((plan.run_dir / "config.snapshot.yaml").read_text(encoding="utf-8")),
    )
    after = {
        "runs": sha256_file(plan.run_dir / "runs.jsonl"),
        "snapshot": sha256_file(plan.run_dir / "config.snapshot.yaml"),
        "dataset": sha256_file(dataset_path),
    }
    if after != before:
        raise RuntimeError(f"rescore 修改了历史事实文件，拒绝完成：before={before} after={after}")
    return {name: path for name, path in plan.paths.as_dict().items() if path}
