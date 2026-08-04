"""跨 repeat 的统计、效率维度与题目质量诊断（metrics.py）。

盯的是同一类错误：**把「没测」算成 0**。方差、token、tool 次数缺一个来源，
指标就会静默变成一个看起来合理、实际没有依据的数字。
"""
from __future__ import annotations

from workflows import metrics


# --- stats：mean ± stddev ---

def test_单点样本方差为零而不是报错():
    assert metrics.stats([0.8]) == {"mean": 0.8, "stddev": 0.0, "min": 0.8, "max": 0.8}


def test_三次重复能区分同均值不同方差():
    """这正是只报 mean 时看不出来的：两组都是 80%，稳定性天差地别。"""
    steady = metrics.stats([0.8, 0.8, 0.8])
    swingy = metrics.stats([1.0, 1.0, 0.4])
    assert steady["mean"] == swingy["mean"] == 0.8
    assert steady["stddev"] == 0.0
    assert swingy["stddev"] > 0.3


def test_空序列返回_None_而不是零():
    # 0 的意思是「测了，得零分」；None 的意思是「没测」。混淆这两者会让 gate 假 FAIL
    assert metrics.stats([]) is None
    assert metrics.stats([None, None]) is None


def test_None_不参与统计():
    assert metrics.stats([1.0, None, 3.0])["mean"] == 2.0


# --- efficiency：token 的两种来源 ---

def test_openclaw_的_total_tokens_直接用():
    out = metrics.efficiency([{"usage": {"total_tokens": 500, "input_tokens": 100,
                                         "output_tokens": 400}}])
    assert out["tokens"]["mean"] == 500


def test_litellm_只给_input_output_要自己相加():
    out = metrics.efficiency([{"usage": {"input_tokens": 100, "output_tokens": 400}}])
    assert out["tokens"]["mean"] == 500


def test_没有_usage_时_token_记_NA_不记零():
    out = metrics.efficiency([{"usage": {}}, {}])
    assert out["tokens"] is None


def test_duration_ms_换算成秒():
    out = metrics.efficiency([{"duration_ms": 1500}, {"duration_ms": 2500}])
    assert out["time_seconds"]["mean"] == 2.0
    assert out["time_seconds"]["min"] == 1.5


def test_错误率按_error_kind_统计():
    out = metrics.efficiency([{"error_kind": "task"}, {}, {}, {}])
    assert out["errors"]["mean"] == 0.25


# --- tool_calls：「用过几种」不等于「调了几次」 ---

def test_tool_次数取聚合总数而不是种类数():
    """OpenClaw 只报聚合 toolSummary；用 len(tool_calls) 会把 7 次调用记成 2 种。"""
    run = {"usage": {"tool_calls_total": 7},
           "tool_calls": [{"name": "write"}, {"name": "read"}]}
    assert metrics.efficiency([run])["tool_calls"]["mean"] == 7


def test_逐次粒度的_runtime_按_count_求和():
    run = {"usage": {}, "tool_calls": [{"name": "write", "count": 3},
                                       {"name": "read", "count": 2}]}
    assert metrics.efficiency([run])["tool_calls"]["mean"] == 5


def test_count_为_None_按一次下限计而不是零():
    # None = 「该 runtime 只报告用过、没报次数」。当 0 处理会让整列失真
    run = {"usage": {}, "tool_calls": [{"name": "write", "count": None}]}
    assert metrics.efficiency([run])["tool_calls"]["mean"] == 1


def test_确实没调_tool_记零而不是_NA():
    assert metrics.efficiency([{"usage": {}, "tool_calls": []}])["tool_calls"]["mean"] == 0


# --- flaky：同题跨 repeat 结果不一致 ---

def test_同题三次结果不一致会被标出来():
    rows = [{"case_id": "a-pos-01", "done": True},
            {"case_id": "a-pos-01", "done": False},
            {"case_id": "a-pos-01", "done": True},
            {"case_id": "a-pos-02", "done": True},
            {"case_id": "a-pos-02", "done": True}]
    flaky = metrics.flaky_cases(rows, key="done")
    assert [f["case_id"] for f in flaky] == ["a-pos-01"]
    assert flaky[0] == {"case_id": "a-pos-01", "passed": 2, "runs": 3, "pass_rate": 0.6667}


def test_只跑一次的题不下_flaky_结论():
    assert metrics.flaky_cases([{"case_id": "a-pos-01", "done": False}], key="done") == []


def test_pass_at_k_和_pass_all_k_区分峰值与稳定性():
    rows = [
        {"case_id": "a-pos-01", "repeat": 0, "done": True},
        {"case_id": "a-pos-01", "repeat": 1, "done": False},
        {"case_id": "a-pos-01", "repeat": 2, "done": True},
        {"case_id": "a-pos-02", "repeat": 0, "done": True},
        {"case_id": "a-pos-02", "repeat": 1, "done": True},
        {"case_id": "a-pos-02", "repeat": 2, "done": True},
    ]
    assert metrics.pass_at_k(rows, key="done", k=3) == 1.0
    assert metrics.pass_all_k(rows, key="done", k=3) == 0.5


def test_pass_k_缺少完整_repeat_时记_NA():
    rows = [{"case_id": "a-pos-01", "repeat": 0, "done": True}]
    assert metrics.pass_at_k(rows, key="done", k=3) is None
    assert metrics.pass_all_k(rows, key="done", k=3) is None


# --- non_discriminating：有无 skill 都满分的题 ---

def test_两边都满分的题不判别_skill():
    base = {"a-pos-01": 1.0, "a-pos-02": 0.3}
    treat = {"a-pos-01": 1.0, "a-pos-02": 1.0}
    assert metrics.non_discriminating(base, treat) == ["a-pos-01"]


def test_只在一边出现的题不参与判定():
    """路由的 none 基线会换一份 gold 不同的数据集，题对不上时不能瞎判。"""
    assert metrics.non_discriminating({"only-base-01": 1.0}, {"only-treat-01": 1.0}) == []


# --- 展示：N/A 不许显示成 0 ---

def test_NA_显示成_NA_不显示成零():
    assert metrics.format_stats(None) == "N/A"
    assert metrics.format_stats({"mean": 0.8, "stddev": 0.1}, percent=True) == "80.0% ± 10.0%"
