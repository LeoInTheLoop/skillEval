"""Subject-level evaluation bundle archive and restore.

One execution directory is already self-contained.  This module adds the
missing lifecycle layer above it: collect the immutable subject snapshots,
their cases/suites/fixtures, and every matching run into one checksummed
package.  Shared resources are copied into the package but retained in the
workspace unless every subject that owns them is selected together.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml


ARCHIVE_FORMAT = "skilleval-subject-archive"
ARCHIVE_SCHEMA_VERSION = 1
_ARCHIVE_SUFFIX = ".skilleval.tar.gz"
_CASE_SCOPE = re.compile(
    r"^(?P<scope>[A-Za-z0-9_.+-]+)-(?:pos|amb|rej|multi)-\d{2,}$"
)
_SUBJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PAYLOAD_PREFIX = "payload/"
_ALLOWED_RESTORE_ROOTS = {"subjects", "evals", "outputs"}


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or not path.parts
        or path.parts[0] not in _ALLOWED_RESTORE_ROOTS
    ):
        raise ValueError(f"archive contains unsafe workspace path: {value!r}")
    return path


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"path escapes workspace root: {path}") from exc


def _path_subjects(stem: str, subjects: Iterable[str]) -> set[str]:
    """Match a subject as one underscore-delimited filename component."""
    return {
        subject
        for subject in subjects
        if re.search(rf"(?:^|_){re.escape(subject)}(?:_|$)", stem)
    }


def _case_subjects(data: dict[str, Any]) -> set[str]:
    subjects = {
        value
        for value in data.get("expected_skills", [])
        if isinstance(value, str) and value != "none"
    }
    case_id = data.get("id")
    match = _CASE_SCOPE.fullmatch(case_id) if isinstance(case_id, str) else None
    if match:
        subjects.update(
            part for part in match.group("scope").split("+") if part != "none"
        )
    return subjects


def _read_dataset_metadata(
    path: Path,
) -> tuple[set[str], set[str], set[str], list[str]]:
    subjects: set[str] = set()
    files: set[str] = set()
    case_ids: set[str] = set()
    issues: list[str] = []
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(f"{path}:{line_number}: invalid JSON ({exc.msg})")
            continue
        if not isinstance(data, dict):
            issues.append(f"{path}:{line_number}: case must be a JSON object")
            continue
        subjects.update(_case_subjects(data))
        if isinstance(data.get("id"), str):
            case_ids.add(data["id"])
        for value in data.get("files", []):
            if isinstance(value, str):
                files.add(value)
    return subjects, files, case_ids, issues


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def _explicit_suite_subjects(suite: dict[str, Any]) -> set[str]:
    skills = suite.get("skills") or {}
    if not isinstance(skills, dict):
        return set()
    target = skills.get("target") or []
    if isinstance(target, str):
        return {target}
    elif isinstance(target, list):
        explicit = {str(value) for value in target}
        if explicit:
            return explicit
    # Compatibility for historical config.snapshot files created before
    # skills.target became part of the strict suite contract.
    versions = skills.get("versions") or {}
    return set(str(value) for value in versions) if isinstance(versions, dict) else set()


def _tracked_paths(root: Path) -> set[str]:
    """Git-tracked examples belong to the product, so archive but never prune."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    }


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    sha256: str
    size: int
    mode: int
    category: str
    subjects: tuple[str, ...]
    workspace_action: str

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["subjects"] = list(self.subjects)
        return data


@dataclass(frozen=True)
class ArchivePlan:
    root: str
    subjects: tuple[str, ...]
    archive_path: str
    created_at: str
    entries: tuple[ArchiveEntry, ...]
    blocked_reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def runnable(self) -> bool:
        return not self.blocked_reasons

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)

    @property
    def removable_files(self) -> int:
        return sum(
            entry.workspace_action == "remove_after_verify" for entry in self.entries
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "subjects": list(self.subjects),
            "archive_path": self.archive_path,
            "created_at": self.created_at,
            "entries": [entry.as_dict() for entry in self.entries],
            "summary": {
                "files": len(self.entries),
                "bytes": self.total_bytes,
                "remove_after_verify": self.removable_files,
                "retained": len(self.entries) - self.removable_files,
            },
            "blocked_reasons": list(self.blocked_reasons),
            "warnings": list(self.warnings),
            "runnable": self.runnable,
        }


