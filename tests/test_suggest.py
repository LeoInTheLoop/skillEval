"""P3 最薄切片：失败证据来自原始 RunResult，建议必须可追溯且真正聚类。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest
import yaml

from contracts import RunResult
from workflows.suggest import (
    BudgetLimits,
    BudgetUsage,
    SuggestionReport,
    cumulative_usage,
    collect_routing_failures,
    generate_suggestions,
    load_scores,
    resolve_iteration_policy,
    resolve_skill_file,
    resolve_source_model,
    stop_decision,
)


def _run_dir(tmp_path):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "none-rej-01",
                        "prompt": "分析合同风险",
                        "expected_skills": [],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "id": "none-rej-02",
                        "prompt": "分析审计结论",
                        "expected_skills": [],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "id": "pdf-pos-01",
                        "prompt": "合并 PDF",
                        "expected_skills": ["pdf"],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.snapshot.yaml").write_text(
        yaml.safe_dump(
            {
                "suite": {
                    "dataset": str(dataset),
                    "skills": {"versions": {"pdf": "v2"}, "include": ["pdf", "docx"]},
                },
                "resolved_model": {
                    "model": "openai/glm-5.1",
                    "api_base_env": "DASHSCOPE_BASE_URL",
                    "api_key_env": "DASHSCOPE_API_KEY",
                    "params": {"temperature": 0},
                },
            }
        ),
        encoding="utf-8",
    )
    inputs = run_dir / "inputs" / "skills" / "pdf"
    inputs.mkdir(parents=True)
    (inputs / "SKILL.md").write_text("---\nname: pdf\n---\n正文", encoding="utf-8")
    rows = [
        RunResult(
            case_id="none-rej-01",
            repeat_index=0,
            model="m",
            selected_skills=["pdf"],
            raw_output='{"reasoning":"合同通常是 PDF 格式"}',
        ),
        RunResult(
            case_id="none-rej-02",
            repeat_index=0,
            model="m",
            selected_skills=["pdf"],
            raw_output='{"reasoning":"审计报告通常以 PDF 交付"}',
        ),
        RunResult(
            case_id="pdf-pos-01",
            repeat_index=0,
            model="m",
            selected_skills=["pdf"],
            raw_output='{"reasoning":"需要合并 PDF"}',
        ),
        RunResult(
            case_id="pdf-pos-01",
            repeat_index=1,
            model="m",
            status="failed",
            error="upstream down",
            error_kind="network",
        ),
    ]
    (run_dir / "runs.jsonl").write_text(
        "\n".join(row.model_dump_json() for row in rows) + "\n",
        encoding="utf-8",
    )
    return run_dir


def _suggestion_response(quote="通常是 PDF 格式"):
    return json.dumps(
        {
            "suggestions": [
                {
                    "pattern": "按文件格式联想，而不是按任务类型激活",
                    "case_ids": ["none-rej-01", "none-rej-02"],
                    "metric": "exact_set_match",
                    "evidence": [
                        {"case_id": "none-rej-01", "quote": quote},
                        {"case_id": "none-rej-02", "quote": "通常以 PDF 交付"},
                    ],
                    "change": (
                        "修改 frontmatter.description：仅在 PDF 格式操作时激活；"
                        "在 exclusions 增加纯内容分析与风险判断。"
                    ),
                }
            ]
        },
        ensure_ascii=False,
    )


def test_collect只收任务失败且保留模型原文(tmp_path):
    failures = collect_routing_failures(_run_dir(tmp_path))

    assert [failure.case_id for failure in failures] == [
        "none-rej-01",
        "none-rej-02",
    ]
    assert failures[0].raw_output == '{"reasoning":"合同通常是 PDF 格式"}'
    assert all(failure.metric == "exact_set_match" for failure in failures)


def test_generate按共同模式聚类并逐字验证证据(tmp_path):
    failures = collect_routing_failures(_run_dir(tmp_path))
    suggestions, prompt = generate_suggestions(
        target_skill="pdf",
        skill_text="---\nname: pdf\n---\n",
        failures=failures,
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={"temperature": 0},
        completion=lambda **_: _suggestion_response(),
    )

    assert len(suggestions) == 1
    assert suggestions[0].case_ids == ["none-rej-01", "none-rej-02"]
    assert "失败 trajectory" in prompt
    assert "按任务类型" in suggestions[0].pattern
    assert "frontmatter.description" in suggestions[0].change


def test_generate拒绝模型编造的证据_quote(tmp_path):
    failures = collect_routing_failures(_run_dir(tmp_path))

    with pytest.raises(ValueError, match="不是 none-rej-01 的原文"):
        generate_suggestions(
            target_skill="pdf",
            skill_text="---\nname: pdf\n---\n",
            failures=failures,
            model="openai/test",
            api_base_env="TEST_BASE",
            api_key_env="TEST_KEY",
            params={"temperature": 0},
            completion=lambda **_: _suggestion_response("模型编造的句子"),
        )


def test_generate拒绝逐题一条没有聚类(tmp_path):
    failures = collect_routing_failures(_run_dir(tmp_path))
    payload = {
        "suggestions": [
            {
                "pattern": "p1",
                "case_ids": ["none-rej-01"],
                "metric": "exact_set_match",
                "evidence": [{"case_id": "none-rej-01", "quote": "PDF 格式"}],
                "change": "改 description",
            },
            {
                "pattern": "p2",
                "case_ids": ["none-rej-02"],
                "metric": "exact_set_match",
                "evidence": [{"case_id": "none-rej-02", "quote": "PDF 交付"}],
                "change": "改 exclusions",
            },
        ]
    }

    with pytest.raises(ValueError, match="建议未聚类"):
        generate_suggestions(
            target_skill="pdf",
            skill_text="---\nname: pdf\n---\n",
            failures=failures,
            model="openai/test",
            api_base_env="TEST_BASE",
            api_key_env="TEST_KEY",
            params={"temperature": 0},
            completion=lambda **_: json.dumps(payload, ensure_ascii=False),
        )


def test_resolve_source_model默认继承source_run配置(tmp_path):
    run_dir = _run_dir(tmp_path)
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    args = argparse.Namespace(model=None, api_base_env=None, api_key_env=None)

    model, base_env, key_env, params = resolve_source_model(snapshot, args)

    assert model == "openai/glm-5.1"
    assert base_env == "DASHSCOPE_BASE_URL"
    assert key_env == "DASHSCOPE_API_KEY"
    assert params == {"temperature": 0}


def test_resolve_skill_file可从run快照自动推断(tmp_path):
    run_dir = _run_dir(tmp_path)
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))

    skill_file = resolve_skill_file(run_dir, snapshot, skill_file=None, skill_id=None)

    assert skill_file == (run_dir / "inputs" / "skills" / "pdf" / "SKILL.md").resolve()


# --- full eval：选对 skill 也可能失败，失败形态和 routing 完全不同 ---


def _full_run_dir(tmp_path, *, with_grading=True):
    """一个 full 模式的 run 归档：一题缺产物，一题产物落了但断言没过。"""
    dataset = tmp_path / "full_cases.jsonl"
    dataset.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False)
            for row in [
                {
                    "id": "brief-pos-01",
                    "prompt": "整理成纪要",
                    "expected_skills": ["brief"],
                    "expect_artifacts": ["out/brief.md"],
                    "expect_tools": ["write"],
                },
                {
                    "id": "brief-pos-02",
                    "prompt": "清一遍转写稿",
                    "expected_skills": ["brief"],
                    "expect_assertions": ["把『进调』当转写错误处理了"],
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "full_run"
    run_dir.mkdir()
    (run_dir / "config.snapshot.yaml").write_text(
        yaml.safe_dump(
            {
                "suite": {
                    "dataset": str(dataset),
                    "skills": {"dir": str(tmp_path / "subjects"), "mode": "full",
                               "include": ["brief"], "cfg": "v1"},
                    "scoring": {"judge": {"id": "qwenplus"}},
                    "pipeline": {"iteration": 1},
                },
                "resolved_model": {"model": "openai/glm-5.1"},
            }
        ),
        encoding="utf-8",
    )
    inputs = run_dir / "inputs" / "skills" / "brief"
    inputs.mkdir(parents=True)
    (inputs / "SKILL.md").write_text(
        "---\nname: brief\ndescription: 会议纪要\n---\n把会议记录整理成纪要。", encoding="utf-8")
    rows = [
        RunResult(case_id="brief-pos-01", repeat_index=0, model="m",
                  loaded_skills=["brief"], final_answer="已经帮你整理好了",
                  tool_calls=[]),
        RunResult(
            case_id="brief-pos-02", repeat_index=0, model="m",
            loaded_skills=["brief"], final_answer="清理完成",
            tool_calls=[{"name": "write", "count": 1}],
            artifacts=[{"path": "out/clean.md", "sha256": "sha256:ab", "size_bytes": 40,
                        "mime_type": "text/markdown",
                        "text_excerpt": "会议要点：进调报告的数据由刘总监提供"}],
        ),
    ]
    (run_dir / "runs.jsonl").write_text(
        "\n".join(row.model_dump_json() for row in rows) + "\n", encoding="utf-8")
    if with_grading:
        (run_dir / "grading.qwenplus.json").write_text(
            json.dumps(
                {
                    "graded": [
                        {
                            "case_id": "brief-pos-02",
                            "repeat_index": 0,
                            "expectations": [
                                {"text": "把『进调』当转写错误处理了", "passed": False,
                                 "evidence": "产物里『进调报告』原样保留，没有标注待核"}
                            ],
                            "dimensions": [{"dimension": "faithfulness", "score": 0.4,
                                            "evidence": "术语未校对"}],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return run_dir


def test_full失败按缺产物与断言分别归因(tmp_path):
    from workflows.suggest import collect_failures

    run_dir = _full_run_dir(tmp_path)
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    failures = collect_failures(run_dir, snapshot)

    by_case = {failure.case_id: failure for failure in failures}
    assert set(by_case) == {"brief-pos-01", "brief-pos-02"}
    # 产物没落 + tool 没调 → 主因是任务没完成，不是"文风不好"
    assert by_case["brief-pos-01"].metric == "task_completion"
    assert "out/brief.md" in by_case["brief-pos-01"].failure_detail
    assert "write" in by_case["brief-pos-01"].failure_detail
    # 该做的都做了、只是内容不对 → 归到 judge 的语义断言
    assert by_case["brief-pos-02"].metric == "assertion"
    assert "进调" in by_case["brief-pos-02"].failure_detail
    assert "faithfulness" in by_case["brief-pos-02"].failure_detail
    # 产物原文要进证据，否则改进模型只能猜产物长什么样
    assert "刘总监" in by_case["brief-pos-02"].failure_detail


def test_full的证据quote可以引用failure_detail(tmp_path):
    """judge 证据和产物原文都在 failure_detail 里；只认 raw_output 会把真证据判成编造。"""
    from workflows.suggest import collect_failures

    run_dir = _full_run_dir(tmp_path)
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    failures = collect_failures(run_dir, snapshot)
    payload = {
        "suggestions": [
            {
                "pattern": "术语校对与产物落盘两条硬要求都没写死",
                "case_ids": ["brief-pos-01", "brief-pos-02"],
                "metric": "task_completion",
                "evidence": [
                    {"case_id": "brief-pos-01", "quote": "声明必须调用的 tool 没调"},
                    {"case_id": "brief-pos-02", "quote": "『进调报告』原样保留"},
                ],
                "change": "正文补一节：必须写出文件，且可疑术语要逐条标注待核。",
            }
        ]
    }

    suggestions, _ = generate_suggestions(
        target_skill="brief",
        skill_text="---\nname: brief\n---\n",
        failures=failures,
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={},
        completion=lambda **_: json.dumps(payload, ensure_ascii=False),
    )
    assert suggestions[0].metric == "task_completion"


def test_没跑grade时只按确定性断言判失败(tmp_path):
    """grade 是可选付费步骤；没跑它不该让「内容对不对」凭空变成通过或失败。"""
    from workflows.suggest import collect_failures

    run_dir = _full_run_dir(tmp_path, with_grading=False)
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    failures = collect_failures(run_dir, snapshot)

    assert [failure.case_id for failure in failures] == ["brief-pos-01"]


# --- --apply：把建议落成新版本目录 + 同题复验 suite ---


def _cluster(**overrides):
    from workflows.suggest import SuggestionCluster

    payload = {
        "pattern": "没写死必须落盘",
        "case_ids": ["brief-pos-01"],
        "metric": "task_completion",
        "evidence": [{"case_id": "brief-pos-01", "quote": "声明必须调用的 tool 没调"}],
        "change": "正文补一条：必须把结果写成文件。",
    }
    payload.update(overrides)
    return SuggestionCluster.model_validate(payload)


def test_apply拒绝改name(tmp_path):
    """name 就是 skill_id。改了它，复验测的就是另一个 skill，对照实验直接作废。"""
    from workflows.suggest import validate_applied_skill

    with pytest.raises(ValueError, match="name 被改了"):
        validate_applied_skill(
            "---\nname: brief-v2\ndescription: 会议纪要\n---\n新正文",
            "---\nname: brief\ndescription: 会议纪要\n---\n旧正文",
        )


def test_apply拒绝原样返回(tmp_path):
    """模型偷懒把原文抄回来时必须报错，否则会落出一个和 v1 逐字相同的 v2。"""
    from workflows.suggest import validate_applied_skill

    original = "---\nname: brief\ndescription: 会议纪要\n---\n正文"
    with pytest.raises(ValueError, match="等于什么都没改"):
        validate_applied_skill(original, original)


def test_apply剥掉围栏并保留frontmatter():
    from workflows.suggest import validate_applied_skill

    cleaned = validate_applied_skill(
        "```markdown\n---\nname: brief\ndescription: 会议纪要 v2\n---\n新正文\n```",
        "---\nname: brief\ndescription: 会议纪要\n---\n旧正文",
    )
    assert cleaned.startswith("---\nname: brief")
    assert "新正文" in cleaned and "```" not in cleaned


def test_写新版本目录会带上源版本的附件(tmp_path):
    """skill 常带 references/ 和 scripts/，只写 SKILL.md 会让新版本运行时缺文件。"""
    from workflows.suggest import next_version, write_new_version

    skill_dir = tmp_path / "brief"
    (skill_dir / "v1" / "references").mkdir(parents=True)
    (skill_dir / "v1" / "SKILL.md").write_text("---\nname: brief\n---\n旧", encoding="utf-8")
    (skill_dir / "v1" / "references" / "style.md").write_text("排版规范", encoding="utf-8")

    assert next_version(skill_dir) == "v2"
    target = write_new_version(
        skill_dir=skill_dir, source_version="v1", version="v2",
        skill_text="---\nname: brief\n---\n新", provenance="来源说明",
    )

    assert (target / "SKILL.md").read_text(encoding="utf-8").endswith("新")
    assert (target / "references" / "style.md").read_text(encoding="utf-8") == "排版规范"
    assert (target / "PROVENANCE.md").exists()
    # 源版本一个字都不许动
    assert (skill_dir / "v1" / "SKILL.md").read_text(encoding="utf-8").endswith("旧")


def test_拒绝覆盖已存在的版本目录(tmp_path):
    from workflows.suggest import write_new_version

    skill_dir = tmp_path / "brief"
    (skill_dir / "v2").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        write_new_version(skill_dir=skill_dir, source_version="v1", version="v2",
                          skill_text="---\nname: brief\n---\n新", provenance="x")


def test_复验suite只换skill版本(tmp_path):
    """唯一变量：题集、模型、runtime、judge 全部照抄，只有被测版本变了。"""
    from workflows.suggest import build_reeval_suite

    run_dir = _full_run_dir(tmp_path)
    snapshot = yaml.safe_load((run_dir / "config.snapshot.yaml").read_text(encoding="utf-8"))
    suite = build_reeval_suite(snapshot=snapshot, skill_id="brief", version="v2")

    assert suite["skills"]["versions"] == {"brief": "v2"}
    assert suite["skills"]["target"] == ["brief"]
    assert suite["dataset"] == snapshot["suite"]["dataset"]
    assert suite["scoring"] == snapshot["suite"]["scoring"]
    assert suite["skills"]["cfg"] == "v1-v2"
    assert suite["pipeline"]["iteration"] == 2
    # 生成 suite 不许反过来改到调用方手里的快照
    assert "versions" not in snapshot["suite"]["skills"]


def test_apply_suggestions走非json模式(tmp_path):
    """建议阶段要 JSON，改写阶段要 Markdown 全文；串了会得到一个被 JSON 包起来的 skill。"""
    from workflows.suggest import apply_suggestions

    seen = {}

    def fake_completion(**kwargs):
        seen.update(kwargs)
        return "---\nname: brief\ndescription: 会议纪要 v2\n---\n必须把结果写成文件。"

    result = apply_suggestions(
        skill_text="---\nname: brief\ndescription: 会议纪要\n---\n旧正文",
        suggestions=[_cluster()],
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={},
        completion=fake_completion,
    )

    assert seen["json_mode"] is False
    assert "必须把结果写成文件" in seen["prompt"]      # 建议原文进了改写 prompt
    assert result.startswith("---\nname: brief")


def test_引用允许剥掉markdown标记但不许改字(tmp_path):
    """模型复制证据时爱把 `反引号` 和 **星号** 剥掉 —— 那不是编造，不该整批拒收。"""
    from workflows.suggest import validate_suggestions, FailureEvidence, SuggestionCluster

    failures = [FailureEvidence(
        case_id="brief-pos-01", repeat_index=0, metric="assertion",
        expected_skills=["brief"], selected_skills=["brief"], raw_output="整理好了",
        failure_detail="judge 证据：产物 `out/x.md` 中**未出现**赵磊此人名",
    )]

    ok = [SuggestionCluster(
        pattern="人名幻觉", case_ids=["brief-pos-01"], metric="assertion",
        evidence=[{"case_id": "brief-pos-01", "quote": "产物 out/x.md 中未出现赵磊此人名"}],
        change="正文补一条：人名必须来自输入。",
    )]
    assert validate_suggestions(ok, failures) == ok

    fabricated = [SuggestionCluster(
        pattern="人名幻觉", case_ids=["brief-pos-01"], metric="assertion",
        evidence=[{"case_id": "brief-pos-01", "quote": "产物中未出现张三此人名"}],
        change="正文补一条：人名必须来自输入。",
    )]
    with pytest.raises(ValueError, match="不是 brief-pos-01 的原文"):
        validate_suggestions(fabricated, failures)


# --- 迭代停止条件：gate / 累计预算 / 最大轮数 ---


def _usage_run(path: Path, *, tokens: int | None, duration_ms: int | None):
    path.mkdir()
    row = {
        "case_id": "c1",
        "repeat_index": 0,
        "model": "m",
        "status": "success",
        "usage": (
            {"input_tokens": tokens - 1, "output_tokens": 1}
            if tokens is not None else {}
        ),
        "duration_ms": duration_ms,
    }
    (path / "runs.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


def test_stop_decision记录全部触发并按优先级给主原因():
    primary, triggered = stop_decision(
        gate_pass=True,
        iteration=3,
        max_iterations=3,
        limits=BudgetLimits(max_total_tokens=100),
        usage=BudgetUsage(total_tokens=100),
    )

    assert primary == "gate_pass"
    assert triggered == ["gate_pass", "budget_exceeded", "max_iterations"]


def test_cumulative_usage跨轮去重累计(tmp_path):
    first = _usage_run(tmp_path / "r1", tokens=30, duration_ms=1500)
    second = _usage_run(tmp_path / "r2", tokens=70, duration_ms=2500)

    usage = cumulative_usage(
        [str(first), str(second)],
        BudgetLimits(max_total_tokens=100, max_total_seconds=10),
    )

    assert usage.total_tokens == 100
    assert usage.total_seconds == 4.0
    assert usage.runs == 2
    with pytest.raises(ValueError, match="重复 run"):
        cumulative_usage([str(first), str(first)], BudgetLimits())


def test_budget配置后usage缺失会fail_closed(tmp_path):
    run = _usage_run(tmp_path / "r1", tokens=None, duration_ms=None)

    with pytest.raises(ValueError, match="没有 token usage"):
        cumulative_usage([str(run)], BudgetLimits(max_total_tokens=1))
    with pytest.raises(ValueError, match="没有 duration_ms"):
        cumulative_usage([str(run)], BudgetLimits(max_total_seconds=1))


def test_previous_report继承预算且iteration必须连续(tmp_path):
    first = _usage_run(tmp_path / "r1", tokens=10, duration_ms=100)
    current = _usage_run(tmp_path / "r2", tokens=10, duration_ms=100)
    previous = SuggestionReport(
        version="v",
        source_run=str(first),
        target_skill="pdf",
        iteration=1,
        max_iterations=3,
        generated_at="2026-07-29",
        model=None,
        prompt_hash=None,
        failures=[],
        suggestions=[],
        run_lineage=[str(first.resolve())],
        budget_limits=BudgetLimits(max_total_tokens=100),
    )
    report_path = tmp_path / "previous.json"
    report_path.write_text(previous.model_dump_json(), encoding="utf-8")
    snapshot = {"suite": {"pipeline": {"iteration": 2}}}

    iteration, maximum, lineage, limits, _ = resolve_iteration_policy(
        run_dir=current,
        snapshot=snapshot,
        previous_report_path=str(report_path),
        iteration_override=None,
        max_iterations_override=None,
        max_total_tokens=None,
        max_total_seconds=None,
    )

    assert (iteration, maximum) == (2, 3)
    assert lineage == [str(first.resolve()), str(current.resolve())]
    assert limits.max_total_tokens == 100

    with pytest.raises(ValueError, match="严格递增"):
        resolve_iteration_policy(
            run_dir=current,
            snapshot={"suite": {"pipeline": {"iteration": 3}}},
            previous_report_path=str(report_path),
            iteration_override=None,
            max_iterations_override=None,
            max_total_tokens=None,
            max_total_seconds=None,
        )


def test_load_scores缺失时提示先评分(tmp_path):
    with pytest.raises(FileNotFoundError, match="先运行对应"):
        load_scores(tmp_path)


def test_main_gate_pass时不调用模型且apply不写版本(tmp_path, monkeypatch):
    from workflows import suggest

    run_dir = _run_dir(tmp_path)
    (run_dir / "scores.json").write_text(json.dumps({"gate_pass": True}), encoding="utf-8")
    monkeypatch.setattr(
        suggest,
        "call_litellm",
        lambda **_: pytest.fail("gate PASS 后不应调用模型"),
    )
    monkeypatch.setattr(sys, "argv", [
        "workflows.suggest", "--run-dir", str(run_dir), "--apply",
    ])

    suggest.main()

    report_path = run_dir / "improvements" / "round-01" / "suggestions.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["stop_reason"] == "gate_pass"
    assert report["apply_status"] == "not_requested"
    assert not (tmp_path / "subjects").exists()


def test_main未确认外发只显示manifest且零写入(tmp_path, monkeypatch, capsys):
    from workflows import suggest

    run_dir = _run_dir(tmp_path)
    (run_dir / "scores.json").write_text(json.dumps({"gate_pass": False}), encoding="utf-8")
    monkeypatch.setattr(
        suggest,
        "call_litellm",
        lambda **_: pytest.fail("未确认外发时不应调用模型"),
    )
    monkeypatch.setattr(sys, "argv", ["workflows.suggest", "--run-dir", str(run_dir)])

    suggest.main()

    output = capsys.readouterr().out
    assert "外发清单" in output
    assert 'skill metadata: {"name": "pdf"}' in output
    assert "failure trajectories: 2 runs / 2 cases" in output
    assert "合同通常是 PDF 格式" in output
    assert "--confirm-egress" in output
    assert not (run_dir / "improvements").exists()


def test_pdf_v1能力基准_mock建议按任务而非文件格式激活():
    """真实外发需单独授权；这里固定 PASS 语义与三题聚类契约。"""
    from workflows.suggest import FailureEvidence

    reasons = {
        "none-rej-01": "用户需要分析PDF合同中的内容，符合pdf skill的处理范围。",
        "none-rej-02": "用户需要读取PDF审计报告判断现金流，符合pdf skill的处理范围。",
        "none-rej-03": "用户要求总结PDF论文观点，需要提取PDF文本内容。",
    }
    failures = [
        FailureEvidence(
            case_id=case_id,
            repeat_index=repeat,
            expected_skills=[],
            selected_skills=["pdf"],
            raw_output=reason,
        )
        for case_id, reason in reasons.items()
        for repeat in range(3)
    ]
    response = {
        "suggestions": [{
            "pattern": "模型按 PDF 文件格式联想激活，而没有判断用户要求的任务类型",
            "case_ids": sorted(reasons),
            "metric": "exact_set_match",
            "evidence": [
                {"case_id": case_id, "quote": reason}
                for case_id, reason in reasons.items()
            ],
            "change": (
                "改 frontmatter.description：仅在提取、转换、合并、拆分、表单等 PDF "
                "格式操作时激活；exclusions 明确排除纯内容分析、风险判断、观点总结。"
            ),
        }]
    }

    suggestions, _ = generate_suggestions(
        target_skill="pdf",
        skill_text="---\nname: pdf\ndescription: Use for PDF files.\n---\n正文",
        failures=failures,
        model="openai/test",
        api_base_env="TEST_BASE",
        api_key_env="TEST_KEY",
        params={},
        completion=lambda **_: json.dumps(response, ensure_ascii=False),
    )

    assert suggestions[0].case_ids == sorted(reasons)
    assert "任务类型" in suggestions[0].pattern
    for phrase in ("提取", "转换", "合并", "拆分", "表单", "纯内容分析", "风险判断", "观点总结"):
        assert phrase in suggestions[0].change
