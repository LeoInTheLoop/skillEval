"""Read-only preflight plan for a reproducible evaluation suite.

This module deliberately *does not* add model, dataset, repeat, or skill CLI
overrides.  Those belong in the suite so that they are captured by
``config_hash``.  Its job is to make the resolved experiment visible before a
user spends API budget or invokes an agent runtime.
"""
from __future__ import annotations

import os
import socket
import json
import hashlib
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from contracts import (
    RoutingCase,
    SkillMeta,
    dataset_review_status,
    format_suite_validation_error,
    load_cases,
    load_suite,
    resolve_suite_references,
)
from workflows.matrix import build_matrix
from workflows.diagnostics import gate_coverage_warnings
from workflows.run_routing import (
    ROOT,
    build_environment,
    build_runtime,
    config_hash,
    file_hash,
    missing_case_files,
    resolve_skills,
    run_dir,
)


def credential_state(key_env: str | None) -> str:
    """Classify one model's API key before anything is spent on it.

    `missing` is not the only unusable state.  Copying `.env.example` without
    editing leaves a literal placeholder, which the provider rejects far
    downstream as an opaque transport error — a non-ASCII placeholder does not
    even survive HTTP header encoding, and litellm reports that as an upstream
    InternalServerError.  Catching it here keeps "you never pasted your key"
    from being reported as "the network is down".
    """
    if not key_env:
        return "not_required"
    value = (os.environ.get(key_env) or "").strip()
    if not value:
        return "missing"
    if not value.isascii() or "REPLACE_ME" in value.upper():
        return "placeholder"
    return "configured"


@dataclass(frozen=True)
class PlannedModel:
    """One model axis after checking only whether its configured key exists."""

    id: str
    model: str | None
    params: dict[str, Any]
    api_base_env: str | None
    api_key_env: str | None
    credential: str
    result_dir: str
    config_hash: str
    tasks: int


@dataclass(frozen=True)
class PipelinePlan:
    """The complete, human-readable execution plan derived from one suite."""

    suite_path: str
    suite_id: str
    suite_version: str
    planned_at: str
    execution_id: str | None
    description: str
    dataset: str
    dataset_hash: str
    cases: int
    turns: int
    skill_mode: str
    skill_dir: str
    skill_cfg: str
    target_subjects: list[str]
    pipeline_mode: str
    iteration: int
    pinned_versions: dict[str, str]
    included_skills: list[str]
    excluded_skills: list[str]
    selected_skills: list[str]
    skill_versions: dict[str, str]
    skill_hashes: dict[str, str]
    runtime: str
    runtime_options: dict[str, Any]
    routing_input: dict[str, Any]
    environment: dict[str, Any]
    health: dict[str, dict[str, Any]]
    repeats: int
    parallelism: int
    timeout_seconds: int
    tools: list[str]
    scoring_metrics: list[str]
    gate: dict[str, str]
    stages: list[dict[str, str]]
    models: list[PlannedModel]
    blocked_reasons: list[str]
    warnings: list[str]
    egress: dict[str, Any]
    mock: bool

    @property
    def runnable(self) -> bool:
        return not self.blocked_reasons

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["runnable"] = self.runnable
        return data


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _stage_plan(suite: dict[str, Any]) -> list[dict[str, str]]:
    """Describe the default pipeline without silently enabling paid judging."""
    scorer = "workflows.score_full" if suite["skills"]["mode"] == "full" else "workflows.score_routing"
    stages = [
        {
            "name": "run",
            "module": "workflows.run_routing",
            "detail": "build matrix → prepare environment → runtime → runs.jsonl + config snapshot",
        },
        {
            "name": "score",
            "module": scorer,
            "detail": "deterministic scoring + gate → scores.json + report.html",
        },
    ]
    if suite.get("scoring", {}).get("judge"):
        stages.append(
            {
                "name": "grade (optional)",
                "module": "workflows.grade",
                "detail": "independent judge; external model call, so never enabled by default",
            }
        )
    return stages


