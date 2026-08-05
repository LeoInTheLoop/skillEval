"""Calibrate continuous semantic dimensions against versioned human scores.

This workflow is offline: it reads immutable ``grading.<judge>.json`` files,
does not call a model, and never treats model-generated labels as human gold.
For a 0-1 score, agreement means ``abs(judge - human) <= tolerance``.  The
tolerance is explicit in the report; mean absolute error is retained as a
second, scale-sensitive diagnostic.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from workflows import dimensions


class DimensionHumanAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    repeat_index: int = Field(default=0, ge=0)
    turn_index: int = Field(default=1, ge=1)
    dimension: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)


class DimensionHumanGold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    calibration_id: str = Field(min_length=1)
    source_run: str = Field(min_length=1)
    annotated_by: str = Field(min_length=1)
    annotated_at: str = Field(min_length=1)
    dimension_versions: dict[str, str] = Field(min_length=1)
    annotations: list[DimensionHumanAnnotation] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_dimensions_and_keys(self) -> "DimensionHumanGold":
        unknown = sorted(set(self.dimension_versions) - set(dimensions.STANDARD_DIMENSIONS))
        if unknown:
            raise ValueError(f"human gold 含未知标准维度：{unknown}")
        annotated = {item.dimension for item in self.annotations}
        declared = set(self.dimension_versions)
        if annotated != declared:
            raise ValueError(
                "dimension_versions 与 annotations 维度不一致；"
                f"missing={sorted(declared - annotated)} unexpected={sorted(annotated - declared)}"
            )
        keys = [_annotation_key(item) for item in self.annotations]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"human dimension gold 含重复定位键：{duplicates}")
        return self


class DimensionJudgeCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grading_file: str
    judge: dict
    dimension: str
    rubric_version: str
    n_annotations: int
    n_valid_scores: int
    within_tolerance_agreement: float
    agreement_tolerance: float
    mean_absolute_error: float | None
    invalid_judge_output_rate: float
    invalid_judge_outputs: int
    attempted_judge_calls: int
    ab_swap_consistency: None = None
    ab_swap_applicability: Literal["not_applicable_absolute_dimension_score"]
    qualified: bool
    disagreements: list[dict]


class DimensionCalibrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    calibration_kind: Literal["standard_dimensions"] = "standard_dimensions"
    calibration_id: str
    gold_file: str
    source_run: str
    generated_at: str
    thresholds: dict[str, float]
    calibrations: list[DimensionJudgeCalibration]


def _annotation_key(item: DimensionHumanAnnotation) -> tuple[str, int, int, str]:
    return item.case_id, item.repeat_index, item.turn_index, item.dimension


def load_gold(path: str | Path) -> DimensionHumanGold:
    return DimensionHumanGold.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _grading_scores(payload: dict, *, target_dimensions: set[str], path: Path) -> dict:
    indexed: dict[tuple[str, int, int, str], dict] = {}
    duplicates: list[tuple[str, int, int, str]] = []
    for run in payload.get("graded") or []:
        base = (
            run.get("case_id"),
            int(run.get("repeat_index", 0)),
            int(run.get("turn_index", 1)),
        )
        for item in run.get("dimensions") or []:
            if item.get("dimension") not in target_dimensions:
                continue
            key = (*base, item.get("dimension"))
            if key in indexed:
                duplicates.append(key)
            indexed[key] = item
    if duplicates:
        raise ValueError(f"{path} 含重复 dimension score：{sorted(set(duplicates))}")
    return indexed


def calibrate_one(
    gold: DimensionHumanGold,
    grading_path: str | Path,
    *,
    agreement_threshold: float = 0.80,
    invalid_threshold: float = 0.02,
    tolerance: float = 0.25,
    min_annotations: int = 10,
) -> list[DimensionJudgeCalibration]:
    path = Path(grading_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    judge = payload.get("judge") or {}
    judge_versions = judge.get("dimensions") or {}
    version_mismatches = {
        dimension: {"human": version, "judge": judge_versions.get(dimension)}
        for dimension, version in gold.dimension_versions.items()
        if judge_versions.get(dimension) != version
    }
    if version_mismatches:
        raise ValueError(
            f"{path} 的 rubric version 与 human gold 不一致：{version_mismatches}"
        )

    expected = {_annotation_key(item): item for item in gold.annotations}
    actual = _grading_scores(
        payload, target_dimensions=set(gold.dimension_versions), path=path
    )
    failures = {
        (
            item.get("case_id"),
            int(item.get("repeat_index", 0)),
            int(item.get("turn_index", 1)),
        )
        for item in payload.get("judge_failures") or []
    }
    missing_without_failure = sorted(
        key for key in set(expected) - set(actual) if key[:3] not in failures
    )
    unexpected = sorted(set(actual) - set(expected))
    if missing_without_failure or unexpected:
        raise ValueError(
            f"{path} 与 human dimension gold 覆盖不一致；"
            f"missing={missing_without_failure or '[]'}；unexpected={unexpected or '[]'}"
        )

    results: list[DimensionJudgeCalibration] = []
    for dimension, rubric_version in gold.dimension_versions.items():
        ordered = [item for item in gold.annotations if item.dimension == dimension]
        valid = [item for item in ordered if _annotation_key(item) in actual]
        deltas = [
            abs(float(actual[_annotation_key(item)]["score"]) - item.score)
            for item in valid
        ]
        agreements = sum(delta <= tolerance for delta in deltas)
        agreement = agreements / len(valid) if valid else 0.0
        mae = sum(deltas) / len(deltas) if deltas else None

        attempted_calls = {(_annotation_key(item))[:3] for item in ordered}
        invalid_calls = attempted_calls & failures
        invalid_rate = len(invalid_calls) / len(attempted_calls) if attempted_calls else 0.0
        disagreements = []
        for item, delta in zip(valid, deltas, strict=True):
            if delta <= tolerance:
                continue
            judged = actual[_annotation_key(item)]
            disagreements.append({
                "case_id": item.case_id,
                "repeat_index": item.repeat_index,
                "turn_index": item.turn_index,
                "human_score": item.score,
                "judge_score": float(judged["score"]),
                "absolute_error": round(delta, 4),
                "human_rationale": item.rationale,
                "judge_evidence": judged.get("evidence") or "",
            })
        results.append(DimensionJudgeCalibration(
            grading_file=str(path),
            judge=judge,
            dimension=dimension,
            rubric_version=rubric_version,
            n_annotations=len(ordered),
            n_valid_scores=len(valid),
            within_tolerance_agreement=round(agreement, 4),
            agreement_tolerance=tolerance,
            mean_absolute_error=round(mae, 4) if mae is not None else None,
            invalid_judge_output_rate=round(invalid_rate, 4),
            invalid_judge_outputs=len(invalid_calls),
            attempted_judge_calls=len(attempted_calls),
            ab_swap_consistency=None,
            ab_swap_applicability="not_applicable_absolute_dimension_score",
            qualified=(
                len(ordered) >= min_annotations
                and bool(valid)
                and agreement >= agreement_threshold
                and invalid_rate <= invalid_threshold
            ),
            disagreements=disagreements,
        ))
    return results


def calibrate(
    gold_path: str | Path,
    grading_paths: list[str | Path],
    *,
    agreement_threshold: float = 0.80,
    invalid_threshold: float = 0.02,
    tolerance: float = 0.25,
    min_annotations: int = 10,
) -> DimensionCalibrationReport:
    gold = load_gold(gold_path)
    calibrations = [
        item
        for path in grading_paths
        for item in calibrate_one(
            gold,
            path,
            agreement_threshold=agreement_threshold,
            invalid_threshold=invalid_threshold,
            tolerance=tolerance,
            min_annotations=min_annotations,
        )
    ]
    return DimensionCalibrationReport(
        calibration_id=gold.calibration_id,
        gold_file=str(Path(gold_path)),
        source_run=gold.source_run,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        thresholds={
            "within_tolerance_agreement_min": agreement_threshold,
            "invalid_judge_output_rate_max": invalid_threshold,
            "absolute_score_error_tolerance": tolerance,
            "n_annotations_min": float(min_annotations),
        },
        calibrations=calibrations,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="离线校准 0-1 标准语义维度；gold 必须由人工标注"
    )
    parser.add_argument("--gold", required=True)
    parser.add_argument("--grading", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--agreement-threshold", type=float, default=0.80)
    parser.add_argument("--invalid-threshold", type=float, default=0.02)
    parser.add_argument("--tolerance", type=float, default=0.25)
    parser.add_argument("--min-annotations", type=int, default=10)
    parser.add_argument("--registry-output")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    for name, value in (
        ("--agreement-threshold", args.agreement_threshold),
        ("--invalid-threshold", args.invalid_threshold),
        ("--tolerance", args.tolerance),
    ):
        if not 0 <= value <= 1:
            parser.error(f"{name} 必须在 0 与 1 之间")
    if args.agreement_threshold < 0.80:
        parser.error("--agreement-threshold 不得低于 registry policy 0.80")
    if args.invalid_threshold > 0.02:
        parser.error("--invalid-threshold 不得高于 registry policy 0.02")
    if args.tolerance > 0.25:
        parser.error("--tolerance 不得高于 registry policy 0.25")
    if args.min_annotations < 10:
        parser.error("--min-annotations 不得少于 registry policy 10")

    output = Path(args.output)
    registry_output = Path(args.registry_output) if args.registry_output else None
    for kind, path in (("校准报告", output), ("calibration registry", registry_output)):
        if path and path.exists() and not args.force:
            raise FileExistsError(f"拒绝覆盖已有{kind}：{path}；需要重算时显式加 --force")

    report = calibrate(
        args.gold,
        args.grading,
        agreement_threshold=args.agreement_threshold,
        invalid_threshold=args.invalid_threshold,
        tolerance=args.tolerance,
        min_annotations=args.min_annotations,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if registry_output:
        from workflows.calibration_registry import registry_from_report

        registry = registry_from_report(output)
        registry_output.parent.mkdir(parents=True, exist_ok=True)
        registry_output.write_text(registry.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for item in report.calibrations:
        judge_id = item.judge.get("id") or Path(item.grading_file).stem
        status = "QUALIFIED" if item.qualified else "NOT QUALIFIED"
        mae = "N/A" if item.mean_absolute_error is None else f"{item.mean_absolute_error:.3f}"
        print(
            f"{judge_id}/{item.dimension}: agreement={item.within_tolerance_agreement:.1%}, "
            f"MAE={mae}, invalid={item.invalid_judge_output_rate:.1%} → {status}"
        )
    print(f"维度校准报告 → {output}")
    if registry_output:
        print(f"校准注册表 → {registry_output}")


if __name__ == "__main__":
    main()
