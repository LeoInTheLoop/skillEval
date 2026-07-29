"""Full-eval 多轮编排与 conversation 级并发验收。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pandas as pd
import pytest

from contracts import (
    FullEvalTurn,
    RoutingCase,
    RunResult,
    RuntimeCapabilities,
    SuiteJudgeSpec,
)
from environments.local import LocalEnvironmentBackend
from workflows.grade import build_grading_prompt, grade_run
from workflows.matrix import build_matrix
from workflows.run_routing import run_one
from workflows.score_full import aggregate, score_run


def _multi_case(case_id: str = "none-rej-01") -> RoutingCase:
    return RoutingCase(
        id=case_id,
        prompt="先创建 out/state.txt，写入第一轮状态",
        expect_artifacts=["out/state.txt"],
        expect_workspace_files=["out/state.txt"],
        turns=[
            FullEvalTurn(
                prompt="沿用刚才的状态，再创建 out/final.md",
                expect_artifacts=["out/final.md"],
                expect_workspace_files=["out/state.txt", "out/final.md"],
                expect_assertions=["回答正确沿用了第一轮状态"],
                requires_context=True,
            )
        ],
    )


def test_case顶层是第一轮_turns从第二轮开始():
    case = _multi_case()

    assert case.turn_count == 2
    assert case.resolved_turn(1).prompt.startswith("先创建")
    assert case.resolved_turn(2).requires_context
    with pytest.raises(IndexError):
        case.resolved_turn(3)


def test_matrix展开turn且同conversation复用session():
    case = _multi_case()
    tasks = build_matrix(
        suite_id="full-multi",
        cases=[case],
        models=[{"id": "m"}],
        repeats=2,
        execution_id="exec",
    )

    assert [task.turn_index for task in tasks] == [1, 2, 1, 2]
    assert [task.request_id for task in tasks] == [
        "full-multi.m.none-rej-01.t1.r0",
        "full-multi.m.none-rej-01.t2.r0",
        "full-multi.m.none-rej-01.t1.r1",
        "full-multi.m.none-rej-01.t2.r1",
    ]
    assert tasks[0].session_id == tasks[1].session_id
    assert tasks[2].session_id == tasks[3].session_id
    assert tasks[0].session_id != tasks[2].session_id


class _ParallelConversationRuntime:
    name = "parallel-capture"
    version = "test"

    def __init__(self, conversations: int):
        self.barrier = threading.Barrier(conversations, timeout=3)
        self.lock = threading.Lock()
        self.requests = []
        self.workspaces: dict[tuple[str, int], list[str]] = {}

    def run(self, request):
        workspace = Path(request.environment.host_workspace)
        key = (request.case_id, request.repeat_index)
        with self.lock:
            self.requests.append(request)
            self.workspaces.setdefault(key, []).append(str(workspace))

        if request.turn_index == 1:
            # 两段独立 conversation 必须真并发到这里；若 runner 仍是串行会超时。
            self.barrier.wait()
            (workspace / "out").mkdir()
            (workspace / "out/state.txt").write_text(
                request.session_id or "missing", encoding="utf-8"
            )
            artifacts = [{
                "path": "out/state.txt",
                "sha256": "sha256:state",
                "size_bytes": 5,
                "mime_type": "text/plain",
            }]
        else:
            assert (workspace / "out/state.txt").is_file()
            (workspace / "out/final.md").write_text("done", encoding="utf-8")
            artifacts = [{
                "path": "out/final.md",
                "sha256": "sha256:final",
                "size_bytes": 4,
                "mime_type": "text/markdown",
            }]
        return RunResult(
            case_id=request.case_id,
            repeat_index=request.repeat_index,
            turn_index=request.turn_index,
            model=request.model["id"],
            final_answer=f"turn {request.turn_index}",
            artifacts=artifacts,
        )

    def capabilities(self):
        return RuntimeCapabilities(
            runtime=self.name,
            skill_modes=["full"],
            multi_turn=True,
            workspace=True,
        )

    def fingerprint(self):
        return {"runtime": self.name}


def test_parallelism按conversation并发且turn共享workspace(tmp_path, monkeypatch):
    cases = [_multi_case("none-rej-01")]
    tasks = build_matrix(
        suite_id="full-multi",
        cases=cases,
        models=[{"id": "m"}],
        repeats=2,
        execution_id="exec",
    )
    suite = {
        "suite_id": "full-multi",
        "suite_version": "1",
        "dataset": "missing.jsonl",
        "skills": {"mode": "full", "cfg": "v1"},
        "models": [{"id": "m"}],
        "repeats": 2,
        "parallelism": 2,
    }
    output = tmp_path / "run"
    monkeypatch.setattr("workflows.run_routing.run_dir", lambda *_args: output)
    runtime = _ParallelConversationRuntime(conversations=2)

    run_one(
        suite,
        {"id": "m"},
        [],
        tasks,
        runtime,
        False,
        "sha256:dataset",
        environment=LocalEnvironmentBackend(),
    )

    persisted = [
        json.loads(line)
        for line in (output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    # 并发完成顺序不固定，但落盘必须恢复矩阵顺序。
    assert [row["request_id"] for row in persisted] == [
        task.request_id for task in tasks
    ]
    assert all(row["ok"] for row in persisted)
    for paths in runtime.workspaces.values():
        assert len(set(paths)) == 1, "同一 conversation 的 turns 必须共享 workspace"
    assert len({paths[0] for paths in runtime.workspaces.values()}) == 2, (
        "不同 repeat 必须使用不同 workspace"
    )
    sessions = {
        key: {request.session_id for request in runtime.requests
              if (request.case_id, request.repeat_index) == key}
        for key in runtime.workspaces
    }
    assert all(len(values) == 1 for values in sessions.values())
    assert len({next(iter(values)) for values in sessions.values()}) == 2
    assert "out/state.txt" in persisted[1]["workspace_files"]
    assert "out/final.md" in persisted[1]["workspace_files"]


class _FirstTurnFails:
    name = "first-turn-fails"
    version = "test"

    def __init__(self):
        self.calls = []

    def run(self, request):
        self.calls.append(request.turn_index)
        return RunResult(
            case_id=request.case_id,
            repeat_index=request.repeat_index,
            turn_index=request.turn_index,
            model="m",
            status="failed",
            error="task failed",
            error_kind="task",
        )

    def capabilities(self):
        return RuntimeCapabilities(
            runtime=self.name,
            skill_modes=["full"],
            multi_turn=True,
            workspace=True,
        )

    def fingerprint(self):
        return {}


def test_前一轮失败后续轮标skipped且不调用runtime(tmp_path, monkeypatch):
    case = _multi_case()
    tasks = build_matrix(
        suite_id="s", cases=[case], models=[{"id": "m"}],
        repeats=1, execution_id="e",
    )
    output = tmp_path / "run"
    monkeypatch.setattr("workflows.run_routing.run_dir", lambda *_args: output)
    runtime = _FirstTurnFails()

    run_one(
        {
            "suite_id": "s", "suite_version": "1", "dataset": "missing",
            "skills": {"mode": "full", "cfg": "v1"}, "parallelism": 2,
        },
        {"id": "m"}, [], tasks, runtime, False, "sha256:d",
        environment=LocalEnvironmentBackend(),
    )

    rows = [
        json.loads(line)
        for line in (output / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert runtime.calls == [1]
    assert rows[0]["status"] == "failed"
    assert rows[1]["status"] == "skipped"
    assert "previous turn t1" in rows[1]["skip_reason"]


def test_multiturn指标按conversation计且skipped不重复扣分():
    case = _multi_case()
    catalog: set[str] = set()
    passed = [
        score_run({
            "case_id": case.id, "repeat_index": 0, "turn_index": 1,
            "session_id": "session-a", "ok": True,
            "artifacts": [{"path": "out/state.txt", "size_bytes": 2,
                           "mime_type": "text/plain"}],
            "workspace_files": ["out/state.txt"],
        }, case, catalog),
        score_run({
            "case_id": case.id, "repeat_index": 0, "turn_index": 2,
            "session_id": "session-a", "ok": True,
            "artifacts": [{"path": "out/final.md", "size_bytes": 2,
                           "mime_type": "text/markdown"}],
            "workspace_files": ["out/state.txt", "out/final.md"],
        }, case, catalog),
    ]
    scores = aggregate(pd.DataFrame(passed))
    assert scores["task_completion"] == 1.0
    assert scores["turn_completion"] == 1.0
    assert scores["session_continuity"] == 1.0
    assert scores["context_retention"] == 1.0
    assert scores["file_state_continuity"] == 1.0

    failed_then_skipped = [
        {**passed[0], "ok": False, "done": False, "error_kind": "task"},
        {**passed[1], "ok": False, "done": False, "skipped": True},
    ]
    failed_scores = aggregate(pd.DataFrame(failed_then_skipped))
    assert failed_scores["task_completion"] == 0.0
    assert failed_scores["turn_completion"] == 0.0


def test_judge按turn读取断言并看到此前对话():
    case = _multi_case()
    run = RunResult(
        case_id=case.id,
        repeat_index=0,
        turn_index=2,
        model="m",
        final_answer="我沿用了第一轮状态",
    )
    history = [
        {"role": "user", "content": case.prompt},
        {"role": "assistant", "content": "已创建 state.txt"},
    ]
    prompt = build_grading_prompt(case, run, history=history)
    assert "此前对话" in prompt
    assert "沿用刚才的状态" in prompt
    assert "回答正确沿用了第一轮状态" in prompt

    def judge(**_kwargs):
        return json.dumps({"expectations": [{
            "text": "回答正确沿用了第一轮状态",
            "passed": True,
            "evidence": "沿用了第一轮状态",
        }]}, ensure_ascii=False)

    grading = grade_run(
        case,
        run,
        judge=SuiteJudgeSpec(id="j", model="judge"),
        completion=judge,
        history=history,
    )
    assert grading.turn_index == 2
    assert grading.pass_rate == 1.0


def test_score_full端到端输出多轮指标和逐轮表(tmp_path, monkeypatch):
    from workflows import score_full

    case = _multi_case()
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(case.model_dump_json() + "\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.snapshot.yaml").write_text(
        "\n".join([
            "suite:",
            f"  dataset: {dataset}",
            "  suite_id: full-multi",
            "  suite_version: '1'",
            "  skills:",
            "    mode: full",
            "    cfg: v1",
            "  scoring:",
            "    metrics: [task_completion, turn_completion]",
            "    gate:",
            "      task_completion: '>= 1.0'",
            "skill_catalog: []",
            "config_hash: sha256:test",
            "mock: false",
        ]) + "\n",
        encoding="utf-8",
    )
    runs = [
        RunResult(
            request_id="s.m.c.t1.r0",
            session_id="session-a",
            case_id=case.id,
            repeat_index=0,
            turn_index=1,
            model="m",
            artifacts=[{
                "path": "out/state.txt", "sha256": "sha256:state",
                "size_bytes": 2, "mime_type": "text/plain",
            }],
            workspace_files=["out/state.txt"],
        ),
        RunResult(
            request_id="s.m.c.t2.r0",
            session_id="session-a",
            case_id=case.id,
            repeat_index=0,
            turn_index=2,
            model="m",
            artifacts=[{
                "path": "out/final.md", "sha256": "sha256:final",
                "size_bytes": 2, "mime_type": "text/markdown",
            }],
            workspace_files=["out/state.txt", "out/final.md"],
        ),
    ]
    (run_dir / "runs.jsonl").write_text(
        "\n".join(run.model_dump_json() for run in runs) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(score_full, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["score_full", "--dir", str(run_dir)])

    score_full.main()

    payload = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
    assert payload["scores"]["task_completion"] == 1.0
    assert payload["scores"]["turn_completion"] == 1.0
    assert payload["scores"]["session_continuity"] == 1.0
    assert payload["n_conversations"] == 1
    assert set(payload["per_turn"]) == {
        f"{case.id}.t1",
        f"{case.id}.t2",
    }