def unreachable_gold_warnings(
    cases: list[RoutingCase], skills: list[SkillMeta], mode: str
) -> list[str]:
    """gold 指向 catalog 里没有的 skill 时，那些题**没有正确答案可选**。

    `skills.include/exclude` 让用户自己决定这次把哪些 skill 传进去（§7.3d），代价是
    可能漏掉目标：漏掉之后模型永远答不对，指标必然掉，而现象看起来像「skill 变差了」，
    翻半天 runs.jsonl 才发现是 catalog 配错。这里在花钱之前把它说出来。

    不拦运行：No-Skill 基线（§18.3）正是**故意**让目标 skill 不可达，那时这条提示
    就是对基线定义的确认。`mode: none` 下 catalog 本来就是空的，无须再说。
    """
    if mode == "none":
        return []
    catalog = {skill.skill_id for skill in skills}
    affected = {
        case.id: sorted(set(case.expected_skills) - catalog)
        for case in cases
        if set(case.expected_skills) - catalog
    }
    if not affected:
        return []
    missing = sorted({skill_id for ids in affected.values() for skill_id in ids})
    sample = ", ".join(sorted(affected)[:3])
    return [
        f"{len(affected)}/{len(cases)} 道题的 gold 指向 catalog 里没有的 skill："
        f"{missing}（例如 {sample}）。这些题没有正确答案可选，指标必然偏低。\n"
        "    → 如果这是 No-Skill 基线，符合预期；否则把它们补进 skills.include，"
        "或检查 skills.exclude 是不是多剔了。"
    ]


def disabled_skill_warnings(skills: list[SkillMeta]) -> list[str]:
    """作者停用的 skill 照常评测，但不能不吭声（AGENTS.md §7.3c）。"""
    disabled = sorted(skill.skill_id for skill in skills if skill.disabled)
    if not disabled:
        return []
    return [
        f"catalog 里有被作者停用的 skill（frontmatter `disable: true`）：{disabled} —— "
        "本次 routing-only 测的是『仍把它暴露给模型』的 catalog，不等同于 Agent 实际加载集。"
        "如果你的生产 agent 遵循 disable，它通常不会加载这些 skill。"
        "若要模拟生产 catalog，请用 skills.exclude 排除它并复核相关 gold；本工具不会替你静默排除。"
    ]


def skill_version_drift_errors(
    suite: dict[str, Any],
    skills: list[SkillMeta],
    outputs_root: Path,
) -> list[str]:
    """Reject reusing a version label for content that historical runs saw differently.

    A run archive preserves the old bytes, so it remains reproducible.  Reusing
    ``skill@v2`` for new bytes still makes human-facing comparisons ambiguous:
    two reports now say "v2" while referring to different inputs.  P3's contract
    is stricter: keep the old version immutable and create ``vN+1``.

    Only snapshots that used the same configured skill root are compared.  This
    avoids treating a deliberate migration from an old ``skills/`` tree to
    ``subjects/`` as mutation of the same version namespace.
    """
    if not outputs_root.exists() or not skills:
        return []

    current_root = Path(suite["skills"]["dir"]).as_posix()
    current = {
        skill.skill_id: (skill.version or "v1", skill.content_hash)
        for skill in skills
    }
    drifted: dict[tuple[str, str, str, str], list[str]] = {}
    for snapshot_path in outputs_root.rglob("config.snapshot.yaml"):
        try:
            snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        historical_suite = snapshot.get("suite") or {}
        historical_spec = historical_suite.get("skills") or {}
        if Path(str(historical_spec.get("dir", ""))).as_posix() != current_root:
            continue
        historical_hashes = snapshot.get("skills") or {}
        # New snapshots persist the fully resolved version for every catalog
        # entry.  Fall back to the old suite-only representation so archives
        # created before that field was added remain useful.
        historical_versions = (
            snapshot.get("skill_versions")
            or historical_spec.get("versions")
            or {}
        )
        for skill_id, (version, content_hash) in current.items():
            old_hash = historical_hashes.get(skill_id)
            old_version = historical_versions.get(skill_id, "v1")
            if old_hash and old_version == version and old_hash != content_hash:
                key = (skill_id, version, old_hash, content_hash)
                drifted.setdefault(key, []).append(str(snapshot_path.parent))

    errors = []
    for (skill_id, version, old_hash, new_hash), run_dirs in sorted(drifted.items()):
        examples = ", ".join(run_dirs[:2])
        errors.append(
            f"{skill_id}@{version} 的 content_hash 与历史 run 不同"
            f"（历史 {old_hash}，当前 {new_hash}；例如 {examples}）。"
            "旧版本必须保持不可变：请把当前内容保存为新版本目录（如 v3），"
            "并在 suite.skills.versions 中钉住新版本。"
        )
    return errors


