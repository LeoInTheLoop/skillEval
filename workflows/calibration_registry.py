"""Judge calibration registry and release-gate qualification.

校准报告是证据，registry 是允许哪把量具进入哪类 gate 的显式清单。匹配使用模型、
endpoint env、参数、system prompt hash 和 rubric versions；judge id 只是标签，不参与。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from workflows import dimensions

ROOT = Path(__file__).parent.parent
Scope = Literal["absolute_assertions", "standard_dimension", "trajectory_dimension"]
_TRAJECTORY_METRICS = {
    "tool_selection", "argument_correctness", "order_correctness",
    "state_persistence", "verification_rate",
}
_AGREEMENT_MIN = 0.80
_INVALID_MAX = 0.02
_DIMENSION_TOLERANCE_MAX = 0.25
_DIMENSION_ANNOTATIONS_MIN = 10


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class JudgeFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model: str = Field(min_length=1)
    api_base_env: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    system_prompt_hash: str = Field(min_length=1)
    dimensions: dict[str, str] = Field(default_factory=dict)


class CalibrationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    entry_id: str = Field(min_length=1)
    calibration_id: str = Field(min_length=1)
    scope: Scope
    dimension: str | None = None
    judge: JudgeFingerprint
    # 对 bool assertion 是 accuracy；对 0-1 dimension 是容差内一致率。
    agreement: float = Field(ge=0, le=1)
    agreement_tolerance: float | None = Field(default=None, ge=0, le=1)
    mean_absolute_error: float | None = Field(default=None, ge=0, le=1)
    invalid_output_rate: float = Field(ge=0, le=1)
    qualified: bool
    evidence_report: str = Field(min_length=1)
    evidence_report_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    # 只用于在 evidence report 内定位记录；runtime 匹配仍不依赖可随意改名的 judge id。
    evidence_judge_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _scope_dimension(self) -> "CalibrationEntry":
        if self.scope == "absolute_assertions" and self.dimension is not None:
            raise ValueError("absolute_assertions calibration 不能声明 dimension")
        if self.scope != "absolute_assertions" and not self.dimension:
            raise ValueError(f"{self.scope} calibration 必须声明 dimension")
        if (self.scope == "standard_dimension"
                and self.dimension not in {*dimensions.STANDARD_DIMENSIONS, "final_answer_quality"}):
            raise ValueError(f"未知 standard dimension：{self.dimension}")
        if self.scope == "trajectory_dimension" and self.dimension not in _TRAJECTORY_METRICS:
            raise ValueError(f"未知 trajectory dimension：{self.dimension}")
        if self.scope == "absolute_assertions" and (
            self.agreement_tolerance is not None or self.mean_absolute_error is not None
        ):
            raise ValueError("absolute_assertions calibration 不使用连续分误差字段")
        if self.scope == "standard_dimension" and self.agreement_tolerance is None:
            raise ValueError("standard_dimension calibration 必须声明 agreement_tolerance")
        return self


class CalibrationRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"] = "1.0"
    entries: list[CalibrationEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_entries_and_gauges(self) -> "CalibrationRegistry":
        ids = [entry.entry_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("calibration registry entry_id 不能重复")
        keys = [
            (
                entry.scope,
                entry.dimension,
                json.dumps(entry.judge.model_dump(mode="json"), sort_keys=True),
            )
            for entry in self.entries
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("同一 scope/dimension/judge gauge 不能登记多次")
        return self


def _resolve(path: str | Path, *, relative_to: Path = ROOT) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else relative_to / candidate


def load_registry(path: str | Path) -> tuple[CalibrationRegistry, Path, str]:
    registry_path = _resolve(path)
    registry = CalibrationRegistry.model_validate_json(
        registry_path.read_text(encoding="utf-8")
    )
    for entry in registry.entries:
        evidence = _resolve(entry.evidence_report)
        if not evidence.is_file():
            raise ValueError(f"calibration evidence report 不存在：{evidence}")
        actual = sha256_file(evidence)
        if actual != entry.evidence_report_sha256:
            raise ValueError(
                f"calibration evidence report hash 不匹配：{evidence}；"
                f"registry={entry.evidence_report_sha256} actual={actual}"
            )
        report = json.loads(evidence.read_text(encoding="utf-8"))
        if report.get("calibration_id") != entry.calibration_id:
            raise ValueError(
                f"calibration evidence id 不匹配：entry={entry.calibration_id} "
                f"report={report.get('calibration_id')}"
            )
        evidence_matches = []
        for item in report.get("calibrations") or []:
            item_fingerprint = _fingerprint(item.get("judge"))
            if (item_fingerprint == entry.judge
                    and (item.get("judge") or {}).get("id") == entry.evidence_judge_id
                    and (
                        entry.scope == "absolute_assertions"
                        or item.get("dimension") == entry.dimension
                    )):
                evidence_matches.append(item)
        if len(evidence_matches) != 1:
            raise ValueError(
                f"calibration evidence 中匹配 judge gauge 的记录必须恰好一条："
                f"entry={entry.entry_id} matches={len(evidence_matches)}"
            )
        evidence_item = evidence_matches[0]
        absolute = entry.scope == "absolute_assertions"
        _validate_qualification_policy(report, evidence_item, scope=entry.scope)
        evidence_qualified = bool(
            evidence_item.get("qualified_for_absolute_assertions")
            if absolute else evidence_item.get("qualified")
        )
        evidence_agreement = (
            evidence_item.get("judge_vs_human_agreement")
            if absolute else evidence_item.get("within_tolerance_agreement")
        )
        if (
            evidence_qualified != entry.qualified
            or float(evidence_agreement if evidence_agreement is not None else -1)
            != entry.agreement
            or float(evidence_item.get("invalid_judge_output_rate", -1))
            != entry.invalid_output_rate
            or (
                not absolute
                and float(evidence_item.get("agreement_tolerance", -1))
                != entry.agreement_tolerance
            )
            or (
                not absolute
                and evidence_item.get("mean_absolute_error")
                != entry.mean_absolute_error
            )
        ):
            raise ValueError(f"calibration registry 与 evidence 指标不一致：{entry.entry_id}")
    return registry, registry_path, sha256_file(registry_path)


def _validate_qualification_policy(
    report: dict[str, Any], item: dict[str, Any], *, scope: Scope
) -> None:
    """Registry has hard floors; a report cannot self-authorize with lax flags."""
    qualified = bool(
        item.get("qualified_for_absolute_assertions")
        if scope == "absolute_assertions" else item.get("qualified")
    )
    if not qualified:
        return
    thresholds = report.get("thresholds") or {}
    agreement_key = (
        "judge_vs_human_agreement_min"
        if scope == "absolute_assertions" else "within_tolerance_agreement_min"
    )
    agreement_threshold = float(thresholds.get(agreement_key, -1))
    invalid_threshold = float(thresholds.get("invalid_judge_output_rate_max", 2))
    if agreement_threshold < _AGREEMENT_MIN or invalid_threshold > _INVALID_MAX:
        raise ValueError(
            "calibration report 使用了低于 registry policy 的门槛："
            f"agreement_min={agreement_threshold}, invalid_max={invalid_threshold}"
        )
    if scope == "standard_dimension":
        tolerance = float(thresholds.get("absolute_score_error_tolerance", 2))
        minimum = int(thresholds.get("n_annotations_min", 0))
        if tolerance > _DIMENSION_TOLERANCE_MAX or minimum < _DIMENSION_ANNOTATIONS_MIN:
            raise ValueError(
                "standard dimension calibration 使用了过松 policy："
                f"tolerance={tolerance}, n_annotations_min={minimum}"
            )
        if int(item.get("n_annotations", 0)) < _DIMENSION_ANNOTATIONS_MIN:
            raise ValueError("standard dimension calibration 人工标注少于 10 条")


def registry_from_report(path: str | Path) -> CalibrationRegistry:
    """把 assertion 或 standard-dimension report 转成可审计 registry。"""
    report_path = _resolve(path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_hash = sha256_file(report_path)
    by_gauge: dict[str, CalibrationEntry] = {}
    report_kind = payload.get("calibration_kind", "absolute_assertions")
    if report_kind not in {"absolute_assertions", "standard_dimensions"}:
        raise ValueError(f"未知 calibration_kind：{report_kind}")
    for item in payload.get("calibrations") or []:
        fingerprint = _fingerprint(item.get("judge"))
        if fingerprint is None:
            raise ValueError("calibration report 的 judge fingerprint 不完整，不能注册")
        judge_id = (item.get("judge") or {}).get("id")
        if not judge_id:
            raise ValueError("calibration report 的 judge.id 为空，无法定位 evidence")
        standard = report_kind == "standard_dimensions"
        dimension = item.get("dimension") if standard else None
        if standard and not dimension:
            raise ValueError("standard dimension calibration 缺 dimension")
        _validate_qualification_policy(
            payload,
            item,
            scope="standard_dimension" if standard else "absolute_assertions",
        )
        entry = CalibrationEntry(
            entry_id=(
                f"{payload['calibration_id']}--{judge_id}--{dimension}"
                if standard else f"{payload['calibration_id']}--{judge_id}"
            ),
            calibration_id=payload["calibration_id"],
            scope="standard_dimension" if standard else "absolute_assertions",
            dimension=dimension,
            judge=fingerprint,
            agreement=(
                item["within_tolerance_agreement"]
                if standard else item["judge_vs_human_agreement"]
            ),
            agreement_tolerance=item.get("agreement_tolerance") if standard else None,
            mean_absolute_error=item.get("mean_absolute_error") if standard else None,
            invalid_output_rate=item["invalid_judge_output_rate"],
            qualified=bool(
                item["qualified"] if standard
                else item["qualified_for_absolute_assertions"]
            ),
            evidence_report=str(report_path),
            evidence_report_sha256=report_hash,
            evidence_judge_id=judge_id,
        )
        key = json.dumps({
            "fingerprint": fingerprint.model_dump(mode="json"),
            "dimension": dimension,
        }, sort_keys=True)
        previous = by_gauge.get(key)
        if previous is None or (entry.agreement, -entry.invalid_output_rate) > (
            previous.agreement, -previous.invalid_output_rate
        ):
            by_gauge[key] = entry
    return CalibrationRegistry(entries=list(by_gauge.values()))


def _fingerprint(judge: dict[str, Any] | None) -> JudgeFingerprint | None:
    if not judge:
        return None
    required = ("model", "api_base_env", "system_prompt_hash")
    if any(not judge.get(key) for key in required):
        return None
    return JudgeFingerprint(
        model=judge["model"],
        api_base_env=judge["api_base_env"],
        params=judge.get("params") or {},
        system_prompt_hash=judge["system_prompt_hash"],
        dimensions=judge.get("dimensions") or {},
    )


def deterministic_trajectory_metrics(layer: dict[str, Any], mode: str) -> set[str]:
    """最终分确实来自 deterministic evidence 的 trajectory metrics。"""
    if mode == "judge":
        return set()
    available: set[str] = set()
    for row in layer.get("structured") or []:
        available.update(
            metric for metric, value in (row.get("metrics") or {}).items()
            if value is not None
        )
    return available


def metric_calibration_scope(
    metric: str,
    *,
    deterministic_trajectory: set[str],
) -> tuple[Scope, str | None] | None:
    if metric in dimensions.STANDARD_DIMENSIONS:
        return "standard_dimension", metric
    if metric == "assertion_pass_rate":
        return "absolute_assertions", None
    if metric == "final_answer_quality":
        return "standard_dimension", metric
    if metric in _TRAJECTORY_METRICS and metric not in deterministic_trajectory:
        return "trajectory_dimension", metric
    return None


def assess_gate_calibration(
    gate: dict[str, str],
    *,
    registry_path: str | Path | None,
    output_judge: dict[str, Any] | None,
    trajectory_judge: dict[str, Any] | None,
    deterministic_trajectory: set[str],
) -> dict[str, Any]:
    required = {
        metric: scope
        for metric in gate
        if (scope := metric_calibration_scope(
            metric, deterministic_trajectory=deterministic_trajectory
        )) is not None
    }
    result: dict[str, Any] = {
        "registry": str(registry_path) if registry_path else None,
        "registry_sha256": None,
        "metrics": {},
        "unqualified": [],
    }
    if not required:
        return result

    registry = None
    if registry_path:
        registry, resolved_path, registry_hash = load_registry(registry_path)
        result["registry"] = str(resolved_path)
        result["registry_sha256"] = registry_hash

    for metric, (scope, dimension) in required.items():
        judge = trajectory_judge if scope == "trajectory_dimension" else output_judge
        fingerprint = _fingerprint(judge)
        matches = [
            entry for entry in (registry.entries if registry else [])
            if entry.scope == scope
            and entry.dimension == dimension
            and fingerprint is not None
            and entry.judge == fingerprint
        ]
        entry = matches[0] if matches else None
        qualified = bool(entry and entry.qualified)
        reason = (
            "qualified"
            if qualified else
            "no calibration registry configured"
            if not registry_path else
            "judge/rubric fingerprint not registered"
            if entry is None else
            "registered calibration did not meet thresholds"
        )
        result["metrics"][metric] = {
            "scope": scope,
            "dimension": dimension,
            "qualified": qualified,
            "reason": reason,
            "entry_id": entry.entry_id if entry else None,
            "calibration_id": entry.calibration_id if entry else None,
        }
        if not qualified:
            result["unqualified"].append(metric)
    return result


def calibrated_gate_outcome(
    gate_rows: list[tuple[str, str, float, bool]],
    unqualified: set[str],
) -> bool | None:
    """确定性/已校准 gate 先判失败；只有未校准量具悬而未决时返回 None。"""
    enforced = [ok for metric, _condition, _actual, ok in gate_rows
                if metric not in unqualified]
    if any(not ok for ok in enforced):
        return False
    if unqualified:
        return None
    return all(enforced) if enforced else None
