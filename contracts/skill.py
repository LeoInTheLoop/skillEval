"""N1：Skill 契约与加载器（AGENTS.md §7）。

routing-only 模式只暴露 frontmatter，绝不读正文 —— 这是 §7.3 的硬约束。

被测 skill 的布局是 `<root>/<skill-id>/<version>/SKILL.md`（§7.3b）：一个 skill
一个目录，版本是它下面的平级子目录。选版本因此只是「挑一个子目录」，不需要
把改过的副本另放一个 root 再按目录覆盖。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import frontmatter
from pydantic import BaseModel, ConfigDict, Field

# 版本目录名：v1、v2、v1.1 都认。skill 目录下只有匹配这个的才算一个版本。
VERSION_DIR = re.compile(r"^v\d+(?:\.\d+)*$")


class SkillMeta(BaseModel):
    """routing-only 模式下暴露给模型的 skill 元数据（不含正文）。"""
    model_config = ConfigDict(extra="ignore")

    skill_id: str
    name: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    content_hash: str
    # 作者在 frontmatter 里停用了这个 skill（`disable: true` / `enabled: false`）。
    # 我们照常评测（§7.3c 不替用户做决定），但必须告警 —— 否则评测 catalog 与
    # Agent 实际加载的 catalog 不一致，而用户看不出来。不进模型输入。
    disabled: bool = Field(default=False, exclude=True)
    source_path: str = Field(exclude=True)
    # 本次取的是哪一版（目录名，如 `v2`）。不进模型输入也不进 config_hash
    # （内容变化由 content_hash 表达），只用于让 plan/快照说清「跑的是哪版」。
    version: str = Field(default="", exclude=True)


def _disabled(meta: dict) -> bool:
    """两种写法都认：skillhub 用 `disable`，部分 Agent 用 `enabled`。"""
    if meta.get("disable") is True:
        return True
    return meta.get("enabled") is False


def _skill_name(meta: dict, fallback: str) -> str:
    """frontmatter 的 name 不是字符串时退回目录名。

    YAML 1.1 会把 `name: on` / `no` / `yes` 解析成布尔值 —— 直接塞进契约会
    在加载阶段抛 ValidationError，报错还指向 pydantic，看不出是 skill 写法的问题。
    """
    name = meta.get("name")
    return name if isinstance(name, str) and name else fallback


def _read_skill(md: Path) -> SkillMeta:
    text = md.read_text(encoding="utf-8")
    meta = frontmatter.loads(text).metadata
    version = md.parent.name if VERSION_DIR.fullmatch(md.parent.name) else ""
    # 退回目录名时要退到 **skill 目录**，不是版本目录 —— 否则 id 会变成 "v2"。
    name = _skill_name(meta, md.parent.parent.name if version else md.parent.name)
    return SkillMeta(
        skill_id=name,
        name=name,
        description=meta.get("description", ""),
        triggers=meta.get("triggers") or [],
        exclusions=meta.get("exclusions") or [],
        content_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        disabled=_disabled(meta),
        source_path=str(md),
        version=version,
    )


def version_order(version: str) -> tuple[int, ...]:
    """`v10` 排在 `v9` 之后 —— 按数字段比，不是按字符串比。"""
    return tuple(int(part) for part in version[1:].split("."))


def discover_skills(skills_dir: str | Path) -> dict[str, dict[str, SkillMeta]]:
    """扫 `<root>/<skill-id>/<vN>/SKILL.md`，返回 {skill_id: {version: meta}}。

    只 parse frontmatter，绝不读正文（§7.3）。skill_id 取 frontmatter 的 `name`，
    不取目录名：上游 slug 常和 skill 自称的名字不一致，而 suite 里的
    include/exclude/versions 全按 skill_id 写。
    """
    found: dict[str, dict[str, SkillMeta]] = {}
    for md in sorted(Path(skills_dir).glob("*/*/SKILL.md")):
        if not VERSION_DIR.fullmatch(md.parent.name):
            continue
        skill = _read_skill(md)
        clash = found.setdefault(skill.skill_id, {}).get(skill.version)
        if clash:
            raise ValueError(
                f"两个目录声明了同一个 skill 的同一版本 "
                f"{skill.skill_id}@{skill.version}：{clash.source_path} 与 {md}；"
                "frontmatter 的 name 决定 skill_id，改掉其中一个"
            )
        found[skill.skill_id][skill.version] = skill
    return found


def load_skills(
    skills_dir: str | Path,
    versions: dict[str, str] | None = None,
    exclude: list[str] | tuple[str, ...] = (),
    include: list[str] | tuple[str, ...] = (),
) -> list[SkillMeta]:
    """解析出本次 catalog：每个 skill 取一个版本。

    content_hash 覆盖**整个文件**（含正文）：正文改了 hash 也要变，
    否则 full eval 的 provenance 会失真（§7.4）。

    versions     按 skill_id 钉版本，如 `{"pdf": "v2"}`。**没钉的取版本号最小的
                 那一版**，不是最新的：evaluation 的第一要求是基线不漂移，日后给
                 某个 distractor 加一版 v3，不该悄悄改变一堆历史 suite 的含义。
                 V1/V2 对照因此是「V1 套件一行不写，V2 套件钉一行」（§7.3b）。
    exclude      按 skill_id 剔除。No-Skill 基线（§18.3「目标 skill 完全不可见」）
                 就是把目标 skill 排除掉，其余条件保持一致。
    """
    catalog = discover_skills(skills_dir)
    if not catalog:
        raise ValueError(
            f"{skills_dir} 下没有 <skill-id>/<vN>/SKILL.md —— 被测 skill 的布局是"
            "「一个 skill 一个目录，版本是它下面的 v1/ v2/ 子目录」（§7.3b）"
        )

    pinned = dict(versions or {})
    unknown = sorted(set(pinned) - set(catalog))
    if unknown:
        raise ValueError(
            f"skills.versions 里的 skill 不在 {skills_dir} 下：{unknown}；"
            f"可选：{sorted(catalog)}"
        )

    skills: dict[str, SkillMeta] = {}
    for skill_id, by_version in catalog.items():
        want = pinned.get(skill_id) or min(by_version, key=version_order)
        if want not in by_version:
            raise ValueError(
                f"{skill_id} 没有版本 {want!r}；已有："
                f"{sorted(by_version, key=version_order)}"
            )
        skills[skill_id] = by_version[want]

    requested = tuple(include)
    if len(requested) != len(set(requested)):
        raise ValueError(f"include 里的 skill id 重复：{list(requested)}")
    missing_includes = sorted(set(requested) - set(skills))
    if missing_includes:
        raise ValueError(f"include 里的 skill 不存在：{missing_includes}；可选：{sorted(skills)}")
    if requested:
        skills = {skill_id: skills[skill_id] for skill_id in requested}

    for skill_id in exclude:
        if skill_id not in skills:
            raise ValueError(f"exclude 里的 {skill_id!r} 不存在；可选：{sorted(skills)}")
        del skills[skill_id]

    return sorted(skills.values(), key=lambda s: s.skill_id)


def build_catalog(skills: list[SkillMeta]) -> str:
    """把 skill 元数据拼成给模型看的目录文本（routing-only 的全部输入）。"""
    lines = []
    for s in skills:
        trig = ("触发词: " + ", ".join(s.triggers)) if s.triggers else ""
        excl = ("排除: " + ", ".join(s.exclusions)) if s.exclusions else ""
        lines.append(f"- {s.skill_id}: {s.description} {trig} {excl}".strip())
    return "\n".join(lines)