def endpoint_health(model: dict[str, Any]) -> tuple[bool, str]:
    """DNS-only model endpoint probe: no completion, credential, or cost.

    It catches the ordinary-user failure mode where every planned request would
    fail because the configured API host cannot resolve.  Provider-default
    endpoints cannot be inferred safely, so those remain an explicit skip.
    """
    base_env = model.get("api_base_env")
    endpoint = os.environ.get(base_env or "")
    if not base_env or not endpoint:
        return True, "endpoint DNS probe skipped (provider default or API base env is unset)"
    parsed = urlparse(endpoint)
    host = parsed.hostname
    if not host:
        return False, f"{base_env} is not a valid URL; set it to the provider API base URL"
    try:
        socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, (
            f"cannot resolve API host {host!r} from {base_env}; check network/DNS and that "
            f"{base_env} is correct, then rerun `pipeline plan --healthcheck`"
        )
    except OSError as error:
        return False, f"cannot inspect API host {host!r}: {error}; check local network settings"
    return True, f"API host {host!r} resolves (DNS-only; no model request was made)"


def build_egress_manifest(
    suite: dict[str, Any],
    cases: list[RoutingCase],
    models: list[PlannedModel],
    *,
    mock: bool,
) -> dict[str, Any]:
    """Describe external data movement without exposing prompt or secret values."""
    if mock:
        payload = {
            "mode": "synthetic_mock",
            "approval_required": False,
            "planned_requests": 0,
            "destinations": [],
            "payload_categories": [],
            "never_in_model_payload": ["API key values", "local output archives"],
        }
    else:
        destinations = []
        for model in models:
            endpoint = os.environ.get(model.api_base_env or "")
            host = urlparse(endpoint).hostname if endpoint else None
            destinations.append({
                "model_id": model.id,
                "endpoint": host or "provider-default/runtime-managed",
            })
        categories = ["dataset prompt and declared conversation context"]
        mode = suite["skills"]["mode"]
        if mode == "routing_only":
            categories.append("selected skill metadata only (name/description/triggers/exclusions)")
        elif mode == "full":
            categories.append("selected SKILL.md body")
            if any(getattr(case, "all_files", getattr(case, "files", [])) for case in cases):
                categories.append("declared case input files")
        if suite.get("tools"):
            categories.append("declared tool catalog")
        payload = {
            "mode": "real_external_model",
            "approval_required": True,
            "planned_requests": (
                sum(getattr(case, "turn_count", 1) for case in cases)
                * suite["repeats"]
                * len(models)
            ),
            "destinations": destinations,
            "payload_categories": categories,
            "never_in_model_payload": ["API key values", "local output archives"],
        }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["manifest_hash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return payload


def _healthcheck_detail(subject: str, detail: str | None, runtime: str) -> str:
    """Explain known sandbox-induced false negatives before the user debugs the wrong layer."""
    text = detail or runtime
    if (
        subject == "runtime"
        and "unable to open database file" in text
        and "openclaw" in runtime.lower()
    ):
        return (
            text
            + " [可能是沙箱限制导致 OpenClaw profile/db 无法写入；若同一 suite 在脱沙箱运行时"
            " 正常，请把这类失败视为环境权限问题，而不是 runtime 本身损坏。]"
        )
    return text


def build_plan(
    suite_path: str | Path,
    *,
    mock: bool = False,
    check_health: bool = False,
    execution_id: str | None = None,
) -> PipelinePlan:
    """Validate and expand a suite without writing outputs or calling a model.

    ``check_health`` may probe the selected runtime/environment locally.  It
    never performs a model completion and does not affect the planned hashes.
    """
    load_dotenv(ROOT / ".env")
    raw_path = Path(suite_path)
    path = raw_path if raw_path.is_absolute() else ROOT / raw_path
    try:
        suite = resolve_suite_references(load_suite(path))
    except ValidationError as error:
        raise SystemExit(format_suite_validation_error(path, error)) from error
    except ValueError as error:
        raise SystemExit(f"suite 环境引用无效：{error}") from error
    dataset_path = ROOT / suite["dataset"]
    cases = load_cases(dataset_path)
    skills = resolve_skills(suite)
    dataset_hash = file_hash(dataset_path)
    runtime = build_runtime(suite, cases, mock)
    environment = build_environment(suite)

    # 版本不需要告警：没钉就取 v1，加多少新版本都不会改变一份旧 suite 的含义
    # （§7.3b）。plan 的 catalog 行会把实际解析到的 `skill@version` 逐个打出来。
    warnings = disabled_skill_warnings(skills)
    warnings.extend(
        unreachable_gold_warnings(cases, skills, suite["skills"]["mode"])
    )
    warnings.extend(gate_coverage_warnings(cases, suite["scoring"].get("gate", {})))

    blocked: list[str] = []
    if dataset_review_status(dataset_path) == "DRAFT":
        blocked.append(
            "dataset review_status is DRAFT: review REVIEW.md and the gold labels, then "
            "change the dataset header to APPROVED before spending model budget"
        )
    missing_files = missing_case_files(cases)
    if missing_files:
        blocked.append(
            "case 声明的输入文件不存在（相对仓库根；素材放 evals/fixtures/）："
            + ", ".join(missing_files)
        )
    blocked.extend(skill_version_drift_errors(suite, skills, ROOT / "outputs"))
    caps = runtime.capabilities()
    mode = suite["skills"]["mode"]
    has_multi_turn = any(case.turn_count > 1 for case in cases)
    if mode not in caps.skill_modes:
        blocked.append(
            f"runtime={runtime.name} does not support skill_mode={mode} "
            f"(supports {caps.skill_modes})"
        )
    if suite.get("tools") and not caps.tools:
        blocked.append(f"runtime={runtime.name} does not support declared tools")
    if has_multi_turn and mode != "full":
        blocked.append("multi-turn cases require skills.mode=full")
    if has_multi_turn and not caps.multi_turn:
        blocked.append(f"runtime={runtime.name} does not support multi_turn")

    health: dict[str, dict[str, Any]] = {}
    if check_health:
        for subject, result in (("environment", environment.healthcheck()),
                                ("runtime", runtime.healthcheck(environment))):
            detail = _healthcheck_detail(subject, result.detail, result.runtime)
            health[subject] = {
                "checked": True,
                "healthy": result.healthy,
                "detail": detail,
                "version": result.version,
            }
            if not result.healthy:
                blocked.append(f"{subject} healthcheck failed: {detail}")
    else:
        health = {
            "environment": {"checked": False},
            "runtime": {"checked": False},
        }

    source_models = [{"id": "mock"}] if mock else suite["models"]
    runnable_models: list[dict[str, Any]] = []
    models: list[PlannedModel] = []
    unusable: list[str] = []
    for model in source_models:
        credential = credential_state(model.get("api_key_env"))
        if credential in {"missing", "placeholder"}:
            unusable.append(f"{model['id']}:{credential}")
        else:
            runnable_models.append(model)

    if check_health:
        for model in runnable_models:
            healthy, detail = endpoint_health(model)
            label = f"model:{model['id']}"
            health[label] = {"checked": True, "healthy": healthy, "detail": detail,
                             "version": None}
            if not healthy:
                blocked.append(f"{label} healthcheck failed: {detail}")

    # Use the exact same deterministic task count and config hash functions as
    # the runner.  A plan therefore shows the actual result directories rather
    # than an approximation that can drift from execution.
    matrix = build_matrix(
        suite_id=suite["suite_id"],
        cases=cases,
        models=runnable_models,
        repeats=suite["repeats"],
        execution_id="preflight",
    ) if runnable_models else []
    tasks_by_model = {str(model["id"]): 0 for model in runnable_models}
    for task in matrix:
        tasks_by_model[task.model_id] += 1

    for model in source_models:
        key_env = model.get("api_key_env")
        credential = credential_state(key_env)
        planned = PlannedModel(
                id=str(model["id"]),
                model=model.get("model"),
                params=dict(model.get("params") or {}),
                api_base_env=model.get("api_base_env"),
                api_key_env=key_env,
                credential=credential,
                result_dir=(
                    _display_path(run_dir(suite, str(model["id"]), execution_id))
                    if execution_id else
                    _display_path(run_dir(suite, str(model["id"]))) + "/<execution-id>"
                ),
                config_hash=config_hash(
                    suite, model, mock, runtime.fingerprint(), skills, dataset_hash,
                    environment.fingerprint(),
                ),
                tasks=tasks_by_model.get(str(model["id"]), 0),
            )
        models.append(planned)
        if execution_id and (ROOT / planned.result_dir).exists():
            blocked.append(f"execution archive already exists: {planned.result_dir}")
    if not runnable_models:
        blocked.append(
            "no runnable model: no usable API key (" + ", ".join(unusable) + ")\n"
            "  → `cp .env.example .env` 之后还要把占位符换成真 key；"
            "变量名对不对照 suite 里的 api_key_env。"
        )
    egress = build_egress_manifest(suite, cases, models, mock=mock)

    return PipelinePlan(
        suite_path=_display_path(path),
        suite_id=suite["suite_id"],
        suite_version=suite["suite_version"],
        planned_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        execution_id=execution_id,
        description=suite.get("description", ""),
        dataset=_display_path(dataset_path),
        dataset_hash=dataset_hash,
        cases=len(cases),
        turns=sum(case.turn_count for case in cases),
        skill_mode=mode,
        skill_dir=suite["skills"]["dir"],
        skill_cfg=suite["skills"]["cfg"],
        target_subjects=list(suite["skills"]["target"]),
        pipeline_mode=suite["pipeline"]["mode"],
        iteration=suite["pipeline"]["iteration"],
        pinned_versions=dict(suite["skills"].get("versions") or {}),
        included_skills=list(suite["skills"].get("include") or []),
        excluded_skills=list(suite["skills"].get("exclude") or []),
        selected_skills=[skill.skill_id for skill in skills],
        skill_versions={skill.skill_id: skill.version for skill in skills},
        skill_hashes={skill.skill_id: skill.content_hash for skill in skills},
        runtime=runtime.name,
        runtime_options=dict(suite.get("runtime_options") or {}),
        routing_input=dict(suite.get("routing_input") or {}),
        environment={
            "backend": environment.name,
            "capabilities": environment.capabilities(),
            "fingerprint": environment.fingerprint(),
        },
        health=health,
        repeats=suite["repeats"],
        parallelism=suite["parallelism"],
        timeout_seconds=suite["timeout_seconds"],
        tools=list(suite.get("tools") or []),
        scoring_metrics=list(suite["scoring"]["metrics"]),
        gate=dict(suite["scoring"].get("gate") or {}),
        stages=_stage_plan(suite),
        models=models,
        blocked_reasons=blocked,
        warnings=warnings,
        egress=egress,
        mock=mock,
    )


def render_plan(plan: PipelinePlan) -> str:
    """Render one compact confirmation page; never print credential values."""
    lines = [
        "Evaluation pipeline preflight (read-only; no model request was made)",
        (
            "RUN MODE: SYNTHETIC MOCK — validates the pipeline only; "
            "scores are not a skill-quality verdict"
            if plan.mock else
            "RUN MODE: REAL MODEL — repository evaluation inputs may leave this machine"
        ),
        f"suite: {plan.suite_path}  [{plan.suite_id} v{plan.suite_version}]",
        f"local time: {plan.planned_at}  (archive IDs below use this local timezone)",
        f"execution archive: {plan.execution_id or 'new ID assigned only when pipeline run starts'}",
    ]
    if plan.description:
        lines.append(f"purpose: {plan.description}")
    lines.extend([
        "",
        f"dataset: {plan.dataset}  | {plan.cases} cases / {plan.turns} turns | "
        f"{plan.dataset_hash}",
        f"skills: mode={plan.skill_mode} cfg={plan.skill_cfg} dir={plan.skill_dir}",
        f"  target/owner: {', '.join(plan.target_subjects)}",
        f"operation: {plan.pipeline_mode}; improvement iteration label={plan.iteration}",
        f"  catalog ({len(plan.selected_skills)}): "
        + (", ".join(f"{s}@{plan.skill_versions[s]}" for s in plan.selected_skills) or "(none)"),
        f"  content hashes: {plan.skill_hashes or '(none)'}",
        f"  pinned versions={plan.pinned_versions or '(none, 全取 v1 基线)'}  "
        f"include={plan.included_skills or '(all)'} exclude={plan.excluded_skills or '(none)'}",
        f"runtime: {plan.runtime}  options={plan.runtime_options or {}}",
        f"routing input: {plan.routing_input or {}}",
        f"environment: {plan.environment['backend']}  {plan.environment['fingerprint']}",
        f"workload: {plan.repeats} repeats × {plan.cases} conversations / "
        f"{plan.turns} turns; parallelism={plan.parallelism}; "
        f"timeout={plan.timeout_seconds}s per turn; "
        f"tools={plan.tools or '(none)'}",
        f"scoring: metrics={plan.scoring_metrics}; gate={plan.gate or '(none)'}",
        "",
        "models and outputs (results always live under project outputs/, not inside a skill source):",
    ])
    if plan.health["runtime"].get("checked"):
        lines.append(
            "preflight checks: local runtime/environment + endpoint DNS only; "
            "provider auth/model availability/quota were NOT verified"
        )
        lines.append(
            "healthcheck: " + "; ".join(
                f"{name}={'healthy' if value['healthy'] else 'FAILED'}"
                + (f" ({value.get('detail')})" if value.get("detail") else "")
                for name, value in plan.health.items()
            )
        )
    else:
        lines.append(
            "healthcheck: not run (add `pipeline plan --healthcheck`; real `pipeline run` "
            "performs this non-invasive check automatically)"
        )
    for model in plan.models:
        api = model.api_key_env or "(runtime-managed)"
        lines.append(
            f"  - {model.id}: model={model.model or '(runtime-managed)'} "
            f"params={model.params or {}} key={api}:{model.credential}\n"
            f"    {model.tasks} tasks → {model.result_dir}\n"
            f"    config_hash={model.config_hash}"
        )
    lines.append("\nexternal data movement:")
    if plan.mock:
        lines.append("  none — mock never calls a model endpoint")
    else:
        lines.append(
            f"  {plan.egress['planned_requests']} planned requests | "
            f"manifest={plan.egress['manifest_hash']}"
        )
        for destination in plan.egress["destinations"]:
            lines.append(f"  - {destination['model_id']} → {destination['endpoint']}")
        lines.append("  payload:")
        lines.extend(f"    - {item}" for item in plan.egress["payload_categories"])
        lines.append(
            "  excluded from model payload: "
            + ", ".join(plan.egress["never_in_model_payload"])
        )
    lines.append("\nmodules to run:")
    for stage in plan.stages:
        lines.append(f"  {stage['name']}: {stage['module']} — {stage['detail']}")
    if plan.warnings:
        lines.append("\n⚠️ WARNINGS（不拦运行，但会影响结论怎么读）:")
        lines.extend(f"  - {warning}" for warning in plan.warnings)
    if plan.blocked_reasons:
        lines.append("\nBLOCKED:")
        lines.extend(f"  - {reason}" for reason in plan.blocked_reasons)
    else:
        lines.append("\nREADY. Execute only after confirming this plan:")
        command = f"  .venv/bin/python -m pipeline run --suite {plan.suite_path} --confirm"
        if not plan.mock:
            command += " --confirm-egress"
        else:
            command += " --mock"
        lines.append(command)
    return "\n".join(lines)
