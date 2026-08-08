"""标准评估维度（dimensions.py）+ grade.py 的维度判定路径。

维度是「跨题通用的尺子」，所以这里盯两件事：
  1. 判不了的维度记 N/A，**不进分母** —— 否则「10 题里只有 3 题有参考答案」
     会把 correctness 直接腰斩；
  2. judge 漏判/多判一个维度必须报错 —— 那会让某个维度的分母静默变小。
"""
from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from workflows import dimensions as dims
from contracts import RoutingCase, RunResult, SuiteJudgeSpec
from workflows.grade import build_grading_prompt, grade_run, grade_run_dir, validate_dimensions


def _case(**kw):
    base = dict(id="dp-pos-01", prompt="按三个大区出季度报告")
    return RoutingCase(**{**base, **kw})


def _run(**kw):
    base = dict(case_id="dp-pos-01", repeat_index=0, model="qwen",
                final_answer="华东 1.2 亿，华北 0.8 亿，华南 0.5 亿")
    return RunResult(**{**base, **kw})


def _judge(scores: dict[str, float], expectations=()):
    """假 judge：按传入的 {维度: 分} 应答，顺序跟着 prompt 里请求的顺序走。"""
    def fake(**kw):
        requested = kw["prompt"].split("[待打分的维度]\n")[1].split("\n\n[")[0] \
            if "[待打分的维度]" in kw["prompt"] else ""
        order = [line[2:].split("（")[0] for line in requested.splitlines()
                 if line.startswith("- ")]
        return json.dumps({
            "expectations": list(expectations),
            "dimensions": [{"dimension": d, "score": scores[d], "evidence": "证据"}
                           for d in order],
        }, ensure_ascii=False)
    return fake


# --- 维度定义本身 ---

def test_默认维度都在标准集里():
    assert set(dims.DEFAULT_DIMENSIONS) <= set(dims.STANDARD_DIMENSIONS)


def test_每个维度都注明了来源():
    """来源是给「换库/对表」用的：没有它，rubric 就成了拍脑袋写的。"""
    for d in dims.STANDARD_DIMENSIONS.values():
        assert d.source, f"{d.id} 没写 source"
        assert d.rubric and "1.0" in d.rubric and "0.0" in d.rubric


def test_维度定义不可变():
    """rubric 被就地改掉 = 同一个版本号下判定标准变了，必须挡住。"""
    with pytest.raises(ValidationError):
        dims.STANDARD_DIMENSIONS["faithfulness"].version = "v2"


def test_拼错维度名直接报错不静默跳过():
    with pytest.raises(ValueError, match="未知的评估维度"):
        dims.resolve(["faithfullness"])          # 少一个 e


def test_suite_里拼错维度名在运行前就被拒():
    with pytest.raises(ValidationError):
        SuiteJudgeSpec(id="j", model="m", dimensions=["not_a_dimension"])


def test_可读性维度乱码符号一票否决():
    """用户明确要求：乱码/莫名其妙符号是必改项，不能被结构和用词两项拉平均分抵消。"""
    d = dims.STANDARD_DIMENSIONS["readability"]
    assert "乱码" in d.rubric["0.0"] and "符号" in d.rubric["0.0"]


def test_维度指纹带版本():
    fp = dims.fingerprint(dims.resolve(["faithfulness", "completeness"]))
    assert fp == {"faithfulness": "v1", "completeness": "v1"}


# --- 适用性：判不了的记 N/A，不是 0 分 ---

def test_没有参考答案就判不了_correctness():
    d = dims.STANDARD_DIMENSIONS["correctness"]
    assert not dims.applicable(d, has_reference=False, has_artifacts=True)
    assert dims.applicable(d, has_reference=True, has_artifacts=True)


def test_不需要参考答案的维度不受影响():
    d = dims.STANDARD_DIMENSIONS["faithfulness"]
    assert dims.applicable(d, has_reference=False, has_artifacts=False)


def test_缺参考答案时_correctness_被跳过而不是给零分():
    active = dims.resolve(["faithfulness", "correctness"])
    g = grade_run(_case(), _run(), judge=SuiteJudgeSpec(id="j", model="m"),
                  active_dims=active, completion=_judge({"faithfulness": 0.9}))
    assert [s.dimension for s in g.dimensions] == ["faithfulness"]
    assert g.skipped_dimensions == ["correctness"]


def test_有参考答案时_correctness_正常判():
    active = dims.resolve(["correctness"])
    g = grade_run(_case(reference="华东 1.2 亿"), _run(),
                  judge=SuiteJudgeSpec(id="j", model="m"), active_dims=active,
                  completion=_judge({"correctness": 1.0}))
    assert g.dimensions[0].score == 1.0
    assert g.skipped_dimensions == []


# --- judge 返回的维度必须与请求一致 ---

