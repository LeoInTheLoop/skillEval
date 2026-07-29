"""语义断言判定（grade.py）：judge 与被测模型解耦 + 静默变空的防护。

全部用注入的假 judge，**不调用任何真实模型**。
"""
from __future__ import annotations

import argparse
import json

import pytest
import yaml

from contracts import RoutingCase, RunResult, SuiteJudgeSpec
from workflows.grade import (
    build_grading_prompt,
    grade_run,
    grade_run_dir,
    load_grading,
    resolve_judge,
    validate_expectations,
)

JUDGE = SuiteJudgeSpec(id="glm5", model="openai/glm-5.1")


def _case(**kw):
    base = dict(id="dp-pos-01", prompt="做一份季度报告",
                expect_assertions=["报告包含季度营收数字"])
    return RoutingCase(**{**base, **kw})


def _run(**kw):
    base = dict(case_id="dp-pos-01", repeat_index=0, model="qwen",
                final_answer="已生成报告，Q3 营收 1.2 亿")
    return RunResult(**{**base, **kw})


def _judge_returns(expectations):
    def fake(**kw):
        return json.dumps({"expectations": expectations}, ensure_ascii=False)
    return fake


# --- judge 与被测模型解耦 ---

def test_judge_默认走独立的_JUDGE_环境变量():
    """重点就在这：judge 是量具，不该跟被测模型共用凭据。"""
    assert JUDGE.api_base_env == "JUDGE_BASE_URL"
    assert JUDGE.api_key_env == "JUDGE_API_KEY"


def test_judge_可以指向另一个_provider_的_env():
    other = SuiteJudgeSpec(id="gpt", model="openai/gpt-x",
                           api_base_env="OPENAI_BASE_URL", api_key_env="OPENAI_API_KEY")
    assert other.api_key_env == "OPENAI_API_KEY"


def test_judge_的_env_名会被原样传给调用层():
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return json.dumps({"expectations": [
            {"text": "报告包含季度营收数字", "passed": True, "evidence": "Q3 营收 1.2 亿"}]})

    grade_run(_case(), _run(), judge=JUDGE, completion=fake)
    assert seen["api_base_env"] == "JUDGE_BASE_URL"
    assert seen["api_key_env"] == "JUDGE_API_KEY"
    assert seen["model"] == "openai/glm-5.1"


def test_suite_没配_judge_也没给_CLI_直接报错():
    args = argparse.Namespace(judge_id=None, judge_model=None, judge_api_base_env=None,
                              judge_api_key_env=None, dimensions=None)
    with pytest.raises(SystemExit, match="没配 scoring.judge"):
        resolve_judge({"suite": {"scoring": {}}}, args)


def test_换_judge_模型不换_id_会被拒绝():
    """否则新结果会覆盖旧 judge 的产物，而文件名看不出区别。"""
    snap = {"suite": {"scoring": {"judge": {"id": "qwen", "model": "openai/qwen3.7-max"}}}}
    args = argparse.Namespace(judge_id=None, judge_model="openai/glm-5.1",
                              judge_api_base_env=None, judge_api_key_env=None,
                              dimensions=None)
    with pytest.raises(SystemExit, match="必须同时给 --judge-id"):
        resolve_judge(snap, args)


def test_routing_only_通用语义维度在CLI层被拒绝(tmp_path, monkeypatch):
    """路由 JSON 不是用户任务的最终回答，不能用 answer-quality rubric 评分。"""
    from workflows import grade

    d = _write_run_dir(tmp_path, [_run()], [_case()])
    snapshot = yaml.safe_load((d / "config.snapshot.yaml").read_text(encoding="utf-8"))
    snapshot["suite"]["skills"] = {"mode": "routing_only"}
    (d / "config.snapshot.yaml").write_text(yaml.safe_dump(snapshot), encoding="utf-8")
    monkeypatch.setattr("sys.argv", [
        "grade", "--dir", str(d), "--judge-id", "glm5", "--judge-model", "openai/glm-5.1",
        "--dimensions", "relevancy",
    ])
    with pytest.raises(SystemExit, match="routing-only"):
        grade.main()


def test_CLI_能整体覆盖_suite_里的_judge():
    snap = {"suite": {"scoring": {"judge": {"id": "qwen", "model": "openai/qwen3.7-max"}}}}
    args = argparse.Namespace(judge_id="glm5", judge_model="openai/glm-5.1",
                              judge_api_base_env=None, judge_api_key_env=None,
                              dimensions=None)
    judge = resolve_judge(snap, args)
    assert (judge.id, judge.model) == ("glm5", "openai/glm-5.1")


