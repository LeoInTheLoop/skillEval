"""Read-only run inspection for the P7 traceability path.

The scorer reports aggregate quality.  This module answers the adjacent user
question: "where are the facts for this exact case/repeat/turn?"  It only reads
immutable run inputs/results and projects them into one stable, filterable
shape; it never rewrites ``runs.jsonl`` or scoring artifacts.
"""
from __future__ import annotations

import html
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import yaml

from contracts import load_cases


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def _dataset_path(run_dir: Path, snapshot: dict[str, Any], root: Path) -> Path | None:
    frozen = run_dir / "inputs" / "dataset.jsonl"
    if frozen.is_file():
        return frozen
    configured = ((snapshot.get("suite") or {}).get("dataset"))
    if not configured:
        return None
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else root / candidate


def _record_status(run: dict[str, Any]) -> str:
    if run.get("status") == "skipped":
        return "skipped"
    return "ok" if run.get("ok", True) else "failed"


def _tool_names(value: Any) -> list[str]:
    return [
        str(item.get("name"))
        for item in (value or [])
        if isinstance(item, dict) and item.get("name")
    ]


def _artifact_paths(value: Any) -> list[str]:
    return [
        str(item.get("path"))
        for item in (value or [])
        if isinstance(item, dict) and item.get("path")
    ]


def inspect_run(run_dir: str | Path, *, root: str | Path) -> dict[str, Any]:
    """Load one exact run directory into a JSON-safe trace projection."""
    workspace = Path(root).resolve()
    directory = Path(run_dir).expanduser().resolve()
    runs_path = directory / "runs.jsonl"
    snapshot_path = directory / "config.snapshot.yaml"
    if not directory.is_dir() or not runs_path.is_file():
        raise FileNotFoundError(
            f"not a run directory: {directory} (expected runs.jsonl); "
            "pass the exact execution directory printed by `pipeline run`"
        )
    snapshot = (
        yaml.safe_load(snapshot_path.read_text(encoding="utf-8")) or {}
        if snapshot_path.is_file() else {}
    )
    suite = snapshot.get("suite") or {}
    dataset_path = _dataset_path(directory, snapshot, workspace)
    cases = {}
    if dataset_path is not None and dataset_path.is_file():
        cases = {case.id: case for case in load_cases(dataset_path)}

    records = []
    observed_models: set[str] = set()
    for line_number, line in enumerate(runs_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"{runs_path}:{line_number} must be a JSON object")
        case_id = str(raw.get("case_id") or "")
        case = cases.get(case_id)
        actual_model = raw.get("resolved_model") or raw.get("model")
        if actual_model:
            observed_models.add(str(actual_model))
        records.append({
            "case_id": case_id,
            "turn": int(raw.get("turn_index") or 1),
            "repeat": int(raw.get("repeat_index") or 0),
            "status": _record_status(raw),
            "model": actual_model,
            "session_id": raw.get("session_id"),
            "prompt": case.prompt if case is not None else None,
            "expected_skills": list(case.expected_skills) if case is not None else [],
            "selected_skills": list(raw.get("selected_skills") or []),
            "loaded_skills": list(raw.get("loaded_skills") or []),
            "tool_calls": _tool_names(raw.get("tool_calls")),
            "tool_call_details": list(raw.get("tool_calls") or []),
            "artifacts": _artifact_paths(raw.get("artifacts")),
            "artifact_details": list(raw.get("artifacts") or []),
            "final_answer": raw.get("final_answer"),
            "error_kind": raw.get("error_kind"),
            "error_subkind": raw.get("error_subkind"),
            "error": raw.get("error"),
        })

    scores = _read_json(directory / "scores.json")
    statuses = Counter(record["status"] for record in records)
    grading = sorted(
        _relative(path, workspace)
        for pattern in ("grading.*.json", "grading/**/*.json")
        for path in directory.glob(pattern)
    )
    reports = sorted(
        _relative(path, workspace)
        for pattern in ("report.html", "reports/**/*.html")
        for path in directory.glob(pattern)
    )
    suggestions = sorted(
        _relative(path, workspace)
        for path in directory.glob("improvements/**/suggestions.json")
    )
    return {
        "schema_version": "run-inspection-v1",
        "run_dir": _relative(directory, workspace),
        "suite_id": suite.get("suite_id"),
        "skill_mode": (suite.get("skills") or {}).get("mode"),
        "skill_cfg": (suite.get("skills") or {}).get("cfg"),
        "config_hash": snapshot.get("config_hash"),
        "observed_models": sorted(observed_models),
        "record_count": len(records),
        "matching_record_count": len(records),
        "available_case_ids": sorted({record["case_id"] for record in records}),
        "status_counts": dict(sorted(statuses.items())),
        "gate_pass": scores.get("gate_pass") if scores else None,
        "quality_verdict": scores.get("quality_verdict") if scores else None,
        "metrics": scores.get("scores", {}) if scores else {},
        "paths": {
            "snapshot": _relative(snapshot_path, workspace) if snapshot_path.is_file() else None,
            "runs": _relative(runs_path, workspace),
            "dataset": _relative(dataset_path, workspace) if dataset_path else None,
            "scores": _relative(directory / "scores.json", workspace) if scores else None,
            "reports": reports,
            "grading": grading,
            "suggestions": suggestions,
        },
        "records": records,
    }