def test_judge_漏判一个维度会报错():
    active = dims.resolve(["faithfulness", "completeness"])

    def fake(**kw):
        return json.dumps({"expectations": [], "dimensions": [
            {"dimension": "faithfulness", "score": 0.9, "evidence": "e"}]})

    with pytest.raises(ValueError, match="维度与请求不一致"):
        grade_run(_case(), _run(), judge=SuiteJudgeSpec(id="j", model="m"),
                  active_dims=active, completion=fake)


def test_judge_自己加一个维度会报错():
    def fake(**kw):
        return json.dumps({"expectations": [], "dimensions": [
            {"dimension": "faithfulness", "score": 0.9, "evidence": "e"},
            {"dimension": "自创维度", "score": 1.0, "evidence": "e"}]})

    with pytest.raises(ValueError, match="维度与请求不一致"):
        grade_run(_case(), _run(), judge=SuiteJudgeSpec(id="j", model="m"),
                  active_dims=dims.resolve(["faithfulness"]), completion=fake)


def test_分数超出_0_1_被拒():
    with pytest.raises(ValidationError):
        dims.DimensionScore(dimension="faithfulness", score=1.5, evidence="e")


def test_维度分必须带证据():
    with pytest.raises(ValidationError):
        dims.DimensionScore(dimension="faithfulness", score=0.9, evidence="")


def test_验证器本身认同序不认集合():
    judged = [dims.DimensionScore(dimension="completeness", score=1.0, evidence="e"),
              dims.DimensionScore(dimension="faithfulness", score=1.0, evidence="e")]
    with pytest.raises(ValueError):
        validate_dimensions(judged, dims.resolve(["faithfulness", "completeness"]),
                            case_id="x")


# --- prompt 组装 ---

def test_prompt_里带上了_rubric_锚点():
    prompt = build_grading_prompt(_case(), _run(), dims.resolve(["faithfulness"]))
    assert "faithfulness" in prompt
    assert "1.0 = " in prompt and "0.0 = " in prompt


def test_参考答案只在有的时候才进_prompt():
    assert "参考答案" not in build_grading_prompt(_case(), _run(), [])
    assert "参考答案" in build_grading_prompt(_case(reference="标准答案"), _run(), [])


def test_没有断言时明确要求返回空数组():
    """不说清楚，judge 会自己编几条断言出来判。"""
    prompt = build_grading_prompt(_case(), _run(), dims.resolve(["faithfulness"]))
    assert "本题没有断言" in prompt


# --- 整目录：维度对所有题生效，不需要逐题写东西 ---

def _write_run_dir(tmp_path, runs, cases):
    (tmp_path / "runs.jsonl").write_text(
        "\n".join(r.model_dump_json() for r in runs) + "\n", encoding="utf-8")
    ds = tmp_path / "cases.jsonl"
    ds.write_text("\n".join(c.model_dump_json() for c in cases) + "\n", encoding="utf-8")
    (tmp_path / "config.snapshot.yaml").write_text(
        yaml.safe_dump({"suite": {"dataset": str(ds)}, "config_hash": "sha256:abc"}),
        encoding="utf-8")
    return tmp_path


def test_一道断言都没写也能跑维度(tmp_path):
    """这是维度相对 assertion 的最大好处：通用尺子，不用逐题写。"""
    d = _write_run_dir(tmp_path, [_run(), _run(repeat_index=1)], [_case()])
    judge = SuiteJudgeSpec(id="glm5", model="m", dimensions=["faithfulness", "relevancy"])
    report = grade_run_dir(d, judge=judge,
                           completion=_judge({"faithfulness": 0.8, "relevancy": 1.0}))
    assert report.total == 0                      # 没有断言
    assert report.pass_rate is None               # N/A，不是 0
    assert report.dimension_means == {"faithfulness": 0.8, "relevancy": 1.0}


def test_维度均值只对真的判了的运行取平均(tmp_path):
    """3 题里只有 1 题有参考答案时，correctness 的分母是 1 不是 3。"""
    cases = [_case(), _case(id="dp-pos-02", prompt="第二题", reference="标准答案")]
    runs = [_run(), _run(case_id="dp-pos-02")]
    d = _write_run_dir(tmp_path, runs, cases)
    judge = SuiteJudgeSpec(id="glm5", model="m", dimensions=["faithfulness", "correctness"])
    report = grade_run_dir(d, judge=judge,
                           completion=_judge({"faithfulness": 0.6, "correctness": 1.0}))
    # faithfulness 两题都判了；correctness 只有第二题够条件
    assert report.dimension_means == {"correctness": 1.0, "faithfulness": 0.6}
    assert report.graded[0].skipped_dimensions == ["correctness"]
    assert report.graded[1].skipped_dimensions == []


def test_rubric_版本进判定产物(tmp_path):
    d = _write_run_dir(tmp_path, [_run()], [_case()])
    judge = SuiteJudgeSpec(id="glm5", model="m", dimensions=["faithfulness"])
    report = grade_run_dir(d, judge=judge, completion=_judge({"faithfulness": 1.0}))
    assert report.judge.dimensions == {"faithfulness": "v1"}
