"""Human-facing diagnostics shared by preflight and scoring.

These checks deliberately warn instead of changing a suite.  A plan should
make an impossible gate visible before it spends a model request, while the
suite remains the user's source of truth.
"""
from __future__ import annotations

from collections.abc import Iterable
from collections import Counter
from typing import Any

from contracts import RoutingCase

# 不算在被测 skill 头上的失败：评测系统/网络/runtime 自己挂了（AGENTS.md ★★★ ⑥）。
# score_full / score_routing / viewer / compare_runs 共用同一份分类，别各自写一份漂移。
SYSTEM_FAILURE_KINDS = ("runtime", "network", "harness")


def canonical_model_identity(model: str) -> str:
    """Return the provider-neutral part of a concrete model identifier.

    LiteLLM-compatible identifiers commonly prefix the same upstream model
    with the adapter/provider used to reach it (for example
    ``openai/qwen3.5-plus`` and ``qwen/qwen3.5-plus``).  The provider is not an
    independent evaluator, so compare the final path component.  We
    deliberately do not maintain a vendor alias table: names such as
    ``qwen-plus`` and ``qwen3.5-plus`` cannot be proven equivalent locally.
    """
    return model.strip().lower().rstrip("/").rsplit("/", 1)[-1]


def self_judge_warnings(
    suite: dict[str, Any],
    *,
    observed_models: Iterable[str] = (),
    judge_model: str | None = None,
) -> list[str]:
    """Warn when self-judging is detected or independence is unknowable."""
    judge = ((suite.get("scoring") or {}).get("judge") or {})
    actual_judge_model = judge_model or judge.get("model")
    if not actual_judge_model:
        return []
    configured_models = {
        model.get("model")
        for model in suite.get("models", [])
        if isinstance(model, dict) and model.get("model")
    }
    runtime_model = (suite.get("runtime_options") or {}).get("model")
    if runtime_model:
        configured_models.add(runtime_model)
    resolved_models = {model for model in observed_models if model}
    execution_models = configured_models | resolved_models

    if not execution_models:
        return [
            f"judge independence is unverified: judge is `{actual_judge_model}`, but the "
            "execution runtime has not exposed a concrete model identity in suite "
            "configuration or RunResult.resolved_model. Do not treat different labels or "
            "missing metadata as proof of an independent judge."
        ]

    judge_identity = canonical_model_identity(actual_judge_model)
    matches = sorted(
        model for model in execution_models
        if canonical_model_identity(model) == judge_identity
    )
    if not matches:
        return []
    execution_display = ", ".join(f"`{model}`" for model in matches)
    return [
        f"judge `{actual_judge_model}` and execution model {execution_display} resolve to "
        f"the same model identity `{judge_identity}`; this is self-judging. "
        "Keep its semantic scores diagnostic-only, or configure an independent judge before "
        "using them for improvement/release decisions."
    ]


def gate_coverage_warnings(cases: Iterable[RoutingCase], gate: dict[str, str]) -> list[str]:
    """Explain gates that have no eligible case and would otherwise become N/A.

    The scorer correctly treats unavailable metrics as N/A rather than zero.
    That is still surprising when, for example, a suite gates on
    ``critical_miss`` but every case is only ``high`` severity.
    """
    all_cases = list(cases)
    counts = {
        "top1": sum(len(case.expected_skills) == 1 for case in all_cases),
        "multi_exact": sum(len(case.expected_skills) > 1 for case in all_cases),
        "no_skill_rejection": sum(not case.expected_skills for case in all_cases),
        "false_activation": sum(not case.expected_skills for case in all_cases),
        "critical_miss": sum(case.severity == "critical" for case in all_cases),
    }
    for case_type in ("pos", "amb", "multi", "rej"):
        counts[f"type_{case_type}"] = sum(
            case.id.split("-")[-2] == case_type if len(case.id.split("-")) >= 3 else False
            for case in all_cases
        )

    warnings = []
    for metric in gate:
        if metric in counts and counts[metric] == 0:
            if metric == "critical_miss":
                remedy = "mark the intended release-blocking cases severity: critical, or remove this gate"
            else:
                remedy = "add an eligible case, or remove this gate"
            warnings.append(
                f"gate `{metric}` has no evaluable cases, so scoring will report N/A; {remedy}."
            )
    return warnings


