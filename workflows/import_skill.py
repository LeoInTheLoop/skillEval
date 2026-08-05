"""Bridge an installed skill source directory into a versioned subjects snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import frontmatter

ROOT = Path(__file__).parent.parent
IGNORED_IMPORT_NAMES = {".git", ".env", "__pycache__", ".DS_Store"}
IMPORT_META_NAME = "_meta.json"


def snapshot_content_manifest(root: Path) -> dict[str, str]:
    """Hash files that become evaluated snapshot content.

    The root ``_meta.json`` is transport/import metadata: SkillHub may provide
    one and skillEval replaces it with its own provenance record.  It must
    therefore be excluded symmetrically for both source and destination.
    Nested files with that name remain ordinary skill content.
    """
    manifest: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        if any(part in IGNORED_IMPORT_NAMES for part in relative.parts):
            continue
        if relative.as_posix() == IMPORT_META_NAME:
            continue
        manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


def _manifest_hash(manifest: dict[str, str]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def resolve_source(path: str | Path) -> Path:
    source = Path(path).expanduser().resolve()
    if source.is_file():
        if source.name != "SKILL.md":
            raise ValueError(f"{source} 不是 skill 目录，也不是 SKILL.md")
        source = source.parent
    if not (source / "SKILL.md").is_file():
        raise ValueError(f"{source} 下没有 SKILL.md")
    return source


def skill_id_for(source: Path) -> str:
    meta = frontmatter.loads((source / "SKILL.md").read_text(encoding="utf-8")).metadata
    return str(meta.get("name") or source.name)


def public_source_label(source: Path) -> str:
    """Record useful provenance without persisting a user's absolute path."""
    try:
        return source.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return f"external:{source.name}"


def import_snapshot(
    source_path: str | Path,
    *,
    dest_root: str | Path = "subjects",
    version: str = "v1",
) -> Path:
    """Copy one upstream installation into an immutable evaluation snapshot."""
    source = resolve_source(source_path)
    skill_id = skill_id_for(source)
    root = Path(dest_root).expanduser()
    if not root.is_absolute():
        root = (ROOT / root).resolve()
    destination = root / skill_id / version
    if destination.exists():
        raise FileExistsError(f"拒绝覆盖已有被测快照：{destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*sorted(IGNORED_IMPORT_NAMES)),
    )
    meta = {
        "imported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": public_source_label(source),
        "skill_id": skill_id,
        "version_dir": version,
        "snapshot_content_hash": _manifest_hash(snapshot_content_manifest(source)),
    }
    source_meta = source / IMPORT_META_NAME
    if source_meta.is_file():
        meta["upstream_meta_sha256"] = "sha256:" + hashlib.sha256(
            source_meta.read_bytes()
        ).hexdigest()
    (destination / IMPORT_META_NAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 SkillHub/agent 的安装源目录（例如 installed_skills/<slug>/）桥接到 subjects/<skill-id>/vN/，生成可复现的被测快照"
    )
    parser.add_argument("--source", required=True, help="安装源目录，或该目录里的 SKILL.md")
    parser.add_argument("--dest-root", default="subjects", help="被测快照根目录；默认导入到项目 subjects/")
    parser.add_argument("--version", default="v1", help="导入后的版本目录名，如 v1 / v2")
    args = parser.parse_args()

    destination = import_snapshot(
        args.source,
        dest_root=args.dest_root,
        version=args.version,
    )
    print(f"已导入被测快照：{destination}")
    print("下一步：让 suite 的 skills.dir 指向 subjects；真正参与评测的是 subjects/，不是原始安装目录。")


if __name__ == "__main__":
    main()
