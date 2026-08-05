"""Guided first-run flow: upstream skill → immutable snapshot → review draft."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import frontmatter
from dotenv import load_dotenv

from contracts import VERSION_DIR
from pipeline.plan import credential_state
from workflows.gen_cases import (
    call_litellm,
    draft_dataset_name,
    generate_case_draft,
    resolve_skill_source,
)
from workflows.import_skill import (
    ROOT,
    import_snapshot,
    resolve_source,
    skill_id_for,
    snapshot_content_manifest,
)


def _sha_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def snapshot_state(source: Path, destination: Path) -> tuple[str, str | None]:
    """Return new/reusable/conflict without mutating either directory."""
    if not destination.exists():
        return "new", None
    source_files = snapshot_content_manifest(source)
    destination_files = snapshot_content_manifest(destination)
    if source_files == destination_files:
        return "reusable", None
    return (
        "conflict",
        f"目标快照已存在但内容不同：{destination}。"
        "拒绝覆盖；请选择新的 --version，或显式使用另一份 source。",
    )


@dataclass(frozen=True)
class InitPlan:
    source: str
    skill_id: str
    version: str
    snapshot_destination: str
    snapshot_state: str
    draft_output: str
    case_count: int
    required_review: bool
    model_id: str
    model: str
    credential: str
    endpoint: str
    acceptance_hash: str
    acceptance_characters: int
    egress: dict[str, Any]
    blocked_reasons: list[str]

    @property
    def runnable(self) -> bool:
        return not self.blocked_reasons

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["runnable"] = self.runnable
        return result


def build_init_plan(
    *,
    source_path: str | Path,
    acceptance: str,
    dest_root: str | Path = "subjects",
    version: str = "v1",
    output_dir: str | Path | None = None,
    count: int = 10,
    model_id: str = "qwen3.7-max-2026-05-17",
    model: str = "openai/qwen3.7-max-2026-05-17",
    api_base_env: str = "DASHSCOPE_BASE_URL",
    api_key_env: str = "DASHSCOPE_API_KEY",
) -> InitPlan:
    """Resolve the entire onboarding plan without writing files or calling a model."""
    load_dotenv(ROOT / ".env")
    source = resolve_source(source_path)
    skill_id = skill_id_for(source)
    source_metadata = frontmatter.loads(
        (source / "SKILL.md").read_text(encoding="utf-8")
    ).metadata
    root = Path(dest_root).expanduser()
    if not root.is_absolute():
        root = (ROOT / root).resolve()
    destination = root / skill_id / version
    state, conflict = snapshot_state(source, destination)
    scope = re.sub(r"[^A-Za-z0-9_.-]", "-", skill_id)
    draft = (
        Path(output_dir).expanduser()
        if output_dir is not None
        else ROOT / "evals" / "drafts" / scope
    ).resolve()

    blocked = []
    if not VERSION_DIR.fullmatch(version):
        blocked.append(f"版本目录名必须是 v1 / v2 / v1.1 形式，当前是 {version!r}")
    if not acceptance.strip():
        blocked.append("业务目标与验收标准不能为空")
    if not source_metadata.get("description"):
        blocked.append(
            "目标 skill 缺少 frontmatter.description；路由出题没有能力边界可依据"
        )
    if not 3 <= count <= 30:
        blocked.append(f"题量必须在 3–30，当前是 {count}")
    if conflict:
        blocked.append(conflict)
    collisions = [
        path
        for path in (
            draft / draft_dataset_name(scope),
            draft / "suite.yaml",
            draft / "REVIEW.md",
        )
        if path.exists()
    ]
    if collisions:
        blocked.append(f"草稿已存在，拒绝覆盖：{collisions}")

    credential = credential_state(api_key_env)
    if credential in {"missing", "placeholder"}:
        blocked.append(
            f"生成模型凭据 {api_key_env}:{credential}；请先配置真 key。"
            "init 会生成真实 gold 草稿，不能用 synthetic mock 代替。"
        )
    endpoint_value = os.environ.get(api_base_env, "")
    endpoint = urlparse(endpoint_value).hostname if endpoint_value else None
    # 两次调用：① 生成题目 ② 拿生成出来的 rej 题面回去盲判一遍（gen_cases 的交叉复审）。
    # 第二次不重发验收标准，只发 catalog metadata 和刚生成的题面。
    egress_base = {
        "approval_required": True,
        "planned_requests": 2,
        "destination": endpoint or "provider-default",
        "payload_categories": [
            "target skill routing metadata (name/description/triggers/exclusions)",
            "business goal and acceptance criteria",
            "generated no-skill case prompts, sent back for blind gold cross-review",
        ],
        "not_sent": ["SKILL.md body", "API key value", "local output archives"],
    }
    canonical = json.dumps(
        egress_base, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    egress = {
        **egress_base,
        "manifest_hash": "sha256:"
        + hashlib.sha256(canonical.encode()).hexdigest()[:16],
    }
    return InitPlan(
        source=str(source),
        skill_id=skill_id,
        version=version,
        snapshot_destination=str(destination),
        snapshot_state=state,
        draft_output=str(draft),
        case_count=count,
        required_review=True,
        model_id=model_id,
        model=model,
        credential=credential,
        endpoint=endpoint or "provider-default",
        acceptance_hash=_sha_text(acceptance),
        acceptance_characters=len(acceptance),
        egress=egress,
        blocked_reasons=blocked,
    )


def render_init_plan(plan: InitPlan) -> str:
    lines = [
        "Skill evaluation initialization plan (read-only; no files written, no model call)",
        f"source: {plan.source}",
        f"snapshot: {plan.skill_id}@{plan.version} → {plan.snapshot_destination}",
        f"snapshot action: {plan.snapshot_state}",
        f"draft: {plan.case_count} routing cases → {plan.draft_output}",
        f"acceptance: {plan.acceptance_characters} chars | {plan.acceptance_hash}",
        f"generator: {plan.model_id} ({plan.model}) | credential={plan.credential}",
        "",
        "external data movement:",
        f"  {plan.egress['planned_requests']} requests → {plan.endpoint} "
        f"| manifest={plan.egress['manifest_hash']}",
        *[f"  - send: {item}" for item in plan.egress["payload_categories"]],
        f"  - do not send: {', '.join(plan.egress['not_sent'])}",
        "",
        "human review gate:",
        "  generated dataset remains DRAFT; pipeline plan/run will reject it until reviewed",
    ]
    if plan.blocked_reasons:
        lines.append("\nBLOCKED:")
        lines.extend(f"  - {reason}" for reason in plan.blocked_reasons)
    else:
        lines.extend([
            "\nREADY. Execute only after confirming both local writes and external data movement:",
            "  add --confirm --confirm-egress to this pipeline init command",
        ])
    return "\n".join(lines)


def execute_init(
    plan: InitPlan,
    *,
    acceptance: str,
    api_base_env: str,
    api_key_env: str,
    completion: Callable[..., str] | None = None,
) -> tuple[Path, Path]:
    """Execute an already reviewed plan and stop at the dataset review gate."""
    if not plan.runnable:
        raise ValueError("init plan is blocked: " + "; ".join(plan.blocked_reasons))
    source = Path(plan.source)
    destination = Path(plan.snapshot_destination)
    if plan.snapshot_state == "new":
        destination = import_snapshot(
            source,
            dest_root=destination.parent.parent,
            version=plan.version,
        )
        print(f"已创建不可变被测快照：{destination}")
    else:
        print(f"复用内容一致的被测快照：{destination}")

    catalog_root, skills, targets = resolve_skill_source(
        destination,
        include_neighbors=False,
    )
    try:
        dataset, suite, required_types = generate_case_draft(
            catalog_root=catalog_root,
            skills=skills,
            target_skill_ids=targets,
            acceptance=acceptance,
            count=plan.case_count,
            model_id=plan.model_id,
            model=plan.model,
            api_base_env=api_base_env,
            api_key_env=api_key_env,
            output_dir=Path(plan.draft_output),
            completion=completion or call_litellm,
        )
    except Exception as error:
        raise RuntimeError(
            f"被测快照已就绪，但题目生成失败：{error}\n"
            "可修复模型/网络后重跑同一 init 命令；内容一致的 snapshot 会被安全复用。"
        ) from error
    print(
        f"已生成待人工审核草稿：{dataset}\n"
        f"suite 草稿：{suite}\n"
        f"题型要求：{list(required_types)}\n"
        "已停止在人审门：先审核 REVIEW.md 和 gold；DRAFT 未批准前不能 plan/run。"
    )
    return dataset, suite