_REMEDIATION = {
    "network_dns": "check network/DNS and the configured API base URL, then rerun `pipeline plan --healthcheck`.",
    "network_connectivity": "check connectivity to the provider endpoint, then rerun `pipeline plan --healthcheck`.",
    "network_timeout": "check provider availability and timeout/network policy before retrying.",
    "provider_authentication": "check the configured API-key environment variable (its value is never printed).",
    "provider_quota_exhausted": "quota is exhausted; pick the next model in MODELS.local.md and rerun the whole suite — never swap models mid-run, and keep the same model for a V1/V2 comparison.",
    "provider_rate_limited": "wait for the provider limit window or reduce request rate before retrying.",
    "network": "check provider connectivity and credentials before retrying.",
    "runtime": "inspect the runtime healthcheck detail and repair the runtime before retrying.",
    "harness": "inspect the recorded error; this is an evaluation-system/configuration failure, not a skill score.",
    "task": "inspect the model output and case expectation; this is an evaluable task failure.",
    "unclassified": "inspect the recorded error before interpreting the score.",
}


def failure_summary(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate failed runs without copying provider messages into reports."""
    failures = [run for run in runs if not run.get("ok", True)]
    by_kind = Counter((run.get("error_kind") or "unclassified") for run in failures)
    by_subkind = Counter(run.get("error_subkind") for run in failures if run.get("error_subkind"))
    remediation_keys = list(by_subkind) or list(by_kind)
    return {
        "failed_runs": len(failures),
        "by_error_kind": dict(sorted(by_kind.items())),
        "by_error_subkind": dict(sorted(by_subkind.items())),
        "remediation": [
            {"category": key, "action": _REMEDIATION.get(key, _REMEDIATION["unclassified"])}
            for key in remediation_keys
        ],
    }


def system_failure_counts(runs: Iterable[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    """How many runs never reached the skill at all, and why.

    ``error_kind`` is the only structured signal used here — never the error
    message text — so a run flips into this bucket by its recorded kind, not
    by pattern-matching provider prose (HANDOFF ★ 更新 16, requirement 3).
    """
    by_kind = Counter(
        run.get("error_kind") for run in runs if run.get("error_kind") in SYSTEM_FAILURE_KINDS
    )
    return sum(by_kind.values()), dict(sorted(by_kind.items()))


def derive_verdict(
    *,
    n_runs: int,
    n_system_failures: int,
    system_failures_by_kind: dict[str, int] | None = None,
    observed_passed: bool | None,
    is_mock: bool = False,
) -> dict[str, Any]:
    """The single place that turns run facts into pass / fail / indeterminate.

    Every consumer — score_full, score_routing, pipeline inspect, pipeline
    view, compare_runs — must call this instead of computing its own verdict
    or trusting a stored ``quality_verdict``/``gate_pass`` field as the
    conclusion. That field can be stale (written by an older scorer, or by a
    run that never got scored) and stale verdicts are what let the same
    all-system-failure run show "FAIL" in one place and "N/A" in another
    (HANDOFF ★ 更新 16). The system-failure check runs first and overrides
    any ``observed_passed`` passed in, precisely so a stale FAIL can't leak
    through when the structured counts show the Skill never executed.
    """
    if is_mock:
        return {
            "quality_verdict": "not_evaluated",
            "gate_pass": None,
            "label": "NOT EVALUATED",
            "reason": "synthetic mock run — pipeline smoke only, not a skill-quality verdict",
        }
    by_kind = dict(sorted((system_failures_by_kind or {}).items()))
    if n_runs == 0 or n_system_failures >= n_runs:
        kinds = ", ".join(f"{k}={v}" for k, v in by_kind.items())
        reason = (
            "no runs recorded" if n_runs == 0 else
            f"all {n_runs} run(s) are system/environment/provider failures ({kinds}); "
            "the Skill never executed — this is not a pass or fail"
        )
        return {"quality_verdict": "indeterminate", "gate_pass": None,
                "label": "INDETERMINATE", "reason": reason}
    if observed_passed is None:
        return {
            "quality_verdict": "indeterminate",
            "gate_pass": None,
            "label": "INDETERMINATE",
            "reason": "no gate metric could be evaluated (all N/A or judge-uncalibrated)",
        }
    return {
        "quality_verdict": "pass" if observed_passed else "fail",
        "gate_pass": observed_passed,
        "label": "PASS" if observed_passed else "FAIL",
        "reason": "deterministic gate PASS" if observed_passed else "deterministic gate FAIL",
    }
