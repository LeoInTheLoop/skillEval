"""CLI for inspect-first evaluation execution."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from workflows.run_routing import ROOT

from .archive import (
    build_archive_plan,
    build_restore_plan,
    execute_archive,
    execute_restore,
    render_archive_plan,
    render_restore_plan,
)
from .initialize import build_init_plan, execute_init, render_init_plan
from .plan import build_plan, render_plan
from .plan import endpoint_health
from .rescore import build_rescore_plan, execute_rescore, parse_stages as parse_rescore_stages
from .rescore import render_rescore_plan


def _run_command(command: list[str]) -> None:
    # 子进程直接写同一个 fd；父进程被管道重定向时是全缓冲的，不 flush 就会出现
    # 「结果先出来、plan 后出来」这种读起来完全错乱的日志顺序。
    print("\n$ " + " ".join(command), flush=True)
    sys.stdout.flush()
    subprocess.run(command, cwd=ROOT, check=True)


def _parse_stages(value: str) -> set[str]:
    stages = {item.strip() for item in value.split(",") if item.strip()}
    unknown = stages - {"run", "grade", "trajectory", "score"}
    if unknown:
        raise SystemExit(
            f"unknown pipeline stages: {sorted(unknown)}; choose run,grade,trajectory,score"
        )
    if not stages:
        raise SystemExit("--stages cannot be empty")
    return stages


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline",
        description="Inspect a versioned evaluation suite before running it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--suite", required=True, help="versioned suite YAML; all experiment parameters come from here")
        target.add_argument("--mock", action="store_true", help="plan/run the runner's explicit mock override")

    init_parser = sub.add_parser(
        "init",
        help="import one skill snapshot and generate a review-gated dataset/suite draft",
    )
    init_parser.add_argument("--source", required=True, help="installed skill directory or SKILL.md")
    acceptance_group = init_parser.add_mutually_exclusive_group(required=True)
    acceptance_group.add_argument("--acceptance", help="business goal and acceptance criteria")
    acceptance_group.add_argument("--acceptance-file", help="file containing acceptance criteria")
    init_parser.add_argument("--dest-root", default="subjects")
    init_parser.add_argument("--version", default="v1")
    init_parser.add_argument("--output-dir")
    init_parser.add_argument("--count", type=int, default=10)
    init_parser.add_argument("--model-id", default="qwen3.7-max-2026-05-17")
    init_parser.add_argument("--model", default="openai/qwen3.7-max-2026-05-17")
    init_parser.add_argument("--api-base-env", default="DASHSCOPE_BASE_URL")
    init_parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    init_parser.add_argument("--confirm", action="store_true", help="approve local snapshot/draft writes")
    init_parser.add_argument(
        "--confirm-egress",
        action="store_true",
        help="approve sending the displayed metadata/acceptance payload to the generator model",
    )
    init_parser.add_argument("--json", action="store_true", help="emit the init plan as JSON")

    plan_parser = sub.add_parser("plan", help="validate and print the no-write execution plan")
    add_common(plan_parser)
    plan_parser.add_argument("--healthcheck", action="store_true", help="also probe runtime/environment locally")
    plan_parser.add_argument("--json", action="store_true", help="emit the plan as JSON")
    plan_parser.add_argument("--execution-id", help="preview a specific immutable archive ID")

    run_parser = sub.add_parser("run", help="run the displayed plan, then deterministic scoring")
    add_common(run_parser)
    run_parser.add_argument("--confirm", action="store_true", help="required acknowledgement after reviewing `pipeline plan`")
    run_parser.add_argument(
        "--confirm-egress",
        action="store_true",
        help="for a real run, explicitly approve the external-data manifest printed by the plan",
    )
    run_parser.add_argument(
        "--stages",
        default="run,score",
        help="comma-separated: run,grade,trajectory,score (default: run,score). grade calls the "
             "suite's independent judge — a paid external call, so it is opt-in; it always "
             "runs before score regardless of the order written here",
    )
    run_parser.add_argument("--execution-id", help="archive ID; default is a fresh timestamp")

    rescore_parser = sub.add_parser(
        "rescore",
        help="re-grade immutable historical runs without executing the Agent again",
    )
    rescore_parser.add_argument("--run-dir", required=True, help="immutable historical run directory")
    rescore_parser.add_argument("--stages", default="score",
                                help="comma-separated: grade,trajectory,score (default: score)")
    rescore_parser.add_argument("--grading-id", help="versioned output id; default: fresh timestamp")
    rescore_parser.add_argument("--judge-id")
    rescore_parser.add_argument("--judge-model")
    rescore_parser.add_argument("--judge-api-base-env")
    rescore_parser.add_argument("--judge-api-key-env")
    rescore_parser.add_argument("--judge-params-json", help="replace judge params with a JSON object")
    rescore_parser.add_argument("--dimensions", nargs="+")
    rescore_parser.add_argument("--confirm", action="store_true",
                                help="write new versioned grading/scores/report files")
    rescore_parser.add_argument("--confirm-egress", action="store_true",
                                help="approve sending archived evidence to judge stages")
    rescore_parser.add_argument("--json", action="store_true")

    archive_parser = sub.add_parser(
        "archive",
        help="package and clean one or more subjects' evaluation history",
    )
    archive_parser.add_argument(
        "--subjects",
        nargs="+",
        required=True,
        help="one or more subject ids under subjects/",
    )
    archive_parser.add_argument(
        "--output",
        help="archive file path; default: archives/<subjects>__<timestamp>.skilleval.tar.gz",
    )
    archive_parser.add_argument(
        "--archive-root",
        default="archives",
        help="default archive directory when --output is omitted",
    )
    archive_parser.add_argument(
        "--confirm",
        action="store_true",
        help="write and verify the package, then remove only unshared/untracked originals",
    )
    archive_parser.add_argument("--json", action="store_true", help="emit the archive plan as JSON")

    unarchive_parser = sub.add_parser(
        "unarchive",
        help="verify and safely restore a subject evaluation package",
    )
    unarchive_parser.add_argument("archive", help="path to a .skilleval.tar.gz package")
    unarchive_parser.add_argument(
        "--confirm",
        action="store_true",
        help="restore missing files; identical files are reused and conflicts are never overwritten",
    )
    unarchive_parser.add_argument("--json", action="store_true", help="emit the restore plan as JSON")

    args = parser.parse_args()
    if args.command == "init":
        acceptance = (
            Path(args.acceptance_file).expanduser().read_text(encoding="utf-8")
            if args.acceptance_file
            else args.acceptance
        )
        assert acceptance is not None
        plan = build_init_plan(
            source_path=args.source,
            acceptance=acceptance,
            dest_root=args.dest_root,
            version=args.version,
            output_dir=args.output_dir,
            count=args.count,
            model_id=args.model_id,
            model=args.model,
            api_base_env=args.api_base_env,
            api_key_env=args.api_key_env,
        )
        print(
            json.dumps(plan.as_dict(), indent=2, ensure_ascii=False)
            if args.json else render_init_plan(plan),
            flush=True,
        )
        if not args.confirm:
            raise SystemExit(0 if plan.runnable else 1)
        if not plan.runnable:
            raise SystemExit("refusing to initialize: preflight is blocked")
        if not args.confirm_egress:
            raise SystemExit(
                "refusing generator model call: review the external-data manifest, "
                "then add --confirm-egress"
            )
        healthy, detail = endpoint_health({
            "id": args.model_id,
            "api_base_env": args.api_base_env,
        })
        if not healthy:
            raise SystemExit(f"refusing to initialize: generator endpoint check failed: {detail}")
        execute_init(
            plan,
            acceptance=acceptance,
            api_base_env=args.api_base_env,
            api_key_env=args.api_key_env,
        )
        return

    if args.command == "archive":
        plan = build_archive_plan(
            args.subjects,
            root=ROOT,
            output=args.output,
            archive_root=args.archive_root,
        )
        print(
            json.dumps(plan.as_dict(), indent=2, ensure_ascii=False)
            if args.json else render_archive_plan(plan),
            flush=True,
        )
        if not args.confirm:
            raise SystemExit(0 if plan.runnable else 1)
        if not plan.runnable:
            raise SystemExit("refusing to archive: preflight is blocked")
        result = execute_archive(plan)
        print(
            f"\nARCHIVED: {result.archive_path}\n"
            f"checksum: {result.archive_sha256}\n"
            f"workspace: removed={result.removed_files}, retained={result.retained_files}",
            flush=True,
        )
        return

    if args.command == "unarchive":
        plan = build_restore_plan(args.archive, root=ROOT)
        print(
            json.dumps(plan.as_dict(), indent=2, ensure_ascii=False)
            if args.json else render_restore_plan(plan),
            flush=True,
        )
        if not args.confirm:
            raise SystemExit(0 if plan.runnable else 1)
        if not plan.runnable:
            raise SystemExit("refusing to restore: preflight is blocked")
        restored, reused = execute_restore(plan)
        print(
            f"\nRESTORED: {plan.archive_path}\n"
            f"workspace: restored={restored}, reused={reused}",
            flush=True,
        )
        return

    if args.command == "rescore":
        load_dotenv(ROOT / ".env")
        try:
            stages = parse_rescore_stages(args.stages)
            judge_params = json.loads(args.judge_params_json) if args.judge_params_json else None
            if judge_params is not None and not isinstance(judge_params, dict):
                raise ValueError("--judge-params-json 必须是 JSON object")
            grading_id = args.grading_id or datetime.now().astimezone().strftime(
                "%Y%m%dT%H%M%S%f%z"
            )
            plan = build_rescore_plan(
                args.run_dir,
                stages=stages,
                grading_id=grading_id,
                judge_id=args.judge_id,
                judge_model=args.judge_model,
                judge_api_base_env=args.judge_api_base_env,
                judge_api_key_env=args.judge_api_key_env,
                dimensions=args.dimensions,
                judge_params=judge_params,
            )
        except (ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"invalid rescore request: {error}") from error
        print(
            json.dumps(plan.as_dict(), indent=2, ensure_ascii=False)
            if args.json else render_rescore_plan(plan),
            flush=True,
        )
        if not args.confirm:
            raise SystemExit(0 if plan.runnable else 1)
        if not plan.runnable:
            raise SystemExit("refusing to rescore: preflight is blocked")
        if plan.egress_required and not args.confirm_egress:
            raise SystemExit(
                "refusing judge calls: review the rescore external-data manifest, "
                "then add --confirm-egress"
            )
        outputs = execute_rescore(plan)
        print("\nRESCORED", flush=True)
        for name, path in outputs.items():
            print(f"  {name}: {path}", flush=True)
        return

    if args.command == "plan":
        plan = build_plan(args.suite, mock=args.mock, check_health=args.healthcheck,
                          execution_id=args.execution_id)
        print(json.dumps(plan.as_dict(), indent=2, ensure_ascii=False)
              if args.json else render_plan(plan), flush=True)
        raise SystemExit(0 if plan.runnable else 1)

    execution_id = args.execution_id or datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    # A real run always repeats the non-invasive local/runtime/DNS preflight.
    # It still makes no model completion, so repository prompts have not left
    # the machine when the consent checks below are evaluated.
    plan = build_plan(
        args.suite,
        mock=args.mock,
        check_health=not args.mock,
        execution_id=execution_id,
    )
    print(render_plan(plan), flush=True)
    if not args.confirm:
        raise SystemExit("refusing to run: review the plan above, then add --confirm")
    if not plan.runnable:
        raise SystemExit("refusing to run: preflight is blocked")
    if plan.egress["approval_required"] and not args.confirm_egress:
        raise SystemExit(
            "refusing external model calls: review the 'external data movement' section, "
            "then add --confirm-egress"
        )
    stages = _parse_stages(args.stages)
    suite = str(Path(args.suite))
    if "run" in stages:
        command = [sys.executable, "-m", "workflows.run_routing", "--suite", suite,
                   "--execution-id", execution_id]
        if args.mock:
            command.append("--mock")
        _run_command(command)
    # grade 必须排在 score 之前：score_full 把 grading.<judge>.json 读进来才算得出
    # assertion_pass_rate 与语义维度。用户在 --stages 里写的先后顺序不作数。
    if "grade" in stages:
        if not any(stage["name"].startswith("grade") for stage in plan.stages):
            raise SystemExit(
                "refusing to grade: suite 没有配 scoring.judge —— 语义判定需要一把独立的尺子。\n"
                "  → 在 suite 里补 scoring.judge（id/model/api_base_env/api_key_env/dimensions），"
                "或从 --stages 去掉 grade"
            )
        for model in plan.models:
            if model.credential == "missing":
                continue
            _run_command([sys.executable, "-m", "workflows.grade",
                          "--dir", str(ROOT / model.result_dir)])
    if "trajectory" in stages:
        if not any(stage["module"] == "workflows.grade_trajectory" for stage in plan.stages):
            raise SystemExit(
                "refusing trajectory grade: suite 没有启用 scoring.trajectory；"
                "请在 suite 中设置 enabled: true"
            )
        for model in plan.models:
            if model.credential == "missing":
                continue
            _run_command([sys.executable, "-m", "workflows.grade_trajectory",
                          "--dir", str(ROOT / model.result_dir)])
    if "score" in stages:
        scorer = "workflows.score_full" if plan.skill_mode == "full" else "workflows.score_routing"
        for model in plan.models:
            if model.credential == "missing":
                continue
            _run_command([sys.executable, "-m", scorer, "--dir", str(ROOT / model.result_dir)])


if __name__ == "__main__":
    main()