@dataclass(frozen=True)
class ArchiveResult:
    archive_path: str
    archive_sha256: str
    removed_files: int
    retained_files: int


@dataclass(frozen=True)
class RestoreEntry:
    path: str
    sha256: str
    size: int
    mode: int
    status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RestorePlan:
    root: str
    archive_path: str
    subjects: tuple[str, ...]
    entries: tuple[RestoreEntry, ...]
    blocked_reasons: tuple[str, ...]

    @property
    def runnable(self) -> bool:
        return not self.blocked_reasons

    def as_dict(self) -> dict[str, Any]:
        counts = Counter(entry.status for entry in self.entries)
        return {
            "root": self.root,
            "archive_path": self.archive_path,
            "subjects": list(self.subjects),
            "entries": [entry.as_dict() for entry in self.entries],
            "summary": dict(sorted(counts.items())),
            "blocked_reasons": list(self.blocked_reasons),
            "runnable": self.runnable,
        }


@dataclass
class _EntryDraft:
    path: Path
    category: str
    subjects: set[str]
    removable: bool


def _iter_tree_files(path: Path, issues: list[str]) -> list[Path]:
    if path.is_symlink():
        issues.append(f"symbolic links are not archived: {path}")
        return []
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    files: list[Path] = []
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            issues.append(f"symbolic links are not archived: {item}")
        elif item.is_file():
            files.append(item)
    return files


