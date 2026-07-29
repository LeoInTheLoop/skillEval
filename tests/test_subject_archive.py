from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile

import pytest
import yaml

from pipeline.archive import (
    build_archive_plan,
    build_restore_plan,
    execute_archive,
    execute_restore,
)


def _write_subject(root: Path, subject: str) -> Path:
    skill = root / "subjects" / subject / "v1" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: {subject}\ndescription: {subject} capability\n---\nbody\n",
        encoding="utf-8",
    )
    return skill


def _write_dataset(
    root: Path,
    name: str,
    cases: list[dict],
) -> Path:
    path = root / "evals" / "datasets" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    return path


def _write_suite(root: Path, name: str, dataset: Path) -> Path:
    path = root / "evals" / "suites" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    targets = sorted(
        json.loads(dataset.read_text(encoding="utf-8").splitlines()[0]).get(
            "expected_skills", []
        )
    )
    path.write_text(
        yaml.safe_dump(
            {
                "suite_id": name.removesuffix(".yaml"),
                "dataset": dataset.relative_to(root).as_posix(),
                "skills": {
                    "dir": "subjects",
                    "target": targets,
                    "mode": "routing_only",
                    "cfg": "v1",
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def _write_run(root: Path, name: str, dataset: Path) -> Path:
    run = root / "outputs" / name / "execution-01"
    (run / "inputs").mkdir(parents=True)
    targets = sorted(
        json.loads(dataset.read_text(encoding="utf-8").splitlines()[0]).get(
            "expected_skills", []
        )
    )
    (run / "config.snapshot.yaml").write_text(
        yaml.safe_dump(
            {
                "suite": {
                    "dataset": dataset.relative_to(root).as_posix(),
                    "skills": {
                        "dir": "subjects",
                        "target": targets,
                        "mode": "routing_only",
                        "cfg": "v1",
                    },
                },
                "config_hash": "sha256:test",
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (run / "inputs" / "dataset.jsonl").write_bytes(dataset.read_bytes())
    (run / "runs.jsonl").write_text('{"case_id":"x"}\n', encoding="utf-8")
    (run / "scores.json").write_text('{"gate_pass":true}\n', encoding="utf-8")
    return run


def _workspace(root: Path) -> dict[str, Path]:
    alpha_skill = _write_subject(root, "alpha")
    beta_skill = _write_subject(root, "beta")
    fixture = root / "evals" / "fixtures" / "alpha.txt"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("fixture", encoding="utf-8")
    expected = root / "evals" / "expected" / "alpha-pos-01" / "answer.txt"
    expected.parent.mkdir(parents=True)
    expected.write_text("expected answer", encoding="utf-8")

    alpha_dataset = _write_dataset(
        root,
        "routing_alpha_v1.0.jsonl",
        [
            {
                "id": "alpha-pos-01",
                "prompt": "alpha",
                "expected_skills": ["alpha"],
                "files": ["evals/fixtures/alpha.txt"],
            }
        ],
    )
    shared_dataset = _write_dataset(
        root,
        "routing_shared_v1.0.jsonl",
        [
            {
                "id": "alpha+beta-multi-01",
                "prompt": "both",
                "expected_skills": ["alpha", "beta"],
            }
        ],
    )
    beta_dataset = _write_dataset(
        root,
        "routing_beta_v1.0.jsonl",
        [
            {
                "id": "beta-pos-01",
                "prompt": "beta",
                "expected_skills": ["beta"],
            }
        ],
    )
    alpha_suite = _write_suite(root, "routing_alpha.yaml", alpha_dataset)
    shared_suite = _write_suite(root, "routing_shared.yaml", shared_dataset)
    beta_suite = _write_suite(root, "routing_beta.yaml", beta_dataset)
    alpha_run = _write_run(root, "alpha-run", alpha_dataset)
    shared_run = _write_run(root, "shared-run", shared_dataset)
    beta_run = _write_run(root, "beta-run", beta_dataset)
    return {
        "alpha_skill": alpha_skill,
        "beta_skill": beta_skill,
        "fixture": fixture,
        "expected": expected,
        "alpha_dataset": alpha_dataset,
        "shared_dataset": shared_dataset,
        "beta_dataset": beta_dataset,
        "alpha_suite": alpha_suite,
        "shared_suite": shared_suite,
        "beta_suite": beta_suite,
        "alpha_run": alpha_run,
        "shared_run": shared_run,
        "beta_run": beta_run,
    }


def test_single_subject_archive_packages_shared_dependencies_but_retains_them(tmp_path):
    paths = _workspace(tmp_path)
    archive = tmp_path / "archives" / "alpha.skilleval.tar.gz"

    plan = build_archive_plan(["alpha"], root=tmp_path, output=archive)

    assert plan.runnable
    entries = {entry.path: entry for entry in plan.entries}
    assert entries[
        paths["alpha_skill"].relative_to(tmp_path).as_posix()
    ].workspace_action == "remove_after_verify"
    assert entries[
        paths["alpha_dataset"].relative_to(tmp_path).as_posix()
    ].workspace_action == "remove_after_verify"
    assert entries[
        paths["shared_dataset"].relative_to(tmp_path).as_posix()
    ].workspace_action == "retain_shared"
    assert entries[
        paths["expected"].relative_to(tmp_path).as_posix()
    ].workspace_action == "remove_after_verify"
    assert paths["beta_dataset"].relative_to(tmp_path).as_posix() not in entries

    result = execute_archive(plan)

    assert Path(result.archive_path).is_file()
    assert not paths["alpha_skill"].exists()
    assert not paths["alpha_dataset"].exists()
    assert not paths["expected"].exists()
    assert not paths["alpha_run"].exists()
    assert paths["shared_dataset"].is_file()
    assert paths["shared_run"].is_dir()
    assert paths["beta_dataset"].is_file()
    assert paths["beta_run"].is_dir()

    restore = build_restore_plan(archive, root=tmp_path)
    assert restore.runnable
    statuses = {entry.path: entry.status for entry in restore.entries}
    assert statuses[paths["alpha_skill"].relative_to(tmp_path).as_posix()] == "restore"
    assert statuses[paths["shared_dataset"].relative_to(tmp_path).as_posix()] == "reuse"

    restored, reused = execute_restore(restore)

    assert restored > 0
    assert reused > 0
    assert paths["alpha_skill"].read_text(encoding="utf-8").startswith("---")
    assert paths["alpha_dataset"].is_file()
    assert paths["expected"].is_file()
    assert paths["alpha_run"].is_dir()
    assert archive.is_file(), "unarchive must retain the portable package"


def test_batch_archive_removes_resources_shared_only_by_selected_subjects(tmp_path):
    paths = _workspace(tmp_path)
    archive = tmp_path / "archives" / "alpha-beta.skilleval.tar.gz"

    plan = build_archive_plan(
        ["beta", "alpha"],
        root=tmp_path,
        output=archive,
    )

    assert plan.runnable
    entries = {entry.path: entry for entry in plan.entries}
    assert entries[
        paths["shared_dataset"].relative_to(tmp_path).as_posix()
    ].workspace_action == "remove_after_verify"

    execute_archive(plan)

    assert not (tmp_path / "subjects" / "alpha").exists()
    assert not (tmp_path / "subjects" / "beta").exists()
    assert not paths["shared_dataset"].exists()
    assert not paths["shared_run"].exists()
    assert archive.is_file()


def test_restore_refuses_to_overwrite_different_content(tmp_path):
    paths = _workspace(tmp_path)
    archive = tmp_path / "archives" / "alpha.skilleval.tar.gz"
    execute_archive(build_archive_plan(["alpha"], root=tmp_path, output=archive))

    paths["alpha_skill"].parent.mkdir(parents=True, exist_ok=True)
    paths["alpha_skill"].write_text("different local work", encoding="utf-8")

    restore = build_restore_plan(archive, root=tmp_path)

    assert not restore.runnable
    assert any("refusing to overwrite" in reason for reason in restore.blocked_reasons)
    with pytest.raises(ValueError, match="blocked"):
        execute_restore(restore)
    assert paths["alpha_skill"].read_text(encoding="utf-8") == "different local work"


def test_archive_never_prunes_git_tracked_files(tmp_path, monkeypatch):
    paths = _workspace(tmp_path)
    tracked = paths["alpha_skill"].relative_to(tmp_path).as_posix()
    monkeypatch.setattr("pipeline.archive._tracked_paths", lambda _root: {tracked})

    plan = build_archive_plan(
        ["alpha"],
        root=tmp_path,
        output=tmp_path / "archives" / "alpha.skilleval.tar.gz",
    )
    entry = next(item for item in plan.entries if item.path == tracked)

    assert entry.workspace_action == "retain_tracked"
    execute_archive(plan)
    assert paths["alpha_skill"].is_file()


def test_missing_subject_and_existing_archive_block_before_writes(tmp_path):
    archive = tmp_path / "archives" / "existing.skilleval.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"do not overwrite")

    plan = build_archive_plan(["missing"], root=tmp_path, output=archive)

    assert not plan.runnable
    assert any("do not exist" in reason for reason in plan.blocked_reasons)
    assert any("already exists" in reason for reason in plan.blocked_reasons)
    with pytest.raises(ValueError, match="blocked"):
        execute_archive(plan)
    assert archive.read_bytes() == b"do not overwrite"


def test_restore_rejects_a_structurally_valid_archive_with_tampered_payload(tmp_path):
    _workspace(tmp_path)
    archive = tmp_path / "archives" / "alpha.skilleval.tar.gz"
    execute_archive(build_archive_plan(["alpha"], root=tmp_path, output=archive))
    rebuilt = tmp_path / "archives" / "tampered.tar.gz"
    changed = False
    with tarfile.open(archive, "r:gz") as source, tarfile.open(rebuilt, "w:gz") as target:
        for member in source.getmembers():
            handle = source.extractfile(member)
            assert handle is not None
            data = handle.read()
            if member.name.startswith("payload/") and data and not changed:
                data = bytes([data[0] ^ 1]) + data[1:]
                changed = True
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            info.mode = member.mode
            target.addfile(info, io.BytesIO(data))
    assert changed

    with pytest.raises(ValueError, match="checksum mismatch"):
        build_restore_plan(rebuilt, root=tmp_path)