# --- 静默变空的防护：judge 少判一条会抬高通过率 ---

def test_judge_漏判一条会报错而不是静默缩小分母():
    case = _case(expect_assertions=["断言A", "断言B"])
    fake = _judge_returns([{"text": "断言A", "passed": True, "evidence": "e"}])
    with pytest.raises(ValueError, match="必须逐字同序"):
        grade_run(case, _run(), judge=JUDGE, completion=fake)


def test_judge_改写断言文本会报错():
    fake = _judge_returns([{"text": "报告里有营收", "passed": True, "evidence": "e"}])
    with pytest.raises(ValueError, match="必须逐字同序"):
        grade_run(_case(), _run(), judge=JUDGE, completion=fake)


def test_judge_多判一条会报错():
    fake = _judge_returns([
        {"text": "报告包含季度营收数字", "passed": True, "evidence": "e"},
        {"text": "自己加的一条", "passed": True, "evidence": "e"}])
    with pytest.raises(ValueError, match="必须逐字同序"):
        grade_run(_case(), _run(), judge=JUDGE, completion=fake)


def test_顺序被打乱也算不一致():
    case = _case(expect_assertions=["A", "B"])
    fake = _judge_returns([{"text": "B", "passed": True, "evidence": "e"},
                           {"text": "A", "passed": True, "evidence": "e"}])
    with pytest.raises(ValueError):
        grade_run(case, _run(), judge=JUDGE, completion=fake)


def test_证据不能为空():
    from pydantic import ValidationError
    fake = _judge_returns([{"text": "报告包含季度营收数字", "passed": True, "evidence": ""}])
    with pytest.raises(ValidationError):
        grade_run(_case(), _run(), judge=JUDGE, completion=fake)


def test_逐条判定后算出通过率():
    case = _case(expect_assertions=["A", "B", "C"])
    fake = _judge_returns([{"text": "A", "passed": True, "evidence": "e"},
                           {"text": "B", "passed": False, "evidence": "缺证据"},
                           {"text": "C", "passed": True, "evidence": "e"}])
    g = grade_run(case, _run(), judge=JUDGE, completion=fake)
    assert (g.passed, g.failed, g.total, g.pass_rate) == (2, 1, 3, 0.6667)


# --- prompt 里必须说清产物内容不可见 ---

def test_文本产物的内容进_prompt():
    """判「报告里的数字是不是编的」必须看到产物原文，只给文件名判不了。"""
    run = _run(artifacts=[{"path": "out/r.md", "sha256": "a" * 64, "size_bytes": 120,
                           "mime_type": "text/markdown",
                           "text_excerpt": "# 周报\n毛利率 32%"}])
    prompt = build_grading_prompt(_case(), run)
    assert "out/r.md" in prompt
    assert "毛利率 32%" in prompt


def test_输入文件原文进_prompt(tmp_path):
    """判「有没有编造」必须看到输入。看不到输入的 judge 会把原文里真有的人名判成幻觉。"""
    transcript = tmp_path / "会议记录.txt"
    transcript.write_text("发言人1：框架 8 月 10 号之前，赵磊负责。", encoding="utf-8")

    prompt = build_grading_prompt(
        _case(files=[str(transcript)],
              expect_assertions=["没有出现文字稿里不存在的人名"]),
        _run(final_answer="行动项：绩效方案框架 —— 责任人 赵磊"),
    )

    assert "赵磊负责" in prompt
    assert "会议记录.txt" in prompt


def test_二进制产物仍然写明内容不可见():
    """docx/png 解不出文本，judge 不说清楚就会凭文件名猜一个 passed。"""
    run = _run(artifacts=[{"path": "out/r.docx", "sha256": "a" * 64, "size_bytes": 9000,
                           "mime_type": "application/vnd.openxmlformats-officedocument"
                                        ".wordprocessingml.document"}])
    prompt = build_grading_prompt(_case(), run)
    assert "out/r.docx" in prompt
    assert "内容不可见" in prompt


# --- 整目录判定 ---