def _is_ancestor(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def build_archive_plan(
    subjects: Iterable[str],
    *,
    root: str | Path,
    output: str | Path | None = None,
    archive_root: str | Path = "archives",
    created_at: str | None = None,
) -> ArchivePlan:
    """Build a no-write, dependency-aware archive plan."""
    workspace = Path(root).resolve()
    selected = tuple(sorted(set(subjects)))
    now = created_at or datetime.now().astimezone().isoformat()
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    subject_label = "__".join(selected or ("empty",))
    if len(subject_label) > 120:
        digest = _sha256_bytes("\0".join(selected).encode("utf-8")).split(":", 1)[1][:10]
        subject_label = (
            "__".join(selected[:3])
            + f"__plus-{max(0, len(selected) - 3)}__{digest}"
        )
    default_name = subject_label + f"__{stamp}{_ARCHIVE_SUFFIX}"
    target = Path(output) if output is not None else Path(archive_root) / default_name
    archive_path = (target if target.is_absolute() else workspace / target).resolve()

    blocked: list[str] = []
    warnings: list[str] = []
    if not selected:
        blocked.append("at least one subject is required")
    invalid = [subject for subject in selected if not _SUBJECT_ID.fullmatch(subject)]
    if invalid:
        blocked.append(f"invalid subject ids: {invalid}")
    if archive_path.exists():
        blocked.append(f"archive already exists; refusing to overwrite: {archive_path}")
    for source_root in ("subjects", "evals", "outputs"):
        if _is_ancestor(workspace / source_root, archive_path):
            blocked.append(
                f"archive output must not be inside active workspace data: {archive_path}"
            )
            break

    subject_root = workspace / "subjects"
    known_subjects = {
        path.name for path in subject_root.iterdir() if path.is_dir()
    } if subject_root.is_dir() else set()
    missing = [subject for subject in selected if subject not in known_subjects]
    if missing:
        blocked.append(f"subject snapshots do not exist under subjects/: {missing}")

    # related subjects answer "should this be included in the package?";
    # owners answer "may it be removed when these subjects are archived?".
    # A pdf-focused dataset may contain docx/xlsx boundary cases, but those
    # neighboring gold skills do not co-own the dataset.  An explicit subject
    # token in the versioned filename is the ownership signal; generic
    # routing_all/example sets are genuinely shared by every related subject.
    dataset_meta: dict[str, tuple[set[str], set[str], set[str], set[str]]] = {}
    dataset_dir = workspace / "evals" / "datasets"
    if dataset_dir.is_dir():
        for path in sorted(dataset_dir.glob("*.jsonl")):
            associations, files, case_ids, issues = _read_dataset_metadata(path)
            filename_owners = _path_subjects(path.stem, known_subjects)
            associations.update(filename_owners)
            owners = filename_owners or set(associations)
            dataset_meta[_relative(workspace, path)] = (
                associations,
                owners,
                files,
                case_ids,
            )
            warnings.extend(issues)

    drafts: dict[str, _EntryDraft] = {}

    def add_resource(
        path: Path,
        *,
        category: str,
        associations: set[str],
        removable: bool,
    ) -> None:
        for file_path in _iter_tree_files(path, blocked):
            relative = _relative(workspace, file_path)
            existing = drafts.get(relative)
            if existing is None:
                drafts[relative] = _EntryDraft(
                    path=file_path,
                    category=category,
                    subjects=set(associations),
                    removable=removable,
                )
            else:
                existing.subjects.update(associations)
                existing.removable = existing.removable and removable

    # The selected subject snapshots are the only mandatory resource.
    for subject in selected:
        path = subject_root / subject
        if path.is_dir():
            add_resource(
                path,
                category="subject",
                associations={subject},
                removable=True,
            )

    selected_datasets: dict[
        str, tuple[set[str], set[str], set[str], set[str]]
    ] = {}
    for relative, metadata in dataset_meta.items():
        associations, owners, _, _ = metadata
        if associations.intersection(selected):
            selected_datasets[relative] = metadata
            add_resource(
                workspace / relative,
                category="dataset",
                associations=associations,
                removable=bool(owners) and owners.issubset(selected),
            )

    # Fixtures are owned by every dataset that references them.  If an
    # unselected dataset still needs one, it is retained after packaging.
    fixture_owners: dict[str, set[str]] = {}
    for _, owners, file_refs, _ in dataset_meta.values():
        for file_ref in file_refs:
            fixture_owners.setdefault(file_ref, set()).update(owners)
    selected_fixture_refs = {
        file_ref
        for _, _, file_refs, _ in selected_datasets.values()
        for file_ref in file_refs
    }
    for file_ref in sorted(selected_fixture_refs):
        try:
            safe_ref = _safe_relative_path(file_ref)
        except ValueError as exc:
            blocked.append(str(exc))
            continue
        fixture = workspace.joinpath(*safe_ref.parts)
        if not fixture.is_file():
            warnings.append(f"referenced fixture is missing: {file_ref}")
            continue
        owners = fixture_owners.get(file_ref, set())
        add_resource(
            fixture,
            category="fixture",
            associations=owners,
            removable=bool(owners) and owners.issubset(selected),
        )

    expected_owners: dict[str, set[str]] = {}
    for _, owners, _, case_ids in dataset_meta.values():
        for case_id in case_ids:
            expected_owners.setdefault(case_id, set()).update(owners)
    selected_case_ids = {
        case_id
        for _, _, _, case_ids in selected_datasets.values()
        for case_id in case_ids
    }
    expected_root = workspace / "evals" / "expected"
    for case_id in sorted(selected_case_ids):
        expected = expected_root / case_id
        if not expected.exists():
            continue
        owners = expected_owners.get(case_id, set())
        add_resource(
            expected,
            category="expected",
            associations=owners,
            removable=bool(owners) and owners.issubset(selected),
        )

    # A suite belongs to the dataset it points at.  versions/target are added
    # as explicit ownership hints, but catalog include/exclude are deliberately
    # ignored: neighboring skills are candidates, not owners of the experiment.
    suite_dir = workspace / "evals" / "suites"
    if suite_dir.is_dir():
        for path in sorted(suite_dir.glob("*.yaml")):
            suite = _load_yaml(path)
            dataset_value = suite.get("dataset")
            explicit_owners = _explicit_suite_subjects(suite)
            filename_owners = _path_subjects(path.stem, known_subjects)
            associations = set(explicit_owners)
            associations.update(filename_owners)
            dataset_owners: set[str] = set()
            if isinstance(dataset_value, str):
                try:
                    dataset_relative = _relative(
                        workspace,
                        (workspace / dataset_value).resolve(),
                    )
                except ValueError:
                    dataset_relative = ""
                dataset_related, dataset_owners, _, _ = dataset_meta.get(
                    dataset_relative, (set(), set(), set(), set())
                )
                associations.update(dataset_related)
            owners = explicit_owners or filename_owners or dataset_owners
            if associations.intersection(selected):
                add_resource(
                    path,
                    category="suite",
                    associations=associations,
                    removable=bool(owners) and owners.issubset(selected),
                )

    # Keep review drafts and subject-specific analysis with the test package.
    draft_root = workspace / "evals" / "drafts"
    if draft_root.is_dir():
        for path in sorted(draft_root.iterdir()):
            associations = {
                subject
                for subject in selected
                if path.name == subject
                or path.name.startswith(subject + "-")
                or path.name.startswith(subject + "_")
            }
            if associations:
                add_resource(
                    path,
                    category="draft",
                    associations=associations,
                    removable=True,
                )
    analysis_root = workspace / "evals" / "analysis"
    if analysis_root.is_dir():
        for path in sorted(analysis_root.iterdir()):
            associations = _path_subjects(path.stem, selected)
            if associations:
                add_resource(
                    path,
                    category="analysis",
                    associations=associations,
                    removable=True,
                )

    # Each config.snapshot marks an execution root, including the historical
    # flat layout.  Prefer the canonical dataset; fall back to its frozen input
    # copy so an already-moved dataset does not make the run undiscoverable.
    run_units: list[tuple[Path, set[str], set[str]]] = []
    outputs = workspace / "outputs"
    if outputs.is_dir():
        for snapshot in sorted(outputs.rglob("config.snapshot.yaml")):
            config = _load_yaml(snapshot)
            suite = config.get("suite") or {}
            explicit_owners = _explicit_suite_subjects(suite)
            associations = set(explicit_owners)
            dataset_owners: set[str] = set()
            dataset_value = suite.get("dataset") if isinstance(suite, dict) else None
            if isinstance(dataset_value, str):
                try:
                    dataset_relative = _relative(
                        workspace,
                        (workspace / dataset_value).resolve(),
                    )
                except ValueError:
                    dataset_relative = ""
                dataset_related, dataset_owners, _, _ = dataset_meta.get(
                    dataset_relative, (set(), set(), set(), set())
                )
                associations.update(dataset_related)
            frozen_dataset = snapshot.parent / "inputs" / "dataset.jsonl"
            if not associations and frozen_dataset.is_file():
                frozen_subjects, _, _, frozen_issues = _read_dataset_metadata(
                    frozen_dataset
                )
                associations.update(frozen_subjects)
                dataset_owners.update(frozen_subjects)
                warnings.extend(frozen_issues)
            if associations.intersection(selected):
                run_units.append(
                    (
                        snapshot.parent,
                        associations,
                        explicit_owners or dataset_owners,
                    )
                )
        for path in sorted(outputs.iterdir()):
            if not path.is_file():
                continue
            associations = _path_subjects(path.stem, known_subjects)
            if associations.intersection(selected):
                add_resource(
                    path,
                    category="comparison",
                    associations=associations,
                    removable=associations.issubset(selected),
                )

    # A legacy flat run can contain newer execution subdirectories.  Archive
    # the ancestor once and union ownership so cleanup remains conservative.
    merged_units: list[tuple[Path, set[str], set[str]]] = []
    for path, associations, owners in sorted(
        run_units, key=lambda item: len(item[0].parts)
    ):
        ancestor = next(
            (
                existing
                for existing in merged_units
                if _is_ancestor(existing[0], path)
            ),
            None,
        )
        if ancestor:
            ancestor[1].update(associations)
            ancestor[2].update(owners)
        else:
            merged_units.append((path, set(associations), set(owners)))
    for path, associations, owners in merged_units:
        add_resource(
            path,
            category="run",
            associations=associations,
            removable=bool(owners) and owners.issubset(selected),
        )

    tracked = _tracked_paths(workspace)
    entries: list[ArchiveEntry] = []
    for relative, draft in sorted(drafts.items()):
        if relative in tracked:
            action = "retain_tracked"
        elif draft.removable:
            action = "remove_after_verify"
        else:
            action = "retain_shared"
        info = draft.path.stat()
        entries.append(
            ArchiveEntry(
                path=relative,
                sha256=_sha256_file(draft.path),
                size=info.st_size,
                mode=stat.S_IMODE(info.st_mode) & 0o777,
                category=draft.category,
                subjects=tuple(sorted(draft.subjects)),
                workspace_action=action,
            )
        )

    if not entries and not missing:
        blocked.append("no subject evaluation files were found")
    shared_count = sum(entry.workspace_action == "retain_shared" for entry in entries)
    tracked_count = sum(entry.workspace_action == "retain_tracked" for entry in entries)
    if shared_count:
        warnings.append(
            f"{shared_count} shared files will be packaged but retained in the workspace"
        )
    if tracked_count:
        warnings.append(
            f"{tracked_count} Git-tracked files will be packaged but retained in the workspace"
        )

    return ArchivePlan(
        root=str(workspace),
        subjects=selected,
        archive_path=str(archive_path),
        created_at=now,
        entries=tuple(entries),
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def render_archive_plan(plan: ArchivePlan) -> str:
    categories = Counter(entry.category for entry in plan.entries)
    retained = len(plan.entries) - plan.removable_files
    lines = [
        "Subject evaluation archive plan (read-only; no files written or removed)",
        f"subjects: {', '.join(plan.subjects) or '(none)'}",
        f"archive: {plan.archive_path}",
        f"payload: {len(plan.entries)} files / {plan.total_bytes} bytes",
        "categories: "
        + (", ".join(f"{key}={value}" for key, value in sorted(categories.items()))
           or "(none)"),
        f"workspace cleanup after checksum verification: remove={plan.removable_files}, "
        f"retain={retained}",
    ]
    if plan.warnings:
        lines.append("\nNOTES:")
        lines.extend(f"  - {warning}" for warning in plan.warnings)
    if plan.blocked_reasons:
        lines.append("\nBLOCKED:")
        lines.extend(f"  - {reason}" for reason in plan.blocked_reasons)
    else:
        lines.extend(
            [
                "\nREADY. Review this plan, then add --confirm.",
                "The package is written and fully verified before removable originals are deleted.",
            ]
        )
    return "\n".join(lines)


def _manifest_for(plan: ArchivePlan) -> dict[str, Any]:
    return {
        "format": ARCHIVE_FORMAT,
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "created_at": plan.created_at,
        "subjects": list(plan.subjects),
        "entries": [entry.as_dict() for entry in plan.entries],
    }


def _validate_manifest(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("archive manifest must be a JSON object")
    if data.get("format") != ARCHIVE_FORMAT:
        raise ValueError("not a skillEval subject archive")
    if data.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported archive schema: {data.get('schema_version')!r}"
        )
    subjects = data.get("subjects")
    entries = data.get("entries")
    if not isinstance(subjects, list) or not all(
        isinstance(value, str) and _SUBJECT_ID.fullmatch(value) for value in subjects
    ):
        raise ValueError("archive manifest has invalid subjects")
    if not isinstance(entries, list):
        raise ValueError("archive manifest entries must be a list")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("archive manifest entry must be an object")
        path = entry.get("path")
        _safe_relative_path(path if isinstance(path, str) else "")
        if path in seen:
            raise ValueError(f"duplicate archive entry: {path}")
        seen.add(path)
        if not isinstance(entry.get("size"), int) or entry["size"] < 0:
            raise ValueError(f"invalid archive entry size: {path}")
        if (
            not isinstance(entry.get("mode"), int)
            or entry["mode"] < 0
            or entry["mode"] > 0o777
        ):
            raise ValueError(f"invalid archive entry mode: {path}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ):
            raise ValueError(f"invalid archive entry checksum: {path}")
    return data


def _verified_manifest(archive_path: Path) -> dict[str, Any]:
    """Validate member types/names/sizes and every payload checksum."""
    try:
        archive = tarfile.open(archive_path, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f"cannot read archive {archive_path}: {exc}") from exc
    with archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError("archive contains duplicate tar members")
        manifest_member = next(
            (member for member in members if member.name == "manifest.json"), None
        )
        if (
            manifest_member is None
            or not manifest_member.isfile()
            or manifest_member.size > 10 * 1024 * 1024
        ):
            raise ValueError("archive is missing a valid manifest.json")
        manifest_handle = archive.extractfile(manifest_member)
        if manifest_handle is None:
            raise ValueError("archive manifest cannot be read")
        try:
            manifest = _validate_manifest(json.load(manifest_handle))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"archive manifest is invalid JSON: {exc}") from exc

        expected_names = {
            "manifest.json",
            *(_PAYLOAD_PREFIX + entry["path"] for entry in manifest["entries"]),
        }
        if set(names) != expected_names:
            extras = sorted(set(names) - expected_names)
            missing = sorted(expected_names - set(names))
            raise ValueError(
                f"archive member mismatch; extra={extras[:3]}, missing={missing[:3]}"
            )
        by_name = {member.name: member for member in members}
        for entry in manifest["entries"]:
            member = by_name[_PAYLOAD_PREFIX + entry["path"]]
            if not member.isfile() or member.size != entry["size"]:
                raise ValueError(f"archive payload metadata mismatch: {entry['path']}")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"archive payload cannot be read: {entry['path']}")
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            if "sha256:" + digest.hexdigest() != entry["sha256"]:
                raise ValueError(f"archive payload checksum mismatch: {entry['path']}")
        return manifest


def _remove_empty_parents(root: Path, paths: Iterable[Path]) -> None:
    stops = {
        root,
        root / "subjects",
        root / "evals",
        root / "evals" / "datasets",
        root / "evals" / "suites",
        root / "evals" / "fixtures",
        root / "evals" / "expected",
        root / "evals" / "drafts",
        root / "evals" / "analysis",
        root / "outputs",
    }
    parents = sorted(
        {parent for path in paths for parent in path.parents if parent not in stops},
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for parent in parents:
        if not _is_ancestor(root, parent):
            continue
        try:
            parent.rmdir()
        except OSError:
            pass


def execute_archive(plan: ArchivePlan) -> ArchiveResult:
    """Write, verify, then prune only the files approved by the plan."""
    if not plan.runnable:
        raise ValueError("archive plan is blocked: " + "; ".join(plan.blocked_reasons))
    root = Path(plan.root)
    archive_path = Path(plan.archive_path)
    if archive_path.exists():
        raise FileExistsError(f"refusing to overwrite archive: {archive_path}")
    for entry in plan.entries:
        source = root / entry.path
        if (
            not source.is_file()
            or source.is_symlink()
            or source.stat().st_size != entry.size
            or _sha256_file(source) != entry.sha256
        ):
            raise RuntimeError(
                f"workspace changed after archive plan; rerun dry-run: {entry.path}"
            )

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".skilleval-archive-",
            suffix=".tmp",
            dir=archive_path.parent,
            delete=False,
        ) as temporary:
            temp_path = Path(temporary.name)
        manifest_bytes = json.dumps(
            _manifest_for(plan),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        with tarfile.open(temp_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_bytes)
            manifest_info.mode = 0o600
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
            for entry in plan.entries:
                source = root / entry.path
                info = tarfile.TarInfo(_PAYLOAD_PREFIX + entry.path)
                info.size = entry.size
                info.mode = entry.mode
                with source.open("rb") as handle:
                    archive.addfile(info, handle)
        _verified_manifest(temp_path)
        os.replace(temp_path, archive_path)
        temp_path = None
        _verified_manifest(archive_path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

    removable = [
        (root / entry.path, entry)
        for entry in plan.entries
        if entry.workspace_action == "remove_after_verify"
    ]
    changed = [
        entry.path
        for path, entry in removable
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != entry.size
            or _sha256_file(path) != entry.sha256
        )
    ]
    if changed:
        raise RuntimeError(
            f"archive is safe at {archive_path}, but cleanup was aborted because "
            f"workspace files changed: {changed[:3]}"
        )
    removed_paths = []
    for path, _ in removable:
        path.unlink()
        removed_paths.append(path)
    _remove_empty_parents(root, removed_paths)
    return ArchiveResult(
        archive_path=str(archive_path),
        archive_sha256=_sha256_file(archive_path),
        removed_files=len(removed_paths),
        retained_files=len(plan.entries) - len(removed_paths),
    )


def build_restore_plan(
    archive: str | Path,
    *,
    root: str | Path,
) -> RestorePlan:
    """Verify an archive and report restore/reuse/collision without writing."""
    workspace = Path(root).resolve()
    archive_path = Path(archive).expanduser().resolve()
    manifest = _verified_manifest(archive_path)
    entries: list[RestoreEntry] = []
    blocked: list[str] = []
    for raw in manifest["entries"]:
        relative = _safe_relative_path(raw["path"])
        destination = workspace.joinpath(*relative.parts)
        current = workspace
        unsafe_parent = None
        for part in relative.parts[:-1]:
            current = current / part
            if current.is_symlink():
                unsafe_parent = current
                break
        if unsafe_parent is not None or destination.is_symlink():
            status = "collision"
            blocked.append(
                f"restore path contains a symbolic link: {unsafe_parent or destination}"
            )
        elif not destination.exists():
            status = "restore"
        elif destination.is_file() and _sha256_file(destination) == raw["sha256"]:
            status = "reuse"
        else:
            status = "collision"
            blocked.append(
                f"existing path has different content; refusing to overwrite: "
                f"{raw['path']}"
            )
        entries.append(
            RestoreEntry(
                path=raw["path"],
                sha256=raw["sha256"],
                size=raw["size"],
                mode=raw["mode"],
                status=status,
            )
        )
    return RestorePlan(
        root=str(workspace),
        archive_path=str(archive_path),
        subjects=tuple(manifest["subjects"]),
        entries=tuple(entries),
        blocked_reasons=tuple(dict.fromkeys(blocked)),
    )


def render_restore_plan(plan: RestorePlan) -> str:
    counts = Counter(entry.status for entry in plan.entries)
    lines = [
        "Subject evaluation restore plan (read-only; no files written)",
        f"archive: {plan.archive_path}",
        f"subjects: {', '.join(plan.subjects)}",
        f"payload: {len(plan.entries)} verified files",
        "actions: "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())),
    ]
    if plan.blocked_reasons:
        lines.append("\nBLOCKED:")
        lines.extend(f"  - {reason}" for reason in plan.blocked_reasons)
    else:
        lines.extend(
            [
                "\nREADY. Review this plan, then add --confirm.",
                "Existing identical files are reused; the archive itself is retained.",
            ]
        )
    return "\n".join(lines)


