"""local/docker 共用的 workspace 与 skill staging 物化逻辑。"""
from __future__ import annotations

import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from contracts import InvocationRequest

_READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH


def stage_input_files(request: InvocationRequest, workspace: Path) -> list[Path]:
    """把 case 声明的输入素材复制进 workspace，并设为只读（AGENTS.md §11.4）。

    按 basename 落在 workspace 根：题面里用户怎么称呼这个文件，模型就能怎么找到它。
    多个 case 共用同一份 fixture 时各复制一份，互不影响（§11.5「只读输入不得被修改」）。
    """
    staged: list[Path] = []
    for raw in request.input_files:
        source = Path(raw).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"case 声明的输入文件不存在：{source}")
        target = workspace / source.name
        if target.exists():
            raise FileExistsError(f"输入文件重名，无法同时物化：{source.name}")
        shutil.copy2(source, target)
        target.chmod(_READ_ONLY)
        staged.append(target)
    return staged


@contextmanager
def materialized_files(request: InvocationRequest) -> Iterator[tuple[Path, Path]]:
    """每个 request 独立目录；skill 复制而非软链，保证 overlay 后内容精确可见。"""
    root = Path(tempfile.mkdtemp(prefix="skilleval-env-"))
    workspace = root / "workspace"
    skills = root / "skills"
    workspace.mkdir()
    skills.mkdir()
    try:
        if request.skill_mode == "full":
            for skill in request.skills:
                source = Path(skill.source_path).parent
                if source.is_dir():
                    shutil.copytree(source, skills / skill.skill_id, symlinks=False)
        stage_input_files(request, workspace)
        yield workspace, skills
    finally:
        # 只读文件在某些平台上会挡住 rmtree，先把权限放开再删。
        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)
        shutil.rmtree(root, ignore_errors=True)
