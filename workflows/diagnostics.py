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


def self_judge_warnings(
    suite: dict[str, Any], *, observed_models: Iterable[str] = ()
) -> list[str]:
    """Warn when the candidate model is also configured as its own judge."""
    judge = ((suite.get("scoring") or {}).get("judge") or {})
    judge_model = judge.get("model")
    if not judge_model:
        return []
    execution_models = {
        model.get("model")
        for model in suite.get("models", [])
        if isinstance(model, dict) and model.get("model")
    }
    runtime_model = (suite.get("runtime_options") or {}).get("model")
    if runtime_model:
        execution_models.add(runtime_model)
    execution_models.update(model for model in observed_models if model)
    if judge_model not in execution_models:
        return []
    return [
        f"execution model and judge model are both `{judge_model}`; this is self-judging. "
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