def execute_restore(plan: RestorePlan) -> tuple[int, int]:
    """Restore missing files atomically; never overwrite different content."""
    if not plan.runnable:
        raise ValueError("restore plan is blocked: " + "; ".join(plan.blocked_reasons))
    fresh = build_restore_plan(plan.archive_path, root=plan.root)
    if not fresh.runnable:
        raise RuntimeError(
            "workspace changed after restore plan: "
            + "; ".join(fresh.blocked_reasons)
        )
    root = Path(plan.root)
    restore_paths = {
        entry.path for entry in fresh.entries if entry.status == "restore"
    }
    restored = 0
    with tarfile.open(plan.archive_path, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
        for entry in fresh.entries:
            if entry.path not in restore_paths:
                continue
            destination = root.joinpath(*PurePosixPath(entry.path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            member = members[_PAYLOAD_PREFIX + entry.path]
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"archive payload disappeared: {entry.path}")
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=".skilleval-restore-",
                    dir=destination.parent,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        temporary.write(chunk)
                if destination.exists() or destination.is_symlink():
                    raise RuntimeError(
                        f"restore destination appeared during extraction: {entry.path}"
                    )
                os.chmod(temporary_path, entry.mode)
                os.replace(temporary_path, destination)
                temporary_path = None
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink()
            if _sha256_file(destination) != entry.sha256:
                raise RuntimeError(f"restored checksum mismatch: {entry.path}")
            restored += 1
    reused = len(fresh.entries) - restored
    return restored, reused
