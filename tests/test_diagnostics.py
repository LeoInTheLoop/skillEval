"""单一推导点：pass/fail/indeterminate 只能从结构化 counts 算（HANDOFF ★ 更新 16）。"""
from __future__ import annotations

from workflows.diagnostics import derive_verdict, system_failure_counts


def test_全部系统故障判_indeterminate_不是_fail():
    """docker-t1 的真实形状：4/4 都是 network 失败，即使历史 gate_pass 存的是 False。"""
    outcome = derive_verdict(
        n_runs=4, n_system_failures=4, system_failures_by_kind={"network": 4},
        observed_passed=False,  # 模拟 stale scores.json 里错误算出来的 FAIL
        is_mock=False,
    )
    assert outcome["quality_verdict"] == "indeterminate"
    assert outcome["gate_pass"] is None
    assert "network" in outcome["reason"]
    assert "4" in outcome["reason"]


def test_没有_run_也判_indeterminate():
    outcome = derive_verdict(n_runs=0, n_system_failures=0, observed_passed=None)
    assert outcome["quality_verdict"] == "indeterminate"


def test_部分系统故障不短路_照常看_gate():
    """只有一部分是系统故障时，真实评出来的 gate 结果要保留，不能被误伤成 indeterminate。"""
    outcome = derive_verdict(
        n_runs=4, n_system_failures=1, system_failures_by_kind={"network": 1},
        observed_passed=True,
    )
    assert outcome["quality_verdict"] == "pass"
    assert outcome["gate_pass"] is True


def test_gate一条都判不了也是_indeterminate():
    outcome = derive_verdict(n_runs=4, n_system_failures=0, observed_passed=None)
    assert outcome["quality_verdict"] == "indeterminate"
    assert outcome["gate_pass"] is None


def test_mock_run忽略_observed_passed_只报_not_evaluated():
    outcome = derive_verdict(
        n_runs=4, n_system_failures=4, system_failures_by_kind={"network": 4},
        observed_passed=True, is_mock=True,
    )
    assert outcome["quality_verdict"] == "not_evaluated"


def test_indeterminate不能被静默折叠成_pass_或_fail():
    """gate_pass 为 None 时不能被 bool(None) 之类的写法悄悄当成 False。"""
    outcome = derive_verdict(
        n_runs=2, n_system_failures=2, system_failures_by_kind={"runtime": 2},
        observed_passed=False,
    )
    assert outcome["gate_pass"] is None
    assert outcome["quality_verdict"] not in ("pass", "fail")


def test_system_failure_counts只数结构化_error_kind_不猜文本():
    runs = [
        {"error_kind": "network"},
        {"error_kind": "task"},
        {"error_kind": "runtime"},
        {"error_kind": None},
        {},
    ]
    n, by_kind = system_failure_counts(runs)
    assert n == 2
    assert by_kind == {"network": 1, "runtime": 1}
