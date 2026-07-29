"""Judge calibration against a versioned human-annotation set.

This is deliberately an offline workflow: it reads already archived grading
files, performs no model calls, and writes a new calibration report.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sklearn.metrics import accuracy_score


class HumanAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    assertion: str = Field(min_length=1)
    passed: bool
    rationale: str = Field(min_length=1)


class HumanGold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    calibration_id: str = Field(min_length=1)
    source_run: str = Field(min_length=1)
    annotated_by: str = Field(min_length=1)
    annotated_at: str = Field(min_length=1)
    annotations: list[HumanAnnotation] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_assertions(self) -> "HumanGold":
        keys = [(item.case_id, item.assertion) for item in self.annotations]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"human gold 含重复 (case_id, assertion)：{duplicates}")
        return self


class JudgeCalibration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grading_file: str
    judge: dict
    n_annotations: int
    judge_vs_human_agreement: float
    invalid_judge_output_rate: float
    invalid_judge_outputs: int
    attempted_judge_calls: int
    ab_swap_consistency: None = None
    ab_swap_applicability: Literal["not_applicable_absolute_assertions"]
    qualified_for_absolute_assertions: bool
    disagreements: list[dict]


class CalibrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    calibration_id: str
    gold_file: str
    source_run: str
    generated_at: str
    thresholds: dict[str, float]
    calibrations: list[JudgeCalibration]


def load_gold(path: str | Path) -> HumanGold:
    return HumanGold.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _grading_assertions(payload: dict, *, path: Path) -> dict[tuple[str, str], dict]:
    indexed: dict[tuple[str, str], dict] = {}
    duplicates: list[tuple[str, str]] = []
    for run in payload.get("graded") or []:
        case_id = run.get("case_id")
        for item in run.get("expectations") or []:
            key = (case_id, item.get("text"))
            if key in indexed:
                duplicates.append(key)
            indexed[key] = item
    if duplicates:
        raise ValueError(f"{path} 含重复 (case_id, assertion)：{sorted(set(duplicates))}")
    return indexed


def calibrate_one(
    gold: HumanGold,
    grading_path: str | Path,
    *,
    agreement_threshold: float = 0.80,
    invalid_threshold: float = 0.02,
) -> JudgeCalibration:
    path = Path(grading_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    actual = _grading_assertions(payload, path=path)
    expected = {(item.case_id, item.assertion): item for item in gold.annotations}

    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    if missing or unexpected:
        raise ValueError(
            f"{path} 与 human gold 覆盖不一致；missing={missing or '[]'}；"
            f"unexpected={unexpected or '[]'}"
        )

    ordered = [(item.case_id, item.assertion) for item in gold.annotations]
    truth = [expected[key].passed for key in ordered]
    predicted = [bool(actual[key]["passed"]) for key in ordered]
    agreement = float(accuracy_score(truth, predicted))

    failures = payload.get("judge_failures") or []
    attempted_calls = len(payload.get("graded") or []) + len(failures)
    invalid_rate = len(failures) / attempted_calls if attempted_calls else 0.0
    disagreements = [
        {
            "case_id": key[0],
            "assertion": key[1],
            "human_passed": expected[key].passed,
            "judge_passed": bool(actual[key]["passed"]),
            "human_rationale": expected[key].rationale,
            "judge_evidence": actual[key].get("evidence") or "",
        }
        for key in ordered
        if expected[key].passed != bool(actual[key]["passed"])
    ]
    return JudgeCalibration(
        grading_file=str(path),
        judge=payload.get("judge") or {},
        n_annotations=len(ordered),
        judge_vs_human_agreement=round(agreement, 4),
        invalid_judge_output_rate=round(invalid_rate, 4),
        invalid_judge_outputs=len(failures),
        attempted_judge_calls=attempted_calls,
        ab_swap_consistency=None,
        ab_swap_applicability="not_applicable_absolute_assertions",
        qualified_for_absolute_assertions=(
            agreement >= agreement_threshold and invalid_rate <= invalid_threshold
        ),
        disagreements=disagreements,
    )


def calibrate(
    gold_path: str | Path,
    grading_paths: list[str | Path],
    *,
    agreement_threshold: float = 0.80,
    invalid_threshold: float = 0.02,
) -> CalibrationReport:
    gold = load_gold(gold_path)
    return CalibrationReport(
        calibration_id=gold.calibration_id,
        gold_file=str(Path(gold_path)),
        source_run=gold.source_run,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        thresholds={
            "judge_vs_human_agreement_min": agreement_threshold,
            "invalid_judge_output_rate_max": invalid_threshold,
        },
        calibrations=[
            calibrate_one(
                gold,
                path,
                agreement_threshold=agreement_threshold,
                invalid_threshold=invalid_threshold,
            )
            for path in grading_paths
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="离线比较 versioned human gold 与一个或多个 grading.<judge>.json"
    )
    parser.add_argument("--gold", required=True)
    parser.add_argument("--grading", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--agreement-threshold", type=float, default=0.80)
    parser.add_argument("--invalid-threshold", type=float, default=0.02)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    for name, value in (
        ("--agreement-threshold", args.agreement_threshold),
        ("--invalid-threshold", args.invalid_threshold),
    ):
        if not 0 <= value <= 1:
            parser.error(f"{name} 必须在 0 与 1 之间")

    output = Path(args.output)
    if output.exists() and not args.force:
        raise FileExistsError(f"拒绝覆盖已有校准报告：{output}；需要重算时显式加 --force")
    report = calibrate(
        args.gold,
        args.grading,
        agreement_threshold=args.agreement_threshold,
        invalid_threshold=args.invalid_threshold,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    for item in report.calibrations:
        judge_id = item.judge.get("id") or Path(item.grading_file).stem
        status = "QUALIFIED" if item.qualified_for_absolute_assertions else "NOT QUALIFIED"
        print(
            f"{judge_id}: agreement={item.judge_vs_human_agreement:.1%}, "
            f"invalid={item.invalid_judge_output_rate:.1%} → {status}"
        )
    print(f"校准报告 → {output}")


if __name__ == "__main__":
    main()