def filter_records(
    records: Iterable[dict[str, Any]],
    *,
    case_id: str | None = None,
    turn: int | None = None,
    repeat: int | None = None,
    status: str | None = None,
    skill: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Filter the stable projection without rereading or changing a run."""
    selected = []
    for record in records:
        if case_id and record["case_id"] != case_id:
            continue
        if turn is not None and record["turn"] != turn:
            continue
        if repeat is not None and record["repeat"] != repeat:
            continue
        if status and record["status"] != status:
            continue
        if model and model.lower() not in str(record.get("model") or "").lower():
            continue
        skills = {
            *record.get("expected_skills", []),
            *record.get("selected_skills", []),
            *record.get("loaded_skills", []),
        }
        if skill and skill not in skills:
            continue
        selected.append(record)
    return selected


def _html_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return html.escape(str(value if value is not None else ""), quote=True)


def _evidence_links(
    view: dict[str, Any], *, root: Path, output: Path
) -> list[tuple[str, str]]:
    links = []
    for key, value in view["paths"].items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not item:
                continue
            path = Path(item)
            absolute = path if path.is_absolute() else root / path
            href = os.path.relpath(absolute, output.parent)
            links.append((key, href))
    return links


def render_html_view(
    view: dict[str, Any], *, root: str | Path, output: str | Path
) -> str:
    """Build a self-contained, offline HTML trace viewer."""
    workspace = Path(root).resolve()
    target = Path(output).resolve()
    verdict = view.get("quality_verdict") or (
        "PASS" if view.get("gate_pass") is True
        else "FAIL" if view.get("gate_pass") is False
        else "N/A"
    )
    metrics = "".join(
        f"<div class='metric'><b>{_html_value(name)}</b><span>{_html_value(value)}</span></div>"
        for name, value in sorted((view.get("metrics") or {}).items())
    ) or "<div class='metric'><b>metrics</b><span>N/A</span></div>"
    evidence = "".join(
        f"<li><b>{_html_value(kind)}</b>: <a href='{_html_value(href)}'>{_html_value(href)}</a></li>"
        for kind, href in _evidence_links(view, root=workspace, output=target)
    )
    rows = []
    for record in view["records"]:
        skills = sorted({
            *record.get("expected_skills", []),
            *record.get("selected_skills", []),
            *record.get("loaded_skills", []),
        })
        detail_parts = []
        for label, key in (
            ("Prompt", "prompt"), ("Answer", "final_answer"),
            ("Loaded skills", "loaded_skills"),
            ("Tools", "tool_call_details"), ("Artifacts", "artifact_details"),
        ):
            value = record.get(key)
            if value:
                detail_parts.append(
                    f"<h4>{label}</h4><pre>{_html_value(value)}</pre>"
                )
        if record.get("error"):
            classification = (
                f"{record.get('error_kind') or 'unclassified'}/"
                f"{record.get('error_subkind') or '-'}: {record['error']}"
            )
            detail_parts.append(f"<h4>Error</h4><pre>{_html_value(classification)}</pre>")
        rows.append(
            "<tr "
            f"data-case='{_html_value(record['case_id']).lower()}' "
            f"data-turn='{record['turn']}' data-repeat='{record['repeat']}' "
            f"data-status='{_html_value(record['status']).lower()}' "
            f"data-skill='{_html_value(' '.join(skills)).lower()}' "
            f"data-model='{_html_value(record.get('model')).lower()}'>"
            f"<td><span class='status {record['status']}'>{_html_value(record['status'])}</span></td>"
            f"<td>{_html_value(record['case_id'])}</td>"
            f"<td>{record['turn']}</td><td>{record['repeat']}</td>"
            f"<td>{_html_value(record.get('model'))}</td>"
            f"<td>{_html_value(record['expected_skills'] or ['∅'])}</td>"
            f"<td>{_html_value(record['selected_skills'] or ['∅'])}</td>"
            f"<td><details><summary>open</summary>{''.join(detail_parts) or 'No detail'}</details></td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>skillEval run · {_html_value(view['run_dir'])}</title>
<style>
:root{{--bg:#f7f7f5;--panel:#fff;--ink:#202124;--muted:#697077;--line:#ddd;--accent:#315efb}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}}
main{{max-width:1500px;margin:auto;padding:24px}} h1{{font-size:22px;margin:0 0 4px}} .muted{{color:var(--muted)}}
.summary,.filters,.evidence{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin:16px 0}}
.metrics{{display:flex;flex-wrap:wrap;gap:8px}} .metric{{min-width:150px;padding:8px 12px;background:#f1f3f5;border-radius:7px}}
.metric span{{display:block;font-size:18px}} .filters{{display:grid;grid-template-columns:repeat(6,minmax(120px,1fr));gap:10px}}
label{{font-size:12px;color:var(--muted)}} input,select{{width:100%;padding:7px;border:1px solid var(--line);border-radius:5px;background:white}}
.table-wrap{{overflow:auto;background:white;border:1px solid var(--line);border-radius:10px}} table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;vertical-align:top;padding:9px;border-bottom:1px solid var(--line)}} th{{position:sticky;top:0;background:#f1f3f5}}
.status{{padding:2px 7px;border-radius:10px;background:#e8eaed}} .status.ok{{background:#d8f3dc}} .status.failed{{background:#ffd6d6}}
pre{{white-space:pre-wrap;max-width:700px}} a{{color:var(--accent)}} #count{{font-weight:600}}
@media(max-width:900px){{.filters{{grid-template-columns:repeat(2,1fr)}}}}
</style></head><body><main>
<h1>skillEval run viewer</h1><div class="muted">{_html_value(view['run_dir'])}</div>
<section class="summary"><p><b>Suite:</b> {_html_value(view.get('suite_id'))} · <b>mode:</b> {_html_value(view.get('skill_mode'))}
· <b>skillcfg:</b> {_html_value(view.get('skill_cfg'))} · <b>verdict:</b> {_html_value(verdict)}</p>
<p><b>Models:</b> {_html_value(view.get('observed_models') or ['unresolved'])} · <b>config:</b> {_html_value(view.get('config_hash'))}</p>
<div class="metrics">{metrics}</div></section>
<section class="evidence"><h2>Evidence files</h2><ul>{evidence}</ul></section>
<section class="filters">
<label>Case<input id="case" placeholder="case id"></label>
<label>Turn<input id="turn" type="number" min="1" placeholder="all"></label>
<label>Repeat<input id="repeat" type="number" min="0" placeholder="all"></label>
<label>Status<select id="status"><option value="">all</option><option>ok</option><option>failed</option><option>skipped</option></select></label>
<label>Skill<input id="skill" placeholder="skill id"></label>
<label>Model<input id="model" placeholder="model"></label>
</section>
<p id="count"></p><div class="table-wrap"><table><thead><tr><th>Status</th><th>Case</th><th>Turn</th><th>Repeat</th><th>Model</th><th>Expected</th><th>Selected</th><th>Trace</th></tr></thead>
<tbody id="records">{''.join(rows)}</tbody></table></div>
<script>
const ids=['case','turn','repeat','status','skill','model'];const numeric=new Set(['turn','repeat']);
const rows=[...document.querySelectorAll('#records tr')];
function apply(){{const q=Object.fromEntries(ids.map(id=>[id,document.getElementById(id).value.trim().toLowerCase()]));let n=0;
for(const row of rows){{const show=ids.every(id=>!q[id]||(numeric.has(id)?row.dataset[id]===q[id]:row.dataset[id].includes(q[id])));row.hidden=!show;if(show)n++;}}
document.getElementById('count').textContent=`${{n}} / ${{rows.length}} records`;}}
ids.forEach(id=>document.getElementById(id).addEventListener('input',apply));apply();
</script></main></body></html>"""


def write_html_view(
    view: dict[str, Any],
    *,
    root: str | Path,
    output: str | Path,
    force: bool = False,
) -> tuple[Path, str]:
    """Write or reuse a deterministic viewer without silent overwrite."""
    target = Path(output).expanduser().resolve()
    content = render_html_view(view, root=root, output=target)
    if target.exists():
        if target.read_text(encoding="utf-8") == content:
            return target, "reused"
        if not force:
            raise FileExistsError(
                f"viewer exists with different content: {target}; rerun with --force to replace it"
            )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target, "written"


def render_inspection(view: dict[str, Any]) -> str:
    """Render a compact terminal view with every artifact path locatable."""
    def excerpt(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    verdict = view.get("quality_verdict") or (
        "PASS" if view.get("gate_pass") is True
        else "FAIL" if view.get("gate_pass") is False
        else "N/A"
    )
    lines = [
        f"Run: {view['run_dir']}",
        f"Suite: {view.get('suite_id') or 'unknown'} | mode={view.get('skill_mode') or 'unknown'} "
        f"| skillcfg={view.get('skill_cfg') or 'unknown'}",
        f"Verdict: {verdict} | records={view.get('matching_record_count', view['record_count'])}"
        f"/{view['record_count']} "
        f"| status={view['status_counts']}",
        f"Models: {', '.join(view.get('observed_models') or []) or 'unresolved'}",
        "Evidence paths:",
    ]
    paths = view["paths"]
    for key in ("snapshot", "dataset", "runs", "scores"):
        lines.append(f"  {key}: {paths.get(key) or 'N/A'}")
    for key in ("reports", "grading", "suggestions"):
        values = paths.get(key) or []
        lines.append(f"  {key}: {', '.join(values) if values else 'N/A'}")
    lines.append("Records:")
    if not view["records"]:
        lines.append("  (no records match the filters)")
        lines.append(
            f"  available case ids: {', '.join(view.get('available_case_ids') or []) or 'N/A'}"
        )
    for record in view["records"]:
        expected = record["expected_skills"] or ["∅"]
        selected = record["selected_skills"] or ["∅"]
        lines.append(
            f"  {record['case_id']} t{record['turn']} r{record['repeat']} "
            f"[{record['status']}] expected={expected} selected={selected}"
        )
        if record.get("prompt"):
            lines.append(f"    prompt: {excerpt(record['prompt'], 180)}")
        if record.get("final_answer"):
            lines.append(f"    answer: {excerpt(record['final_answer'], 300)}")
        if record["tool_calls"] or record["artifacts"]:
            lines.append(
                f"    tools={record['tool_calls'] or []} artifacts={record['artifacts'] or []}"
            )
        if record.get("error"):
            lines.append(
                f"    error={record.get('error_kind') or 'unclassified'}/"
                f"{record.get('error_subkind') or '-'}: {record['error']}"
            )
    return "\n".join(lines)
