"""N3 Matrix Builder 验收测试（AGENTS.md §9.3）。"""
from __future__ import annotations

from time import perf_counter

import pytest

from contracts import RoutingCase
from workflows.matrix import build_matrix


def _cases(n: int) -> list[RoutingCase]:
    return [RoutingCase(id=f"pdf-pos-{i:05d}", prompt=f"case {i}") for i in range(n)]


def _models(n: int) -> list[dict]:
    return [{"id": f"model-{i}", "model": f"provider/model-{i}"} for i in range(n)]


def test_任务数等于矩阵乘积且顺序确定():
    kw = dict(
        suite_id="demo",
        cases=_cases(2),
        models=_models(2),
        repeats=2,
        execution_id="exec-1",
    )
    a = build_matrix(**kw)
    b = build_matrix(**kw)

    assert len(a) == 2 * 2 * 2
    assert [task.request_id for task in a] == [task.request_id for task in b]
    assert [(task.model_id, task.case.id, task.repeat_index) for task in a] == [
        ("model-0", "pdf-pos-00000", 0),
        ("model-0", "pdf-pos-00000", 1),
        ("model-0", "pdf-pos-00001", 0),
        ("model-0", "pdf-pos-00001", 1),
        ("model-1", "pdf-pos-00000", 0),
        ("model-1", "pdf-pos-00000", 1),
        ("model-1", "pdf-pos-00001", 0),
        ("model-1", "pdf-pos-00001", 1),
    ]


def test_每个任务和_repeat_都有唯一_id_及_session():
    tasks = build_matrix(
        suite_id="demo",
        cases=_cases(3),
        models=_models(2),
        repeats=3,
        execution_id="exec-1",
    )
    assert len({task.request_id for task in tasks}) == len(tasks)
    assert len({task.session_id for task in tasks}) == len(tasks)


def test_重新执行保留任务_id_但使用新_session():
    common = dict(suite_id="demo", cases=_cases(1), models=_models(1), repeats=2)
    first = build_matrix(**common, execution_id="exec-1")
    rerun = build_matrix(**common, execution_id="exec-2")

    assert [task.request_id for task in first] == [task.request_id for task in rerun]
    assert {task.session_id for task in first}.isdisjoint(task.session_id for task in rerun)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"repeats": 0}, "repeats"),
        ({"cases": []}, "case"),
        ({"models": []}, "model"),
        ({"suite_id": ""}, "suite_id"),
        ({"execution_id": ""}, "execution_id"),
        ({"models": [{"id": ""}]}, "model"),
        ({"cases": [*_cases(1), *_cases(1)]}, "case id"),
        ({"models": [*_models(1), *_models(1)]}, "model id"),
    ],
)
def test_非法矩阵不会静默丢任务(overrides, message):
    args = dict(
        suite_id="demo",
        cases=_cases(1),
        models=_models(1),
        repeats=1,
        execution_id="exec-1",
    )
    args.update(overrides)
    with pytest.raises(ValueError, match=message):
        build_matrix(**args)


def test_一万任务展开无明显阻塞():
    started = perf_counter()
    tasks = build_matrix(
        suite_id="large",
        cases=_cases(1000),
        models=_models(2),
        repeats=5,
        execution_id="exec-1",
    )
    elapsed = perf_counter() - started

    assert len(tasks) == 10_000
    assert elapsed < 2.0