def _write_run_dir(tmp_path, runs, cases):
    (tmp_path / "runs.jsonl").write_text(
        "\n".join(r.model_dump_json() for r in runs) + "\n", encoding="utf-8")
    ds = tmp_path / "cases.jsonl"
    ds.write_text("\n".join(c.model_dump_json() for c in cases) + "\n", encoding="utf-8")
    (tmp_path / "config.snapshot.yaml").write_text(
        yaml.safe_dump({"suite": {"dataset": str(ds)}, "config_hash": "sha256:abc"}),
        encoding="utf-8")
    return tmp_path


def test_系统故障不送去判(tmp_path):
    """模型根本没跑，判它「没完成任务」是把评测系统的锅记到 skill 头上。"""
    runs = [_run(repeat_index=0),
            _run(repeat_index=1, status="failed", error="boom", error_kind="network")]
    d = _write_run_dir(tmp_path, runs, [_case()])
    fake = _judge_returns([{"text": "报告包含季度营收数字", "passed": True, "evidence": "e"}])
    report = grade_run_dir(d, judge=JUDGE, completion=fake)
    assert len(report.graded) == 1
    assert report.n_skipped_system_failure == 1


def test_没写断言的题跳过而不是判零分(tmp_path):
    cases = [_case(), _case(id="dp-pos-02", prompt="另一题", expect_assertions=[])]
    runs = [_run(), _run(case_id="dp-pos-02")]
    d = _write_run_dir(tmp_path, runs, cases)
    fake = _judge_returns([{"text": "报告包含季度营收数字", "passed": True, "evidence": "e"}])
    report = grade_run_dir(d, judge=JUDGE, completion=fake)
    assert report.n_skipped_nothing_to_judge == 1
    assert report.pass_rate == 1.0


def test_一条都判不了时通过率是_None_不是零(tmp_path):
    d = _write_run_dir(tmp_path, [_run(case_id="dp-pos-02")],
                       [_case(id="dp-pos-02", expect_assertions=[])])
    report = grade_run_dir(d, judge=JUDGE, completion=_judge_returns([]))
    assert report.pass_rate is None


def test_判定结果记下是谁判的(tmp_path):
    d = _write_run_dir(tmp_path, [_run()], [_case()])
    fake = _judge_returns([{"text": "报告包含季度营收数字", "passed": True, "evidence": "e"}])
    report = grade_run_dir(d, judge=JUDGE, completion=fake)
    assert report.judge.id == "glm5"
    assert report.judge.model == "openai/glm-5.1"
    assert report.judge.system_prompt_hash.startswith("sha256:")
    assert report.config_hash == "sha256:abc"


def test_单条judge失败不终止整批且明确归类(tmp_path):
    runs = [_run(repeat_index=0), _run(repeat_index=1)]
    d = _write_run_dir(tmp_path, runs, [_case()])
    calls = 0

    def flaky(**_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("judge timeout")
        return json.dumps({"expectations": [
            {"text": "报告包含季度营收数字", "passed": True, "evidence": "Q3 营收 1.2 亿"}
        ]})

    events = []
    report = grade_run_dir(
        d, judge=JUDGE, completion=flaky,
        progress=lambda *event: events.append(event),
    )
    assert len(report.graded) == 1
    assert report.judge_failures[0].error == "ConnectionError('judge timeout')"
    assert any(event[2] == "failed" for event in events)
    assert any(event[2] == "ok" for event in events)


# --- load_grading：给 score_*.py 读 ---

def test_没跑过_grade_返回_None_而不是零(tmp_path):
    snap = {"suite": {"scoring": {"judge": {"id": "glm5"}}}}
    assert load_grading(tmp_path, snap, None) is None


def test_多个_judge_结果并存时必须显式指定用哪个(tmp_path):
    for jid in ("glm5", "qwen"):
        (tmp_path / f"grading.{jid}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="明确指定用哪把尺子"):
        load_grading(tmp_path, {"suite": {"scoring": {}}}, None)


def test_按_judge_id_取对应那份(tmp_path):
    (tmp_path / "grading.glm5.json").write_text('{"pass_rate": 0.9}', encoding="utf-8")
    (tmp_path / "grading.qwen.json").write_text('{"pass_rate": 0.7}', encoding="utf-8")
    assert load_grading(tmp_path, {}, "qwen")["pass_rate"] == 0.7


def test_不同_judge_的结果互不覆盖(tmp_path):
    from workflows.grade import grading_path
    assert grading_path(tmp_path, "glm5") != grading_path(tmp_path, "qwen")
