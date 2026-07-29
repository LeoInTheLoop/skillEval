"""路由 eval 编排层：suite(YAML) → 工厂造 runtime → 跑 → outputs/<run>/runs.jsonl

本文件**只做编排**，不含任何模型/agent 调用逻辑 —— 那些在 adapters/runtimes/ 里，
通过 `create_runtime(name)` 工厂拿到，上层不知道背后是 LiteLLM 还是 OpenClaw。

一切配置在 suite 里，脚本不留可调参数（改参数要能被 config_hash 捕获）。
密钥不进 suite，suite 只写环境变量名，值留 .env。

用法：
  python -m workflows.run_routing                                  # 跑默认 suite
  python -m workflows.run_routing --suite evals/suites/xxx.yaml    # 跑指定 suite
  python -m workflows.run_routing --mock                           # 强制用 mock runtime
  python -m workflows.run_routing --healthcheck                    # 只探 runtime 健康度，不跑

目录/命名/写题规范见 evals/AUTHORING.md，整体组织见 evals/RUNBOOK.md。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import os
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from adapters.runtimes import create_runtime
from contracts import (
    InvocationRequest,
    RoutingCase,
    RunResult,
    SkillMeta,
    load_cases,
    load_skills,
    load_suite,
    resolve_suite_references,
    format_suite_validation_error,
    require_approved_dataset,
)
from workflows.matrix import MatrixTask, build_matrix
from environments import create_environment
from environments.filesystem import stage_input_files

ROOT = Path(__file__).parent.parent


def run_dir(suite: dict, model_id: str, execution_id: str | None = None) -> Path:
    """One immutable archive below the logical dataset/model/skill group."""
    slug = re.sub(r"[^A-Za-z0-9.+-]", "-", model_id)
    dataset = Path(suite["dataset"]).stem
    group = ROOT / "outputs" / f"{dataset}__{slug}__{suite['skills']['cfg']}"
    return group / execution_id if execution_id else group


def file_hash(path: Path) -> str:
    """输入文件内容 hash；suite 只保存路径，内容变化也必须进指纹。"""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# 纯给人读的元信息：改它们一个字都不会改变 runs.jsonl，所以不进 hash（AGENTS.md §8.3）。
# 它们照常进 config.snapshot.yaml —— 追溯看快照，可比性看 hash，两件事分开。
# 踩过：只把 suite 的 description 改了一句，hash 就变了，工具随即报「配置动过、
# 不可直接比」。这种假警报会训练用户忽略真警报。
# models 单独剔除的理由见下面 docstring，语义不同，不合并进这个集合。
_HASH_EXEMPT_SUITE_FIELDS = frozenset({"models", "suite_id", "suite_version", "description"})


def config_hash(suite: dict, model: dict | None = None, mock: bool = False,
                runtime_fp: dict | None = None, skills: list[SkillMeta] | None = None,
                dataset_hash: str | None = None,
                environment_fp: dict | None = None) -> str:
    """**单个 run** 的配置指纹。两个 run 的 hash 不同 = 结果不可直接比较。

    六部分：
      suite(除 models 与纯文档字段) —— 数据集/skill 配置/重复数/门槛/runtime
      model            —— 只含**本 run 用的那一个**模型条目，不是整个 models 列表
      mock             —— 假数据和真实调用用同一个 suite，但结果绝不可比
      runtime_fp       —— adapter 内部会改变结果的东西（system prompt、CLI 版本…），
                          否则改一句 prompt 就能悄悄改变结果而指纹不变
      skills           —— 实际暴露给模型的 skill 目录及内容 hash。skill 才是被测对象：
                          往 installed_skills/ 装个新 skill 会改变 catalog，而 suite 一个字没动
      dataset_hash     —— 测试题内容 hash。suite 只写路径，题变了 hash 也必须变

    为什么 models 列表要剔除、只留本 run 的那条：每个模型出独立目录，
    给 suite **新增**一个模型、或修另一个模型的端点，都不该让已跑模型的结果失效。
    整份 models 进 hash 会让"加个模型"作废全部历史结果 —— 粒度错了。

    `scoring.judge` 同理剔除：judge 是**评分阶段**的量具，`grade.py` 跑在 run 之后，
    换它一个字节都不会改变 runs.jsonl。让它进 hash，就会出现「改了判分模型 →
    config_hash 变 → 历史 run 看起来不可比」，可两边的原始运行其实一模一样。
    judge 的追溯走另一条路：`scores.json` 的 `judge` 字段 + compare_runs 的
    `[⚠️ 尺子不同]`，那才是它真正影响的范围（只有 assertion_pass_rate 一行）。
    """
    suite_wo_models = {
        k: v for k, v in suite.items() if k not in _HASH_EXEMPT_SUITE_FIELDS
    }
    # parallelism=1 就是旧版串行行为。规范化时去掉默认值，避免仅升级契约便让所有
    # 历史基准 hash 失效；真正启用并发（>1）会保留并进入 hash。
    if suite_wo_models.get("parallelism", 1) == 1:
        suite_wo_models.pop("parallelism", None)
    # skills.target is experiment ownership/provenance.  It changes which
    # subject bundle and Viewer index a run belongs to, but not the catalog,
    # prompt, runtime, or runs.jsonl.  Keep it in config.snapshot, not in the
    # execution-comparability hash.
    skill_spec = suite_wo_models.get("skills")
    if isinstance(skill_spec, dict) and "target" in skill_spec:
        suite_wo_models = {
            **suite_wo_models,
            "skills": {key: value for key, value in skill_spec.items() if key != "target"},
        }
    scoring = suite_wo_models.get("scoring")
    if isinstance(scoring, dict) and "judge" in scoring:
        suite_wo_models = {**suite_wo_models,
                           "scoring": {k: v for k, v in scoring.items() if k != "judge"}}
    blob = yaml.safe_dump(
        {"suite": suite_wo_models, "model": model or {}, "mock": mock,
         "runtime": runtime_fp or {},
         "environment": environment_fp or {},
         "skills": {s.skill_id: s.content_hash for s in (skills or [])},
         "dataset_hash": dataset_hash or ""},
        sort_keys=True, allow_unicode=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_runtime(suite: dict, cases: list[RoutingCase], mock: bool):
    """工厂调用点。mock 覆盖 suite 里的 runtime 选择。"""
    if mock:
        return create_runtime("mock", expected={c.id: c.expected_skills for c in cases})
    name = suite.get("runtime", "litellm")
    options = dict(suite.get("runtime_options") or {})
    if name in {"litellm", "openclaw"}:
        # “给模型看什么”由独立策略工厂决定；runtime 只负责执行。
        options["routing_input"] = suite.get("routing_input") or {
            "strategy": "direct", "options": {}
        }
    return create_runtime(name, **options)


def build_environment(suite: dict):
    """suite environment 配置只进 Environment 工厂，不泄漏给 Runtime/Evaluator。"""
    spec = suite.get("environment") or {"backend": "local"}
    options = dict(spec.get("options") or {})
    backend = spec.get("backend", "local")
    if backend == "docker":
        for key in ("image", "network", "cpus", "memory", "env_passthrough"):
            if spec.get(key) is not None:
                options[key] = spec[key]
    return create_environment(backend, **options)


def progress(line: str) -> None:
    """进度写 stderr 并立刻 flush：stdout 留给结果，管道里也不会攒到最后才吐。"""
    print(line, file=sys.stderr, flush=True)


def resolve_case_files(case: RoutingCase, turn_index: int = 1) -> list[str]:
    """某一轮声明的素材转成宿主机绝对路径（§11.4）。"""
    return [
        str((ROOT / raw).resolve())
        for raw in case.resolved_turn(turn_index).files
    ]


def missing_case_files(cases: list[RoutingCase]) -> list[str]:
    """跑之前就把「素材找不到」查出来，别跑到一半才炸（§11.4 验收标准）。"""
    return [
        f"{case.id}: {raw}"
        for case in cases
        for raw in case.all_files
        if not (ROOT / raw).is_file()
    ]


def _workspace_files(request: InvocationRequest) -> list[str]:
    """列出轮次结束时仍存在的 workspace 文件，供多轮延续断言使用。"""
    environment = request.environment
    if not environment or not environment.host_workspace:
        return []
    root = Path(environment.host_workspace)
    if not root.is_dir():
        return []
    files: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in {".git", ".openclaw"} for part in relative.parts):
            continue
        files.append(relative.as_posix())
    return sorted(files)


def _request_for_task(
    task: MatrixTask,
    *,
    mode: str,
    model: dict,
    skills: list[SkillMeta],
    allowed_tools: list[str],
    timeout: int,
) -> InvocationRequest:
    turn = task.turn
    return InvocationRequest(
        request_id=task.request_id,
        case_id=task.case.id,
        repeat_index=task.repeat_index,
        turn_index=task.turn_index,
        prompt=turn.prompt,
        context=task.case.context,
        skills=skills,
        skill_mode=mode,
        model=model,
        input_files=resolve_case_files(task.case, task.turn_index),
        allowed_tools=allowed_tools,
        session_id=task.session_id,
        timeout_seconds=timeout,
    )


def _harness_failure(task: MatrixTask, model: dict, runtime, error: Exception) -> RunResult:
    return RunResult(
        request_id=task.request_id,
        session_id=task.session_id,
        case_id=task.case.id,
        repeat_index=task.repeat_index,
        turn_index=task.turn_index,
        model=str(model.get("id", "unknown")),
        status="failed",
        error=repr(error),
        error_kind="harness",
        runtime_name=runtime.name,
    )


def _skipped_turn(task: MatrixTask, model: dict, runtime, previous: RunResult) -> RunResult:
    return RunResult(
        request_id=task.request_id,
        session_id=task.session_id,
        case_id=task.case.id,
        repeat_index=task.repeat_index,
        turn_index=task.turn_index,
        model=str(model.get("id", "unknown")),
        status="skipped",
        skip_reason=(
            f"previous turn t{previous.turn_index} ended with status={previous.status}"
        ),
        runtime_name=runtime.name,
    )


def _execute_conversation(
    tasks: list[MatrixTask],
    *,
    mode: str,
    model: dict,
    skills: list[SkillMeta],
    allowed_tools: list[str],
    timeout: int,
    runtime,
    environment,
    runtime_lock: threading.Lock | None,
    announce,
) -> list[RunResult]:
    """同一 case/repeat 的 turns 串行执行，并共享 environment/session/workspace。"""
    requests = [
        _request_for_task(
            task,
            mode=mode,
            model=model,
            skills=skills,
            allowed_tools=allowed_tools,
            timeout=timeout,
        )
        for task in tasks
    ]
    results: list[RunResult] = []

    def run_prepared(task: MatrixTask, request: InvocationRequest) -> RunResult:
        announce(task)
        try:
            with runtime_lock if runtime_lock is not None else nullcontext():
                result = runtime.run(request)
        except Exception as error:  # noqa: BLE001 — adapter 违反不抛异常契约
            result = _harness_failure(task, model, runtime, error)
        result.request_id = task.request_id
        result.session_id = task.session_id
        result.turn_index = task.turn_index
        result.workspace_files = _workspace_files(request)
        return result

    if environment is None:
        # 历史单轮单测/调用保持兼容。多轮 production 总会由 main 传 Environment；
        # 没有它就没有可共享 workspace，只能显式失败，不能假装多轮已隔离。
        if len(tasks) > 1:
            announce(tasks[0])
            first = _harness_failure(
                tasks[0], model, runtime,
                RuntimeError("多轮 full eval 必须提供 Environment Backend"),
            )
            skipped = []
            for task in tasks[1:]:
                announce(task)
                skipped.append(_skipped_turn(task, model, runtime, first))
            return [first, *skipped]
        return [run_prepared(tasks[0], requests[0])]

    current_index = 0
    try:
        with environment.prepared(requests[0]) as first_request:
            shared_environment = first_request.environment
            if shared_environment is None:
                raise RuntimeError("Environment Backend 没有返回 ResolvedEnvironment")
            for current_index, (task, request) in enumerate(zip(tasks, requests)):
                if results and not results[-1].ok:
                    announce(task)
                    results.append(_skipped_turn(task, model, runtime, results[-1]))
                    continue
                if current_index == 0:
                    prepared = first_request
                else:
                    host_workspace = shared_environment.host_workspace
                    if request.input_files:
                        if not host_workspace:
                            raise RuntimeError("后续轮次有输入文件，但 environment 没有 host workspace")
                        stage_input_files(request, Path(host_workspace))
                    prepared = request.model_copy(
                        update={"environment": shared_environment}
                    )
                results.append(run_prepared(task, prepared))
    except Exception as error:  # noqa: BLE001 — setup/staging/teardown 都归 harness
        if len(results) <= current_index:
            announce(tasks[current_index])
            failed = _harness_failure(tasks[current_index], model, runtime, error)
            results.append(failed)
        previous = results[-1]
        for task in tasks[len(results):]:
            announce(task)
            results.append(_skipped_turn(task, model, runtime, previous))
    return results


def resolve_skills(suite: dict) -> list[SkillMeta]:
    """按 suite 解析实际可见的 skill catalog；none 模式必须真正返回空目录。"""
    sk = suite["skills"]
    if sk["mode"] == "none":
        return []
    return load_skills(
        ROOT / sk["dir"],
        versions=sk.get("versions") or None,
        exclude=sk.get("exclude") or (),
        include=sk.get("include") or (),
    )


def snapshot_inputs(d: Path, suite: dict, skills: list[SkillMeta]) -> None:
    """Preserve exact dataset and resolved SKILL.md content for this execution.

    Hashes tell us that later V2/V3 or dataset edits changed conditions, but
    copies make the original condition reconstructible.
    """
    inputs = d / "inputs"
    dataset = ROOT / suite["dataset"]
    if dataset.is_file():
        inputs.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dataset, inputs / "dataset.jsonl")
    for skill in skills:
        source = Path(skill.source_path)
        if source.is_file():
            target = inputs / "skills" / skill.skill_id / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def run_one(suite: dict, m: dict, skills: list[SkillMeta], tasks: list[MatrixTask],
            runtime, mock: bool, dataset_hash: str,
            skipped_models: list[str] | None = None, environment=None,
            execution_id: str | None = None) -> Path:
    """跑一个 (suite × model) 组合，落 runs.jsonl + config.snapshot.yaml。"""
    if not tasks:
        raise ValueError(f"model={m.get('id', '?')} 没有矩阵任务，拒绝静默生成空 run")
    cases = list(dict.fromkeys(task.case.id for task in tasks))
    repeats = len({task.repeat_index for task in tasks})
    conversation_keys = list(dict.fromkeys(task.conversation_key for task in tasks))
    mode = suite["skills"]["mode"]
    timeout = suite.get("timeout_seconds", 300)
    parallelism = min(suite.get("parallelism", 1), len(conversation_keys))
    model_id = "mock" if mock else m["id"]
    execution_id = execution_id or tasks[0].execution_id
    d = run_dir(suite, model_id, execution_id)
    if d.exists():
        raise FileExistsError(
            f"归档已存在，拒绝覆盖历史结果：{d}；请使用新的 execution_id"
        )
    d.mkdir(parents=True, exist_ok=True)
    snapshot_inputs(d, suite, skills)

    # 配置快照：结果目录自解释，不用回头翻 suite 改没改
    snapshot = {
        "suite": copy.deepcopy(suite),
        "resolved_model": copy.deepcopy({**m, "id": model_id}),
        "runtime": {"name": runtime.name, "version": getattr(runtime, "version", None),
                    "capabilities": runtime.capabilities().model_dump(),
                    "fingerprint": runtime.fingerprint()},
        "environment": {
            "name": environment.name,
            "capabilities": environment.capabilities(),
            "fingerprint": environment.fingerprint(),
        } if environment else None,
        "config_hash": config_hash(
            suite, m, mock, runtime.fingerprint(), skills, dataset_hash,
            environment.fingerprint() if environment else None,
        ),
        "dataset_hash": dataset_hash,
        "skills": {s.skill_id: s.content_hash for s in skills},
        "dataset_cases": len(cases),
        "matrix_tasks": len(tasks),
        "conversations": len(conversation_keys),
        "parallelism": parallelism,
        "execution_id": tasks[0].execution_id,
        "archive_layout": "outputs/{dataset}__{model}__{skillcfg}/{execution_id}",
        "skill_catalog": [s.skill_id for s in skills],   # 实际暴露给模型的目录，none 基线看这里
        # 本次每个 skill 取了哪一版。suite 只钉了目标 skill 的版本，其余取最新 ——
        # 归档必须把「实际解析到的版本」写下来，否则日后加了 v3 就说不清这个 run 跑的是谁。
        "skill_versions": {s.skill_id: s.version for s in skills},
        "skipped_models": skipped_models or [],          # suite 写了但因缺 key 没跑的
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mock": mock,
    }
    (d / "config.snapshot.yaml").write_text(
        yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True), encoding="utf-8")

    print(f"[{model_id}] runtime={runtime.name} skills={len(skills)} "
          f"cases={len(cases)} repeats={repeats} turns={len(tasks)} "
          f"parallelism={parallelism} config_hash={snapshot['config_hash']}",
          flush=True)

    groups: dict[tuple[str, str, int], list[MatrixTask]] = {}
    for task in tasks:
        groups.setdefault(task.conversation_key, []).append(task)

    progress_lock = threading.Lock()
    progress_index = 0

    def announce(task: MatrixTask) -> None:
        nonlocal progress_index
        with progress_lock:
            progress_index += 1
            index = progress_index
        progress(
            f"  [{index}/{len(tasks)}] {task.case.id} "
            f"t{task.turn_index} r{task.repeat_index}"
        )

    # 本机 OpenClaw 的 config/profile 是进程外共享状态；并发 _swapped 会互相覆盖。
    # Docker 每 conversation 一个容器，不共享配置，可以真并发。其他 runtime 没有
    # OpenClaw 的全局配置问题，直接按 suite.parallelism 执行。
    runtime_lock = (
        threading.Lock()
        if parallelism > 1
        and runtime.name == "openclaw"
        and (environment is None or environment.name == "local")
        else None
    )
    if runtime_lock is not None:
        print(
            "  ⚠️ local OpenClaw profile 是共享状态：conversation 会并行准备，"
            "agent 调用串行；要真并发 full eval 请用 docker environment",
            flush=True,
        )

    def execute(group: list[MatrixTask]) -> list[RunResult]:
        try:
            return _execute_conversation(
                group,
                mode=mode,
                model=m,
                skills=skills,
                allowed_tools=list(suite.get("tools") or []),
                timeout=timeout,
                runtime=runtime,
                environment=environment,
                runtime_lock=runtime_lock,
                announce=announce,
            )
        except Exception as error:  # noqa: BLE001 — 一个 conversation 不能拖垮整批
            results: list[RunResult] = []
            for index, task in enumerate(group):
                announce(task)
                if index == 0:
                    results.append(_harness_failure(task, m, runtime, error))
                else:
                    results.append(_skipped_turn(task, m, runtime, results[-1]))
            return results

    completed: dict[str, RunResult] = {}
    if parallelism == 1:
        group_results = [execute(group) for group in groups.values()]
    else:
        group_results = []
        with ThreadPoolExecutor(
            max_workers=parallelism,
            thread_name_prefix="skilleval",
        ) as executor:
            futures = [executor.submit(execute, group) for group in groups.values()]
            for future in as_completed(futures):
                group_results.append(future.result())
    for results in group_results:
        for result in results:
            if result.request_id is None:
                raise RuntimeError("编排结果缺 request_id，无法恢复确定性矩阵顺序")
            completed[result.request_id] = result

    # 并发完成顺序不稳定；落盘严格恢复 matrix 顺序，保证同配置字节级可比。
    ordered_results = [completed[task.request_id] for task in tasks]
    with open(d / "runs.jsonl", "w", encoding="utf-8") as stream:
        for result in ordered_results:
            stream.write(result.model_dump_json() + "\n")

    n_ok = sum(result.ok for result in ordered_results)
    n_skipped = sum(result.status == "skipped" for result in ordered_results)
    failures: dict[str, int] = {}
    for result in ordered_results:
        if not result.ok and result.status != "skipped":
            kind = result.error_kind or "unclassified"
            failures[kind] = failures.get(kind, 0) + 1
    tail = "  " + " ".join(f"{k}={v}" for k, v in sorted(failures.items())) if failures else ""
    if n_skipped:
        tail += f" skipped={n_skipped}"
    print(f"  → {d}  ok_turns={n_ok}/{len(tasks)}{tail}", flush=True)
    return d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default=str(ROOT / "evals/suites/example_routing.yaml"))
    ap.add_argument("--mock", action="store_true", help="强制用 mock runtime，不调 API")
    ap.add_argument("--healthcheck", action="store_true", help="只探 runtime 健康度")
    ap.add_argument("--runtime", help="仅配合 --healthcheck：临时探另一个 runtime")
    ap.add_argument("--runtime-option", action="append", default=[], metavar="K=V",
                    help="仅配合 --runtime：传给该 runtime 的构造参数，可重复")
    ap.add_argument("--execution-id", help="本次归档 ID；pipeline 自动传入")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    try:
        suite = resolve_suite_references(load_suite(args.suite))
    except ValidationError as error:
        raise SystemExit(format_suite_validation_error(args.suite, error)) from error
    except ValueError as error:
        raise SystemExit(f"suite 环境引用无效：{error}") from error
    # Healthcheck probes the runtime and environment only.  It runs before the
    # catalog and dataset are resolved so that "is OpenClaw installed?" cannot
    # be answered with an unrelated error about a missing skill — that misdirects
    # exactly the person who is still setting the machine up.
    if args.healthcheck:
        if args.runtime:
            # "Is OpenClaw installed on this machine?" is a question about the
            # machine, not about any experiment.  Answering it must not require
            # authoring a full suite first — the override lives here and nowhere
            # else, so it can never leak into a scored run or a config_hash.
            suite = {**suite, "runtime": args.runtime, "runtime_options": dict(
                option.split("=", 1) for option in args.runtime_option
            )}
        elif args.runtime_option:
            raise SystemExit("--runtime-option 只在配合 --runtime 时有意义")
        environment = build_environment(suite)
        runtime = build_runtime(suite, [], args.mock)
        eh = environment.healthcheck()
        print(f"environment={environment.name} healthy={'✓' if eh.healthy else '✗'}")
        if eh.detail:
            print(f"  {eh.detail}")
        h = runtime.healthcheck(environment)
        print(f"runtime={h.runtime} version={h.version or '?'} "
              f"healthy={'✓' if h.healthy else '✗'}")
        if h.detail:
            print(f"  {h.detail}")
        raise SystemExit(0 if h.healthy and eh.healthy else 1)

    skills = resolve_skills(suite)
    dataset_path = ROOT / suite["dataset"]
    try:
        require_approved_dataset(dataset_path)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    cases = load_cases(dataset_path)
    dataset_hash = file_hash(dataset_path)
    runtime = build_runtime(suite, cases, args.mock)
    environment = build_environment(suite)

    # 能力校验：suite 要的模式 runtime 支不支持，跑之前就说清楚，别跑一半才炸
    caps = runtime.capabilities()
    mode = suite["skills"]["mode"]
    has_multi_turn = any(case.turn_count > 1 for case in cases)
    if mode not in caps.skill_modes:
        raise SystemExit(f"runtime={runtime.name} 不支持 skill_mode={mode}；"
                         f"它支持 {caps.skill_modes}")
    if suite.get("tools") and not caps.tools:
        raise SystemExit(f"suite 声明了 tools，但 runtime={runtime.name} 不支持 tool 调用")
    if has_multi_turn and mode != "full":
        raise SystemExit("多轮 case 只允许用于 skills.mode=full；routing-only 仍是一题一次路由判断")
    if has_multi_turn and not caps.multi_turn:
        raise SystemExit(f"dataset 含多轮 case，但 runtime={runtime.name} 不支持 multi_turn")

    models = [{"id": "mock"}] if args.mock else suite["models"]

    # 密钥体检：缺 key 的模型直接跳过，别等跑到一半每题都抛 AuthenticationError。
    # 跳过是安全的 —— 每个模型出独立目录，少跑一个不影响另一个的可比性；
    # 但必须大声说出来，并记进快照，否则会出现"suite 写了 2 个模型、结果只有 1 个"的哑谜。
    def has_key(m: dict) -> bool:
        return not m.get("api_key_env") or bool(os.environ.get(m["api_key_env"]))

    skipped = [m for m in models if not has_key(m)]
    models = [m for m in models if has_key(m)]
    for m in skipped:
        print(f"⚠️  跳过 {m['id']}：.env 里没有 {m['api_key_env']}")
    if not models:
        raise SystemExit("没有任何模型可跑（key 全缺）。suite 只写变量名，值放 .env —— "
                         "见 evals/RUNBOOK.md §2")

    execution_id = args.execution_id or datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", execution_id):
        raise SystemExit("--execution-id 只允许字母、数字、.、_、+、-")
    matrix = build_matrix(
        suite_id=suite["suite_id"],
        cases=cases,
        models=models,
        repeats=suite.get("repeats", 3),
        execution_id=execution_id,
    )

    # config_hash 是 per-model 的，在 run_one 里逐个打印
    print(f"suite={suite['suite_id']} v{suite['suite_version']} "
          f"runtime={runtime.name} skills={len(skills)} models={len(models)} "
          f"matrix_tasks={len(matrix)}")

    skipped_ids = [m["id"] for m in skipped]
    tasks_by_model = {str(m["id"]): [] for m in models}
    for task in matrix:
        tasks_by_model[task.model_id].append(task)
    dirs = [
        run_one(
            suite,
            m,
            skills,
            tasks_by_model[str(m["id"])],
            runtime,
            args.mock,
            dataset_hash,
            skipped_ids,
            environment,
            execution_id,
        )
        for m in models
    ]
    scorer = "workflows.score_full" if mode == "full" else "workflows.score_routing"
    print("\n下一步:")
    for d in dirs:
        print(f"  python -m {scorer} --dir {d}")


if __name__ == "__main__":
    main()
