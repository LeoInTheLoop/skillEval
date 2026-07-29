"""2026-07-28 用户轨迹评测暴露的问题，逐条落成回归测试。

每条测试对应 HANDOFF.md「F 编号」里的一项。它们都是**用真实使用轨迹**发现的，
不是想象出来的边界情况 —— 所以宁可各写一条，也不合并。
"""
from __future__ import annotations

import stat

import pytest

from contracts import InvocationRequest, RoutingCase, SkillMeta, load_skills
from environments.filesystem import materialized_files, stage_input_files


def _skill_dir(root, name, body="正文", version="v1", **frontmatter):
    directory = root / name / version
    directory.mkdir(parents=True, exist_ok=True)
    extra = "".join(f"{k}: {v}\n" for k, v in frontmatter.items())
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} 的描述\n{extra}---\n{body}",
        encoding="utf-8",
    )
    return directory


# --- F11a：纯文档字段不进 config_hash ------------------------------------


def test_只改_suite_的人读说明不该让两次结果变得不可比():
    from contracts import load_suite
    from workflows.run_routing import config_hash

    suite = load_suite("evals/suites/example_routing.yaml").canonical_dict()
    baseline = config_hash(suite, {"id": "m"}, False, {}, [], "sha256:x", {})

    for field, value in (
        ("description", "换一句给人看的说明"),
        ("suite_id", "renamed_suite"),
        ("suite_version", "9.9"),
    ):
        assert config_hash({**suite, field: value}, {"id": "m"}, False, {}, [],
                           "sha256:x", {}) == baseline, f"{field} 不该进 hash"

    # 真正会改变结果的东西仍然必须改变 hash
    assert config_hash({**suite, "repeats": suite["repeats"] + 1}, {"id": "m"},
                       False, {}, [], "sha256:x", {}) != baseline


# --- F11e：被作者停用的 skill 必须告警 -----------------------------------


def test_frontmatter_里_disable_true_会被读出来(tmp_path):
    _skill_dir(tmp_path, "alive")
    _skill_dir(tmp_path, "stopped", **{"disable": "true"})
    _skill_dir(tmp_path, "legacy", **{"enabled": "false"})
    # YAML 1.1 会把裸的 on/no/yes 解析成布尔值；这时退回目录名，而不是抛 pydantic 错
    _skill_dir(tmp_path, "on")

    by_id = {s.skill_id: s for s in load_skills(tmp_path)}

    assert by_id["alive"].disabled is False
    assert by_id["stopped"].disabled is True
    assert by_id["legacy"].disabled is True
    assert by_id["on"].name == "on"
    # 不能泄漏进给模型看的元数据
    assert "disabled" not in by_id["stopped"].model_dump()


def test_停用的_skill_产生告警而不是被静默评测(tmp_path):
    from pipeline.plan import disabled_skill_warnings

    _skill_dir(tmp_path, "stopped", **{"disable": "true"})
    skills = load_skills(tmp_path)

    warnings = disabled_skill_warnings(skills)
    assert len(warnings) == 1
    assert "stopped" in warnings[0]
    assert "生产 catalog" in warnings[0]
    assert disabled_skill_warnings([]) == []


def test_gate缺少可评题会在花钱前给出修复建议():
    from workflows.diagnostics import gate_coverage_warnings

    warnings = gate_coverage_warnings(
        [RoutingCase(id="alpha-pos-01", prompt="p", expected_skills=["alpha"], severity="high")],
        {"critical_miss": "<= 0", "no_skill_rejection": ">= 0.9"},
    )

    assert len(warnings) == 2
    assert "severity: critical" in warnings[0]
    assert "no_skill_rejection" in warnings[1]


# --- F3：未钉版本取 v1，新增版本不能污染历史基线 ------------------------


def test_有多个版本而suite未钉时仍固定选择_v1(tmp_path):
    base = tmp_path / "skills"
    for name in ("alpha", "beta"):
        _skill_dir(base, name)
    _skill_dir(base, "alpha", body="alpha 的 V2 正文", version="v2")

    skills = load_skills(base)
    assert [(skill.skill_id, skill.version) for skill in skills] == [
        ("alpha", "v1"), ("beta", "v1")
    ]


def test_suite_契约拒绝非法或无效的版本钉选():
    from pydantic import ValidationError

    from contracts.suite import SuiteSkillSpec

    with pytest.raises(ValidationError, match="版本目录名"):
        SuiteSkillSpec.model_validate({
            "dir": "skills", "target": ["alpha"], "cfg": "v1",
            "versions": {"alpha": "latest"},
        })
    with pytest.raises(ValidationError, match="exclude"):
        SuiteSkillSpec.model_validate({
            "dir": "skills", "target": ["alpha"], "cfg": "v1",
            "versions": {"alpha": "v1"}, "exclude": ["alpha"],
        })


# --- F12：include/exclude 选错 skill 时，题集会变成无解题 ------------------


def test_gold_指向不在catalog里的skill时在花钱前告警(tmp_path):
    from pipeline.plan import unreachable_gold_warnings

    base = tmp_path / "subjects"
    for name in ("alpha", "beta"):
        _skill_dir(base, name)
    cases = [
        RoutingCase(id="alpha-pos-01", prompt="p", expected_skills=["alpha"]),
        RoutingCase(id="gamma-pos-01", prompt="q", expected_skills=["gamma"]),
        RoutingCase(id="none-neg-01", prompt="r", expected_skills=[]),
    ]

    # include 漏掉了 gamma：那道题没有正确答案可选，指标会掉而看不出原因
    warnings = unreachable_gold_warnings(cases, load_skills(base), "routing_only")
    assert len(warnings) == 1
    assert "1/3 道题" in warnings[0] and "gamma" in warnings[0]
    assert "alpha" not in warnings[0]      # 可达的与 No-Skill 题不该被点名

    # mode=none 的 catalog 本来就是空的，不必再说一遍
    assert unreachable_gold_warnings(cases, [], "none") == []


def test_gold_全部可达时不产生噪音(tmp_path):
    from pipeline.plan import unreachable_gold_warnings

    base = tmp_path / "subjects"
    _skill_dir(base, "alpha")
    cases = [RoutingCase(id="alpha-pos-01", prompt="p", expected_skills=["alpha"])]

    assert unreachable_gold_warnings(cases, load_skills(base), "routing_only") == []


# --- F5：case 输入文件真的会被只读挂载进 workspace ------------------------


def _request(**kwargs):
    return InvocationRequest(
        request_id="r1", case_id="c1", repeat_index=0, prompt="p", **kwargs
    )


def test_声明的输入文件被复制进workspace并只读(tmp_path):
    fixture = tmp_path / "纪要.txt"
    fixture.write_text("发言人1：这是文字稿", encoding="utf-8")

    request = _request(input_files=[str(fixture)])
    with materialized_files(request) as (workspace, _):
        staged = workspace / "纪要.txt"
        assert staged.read_text(encoding="utf-8") == "发言人1：这是文字稿"
        assert not stat.S_IMODE(staged.stat().st_mode) & stat.S_IWUSR
    # 退出上下文后临时目录整体清理掉，只读文件不能把 rmtree 卡住
    assert not workspace.exists()


def test_输入文件不存在时立刻报错而不是跑到一半才炸(tmp_path):
    request = _request(input_files=[str(tmp_path / "缺失.docx")])
    with pytest.raises(FileNotFoundError, match="不存在"):
        stage_input_files(request, tmp_path)


def test_plan_在跑之前就拦下缺失的素材(tmp_path, monkeypatch):
    from workflows import run_routing

    monkeypatch.setattr(run_routing, "ROOT", tmp_path)
    (tmp_path / "evals" / "fixtures").mkdir(parents=True)
    (tmp_path / "evals" / "fixtures" / "有的.txt").write_text("x", encoding="utf-8")

    cases = [
        RoutingCase(id="a-pos-01", prompt="p", files=["evals/fixtures/有的.txt"]),
        RoutingCase(id="a-pos-02", prompt="p", files=["evals/fixtures/没有的.txt"]),
    ]
    missing = run_routing.missing_case_files(cases)
    assert missing == ["a-pos-02: evals/fixtures/没有的.txt"]
    assert run_routing.resolve_case_files(cases[0])[0].endswith("有的.txt")


# --- F11d：severity=critical 真的被消费 ----------------------------------


def test_critical_miss_端到端进入scores和gate(tmp_path, monkeypatch, capsys):
    """severity=critical 以前只被契约校验、从没被评分读过 —— 走一遍真实评分链路。"""
    import json
    import sys

    import yaml

    from workflows import score_routing

    dataset = tmp_path / "routing_x_v1.0.jsonl"
    dataset.write_text("\n".join([
        json.dumps({"id": "alpha-pos-01", "prompt": "p", "expected_skills": ["alpha"],
                    "severity": "critical"}, ensure_ascii=False),
        json.dumps({"id": "alpha-pos-02", "prompt": "q", "expected_skills": ["alpha"],
                    "severity": "critical"}, ensure_ascii=False),
        json.dumps({"id": "none-rej-01", "prompt": "r", "expected_skills": [],
                    "severity": "medium"}, ensure_ascii=False),
    ]) + "\n", encoding="utf-8")

    run = tmp_path / "run"
    run.mkdir()
    (run / "config.snapshot.yaml").write_text(yaml.safe_dump({
        "suite": {
            "suite_id": "t", "suite_version": "1.0", "dataset": str(dataset),
            "skills": {"mode": "routing_only"},
            "scoring": {"metrics": ["critical_miss"], "gate": {"critical_miss": "<= 0"}},
        },
        "config_hash": "sha256:test",
    }, allow_unicode=True), encoding="utf-8")
    (run / "runs.jsonl").write_text("\n".join(
        json.dumps(r) for r in [
            {"case_id": "alpha-pos-01", "repeat_index": 0, "model": "m",
             "selected_skills": ["alpha"], "ok": True},
            {"case_id": "alpha-pos-02", "repeat_index": 0, "model": "m",
             "selected_skills": [], "ok": True},          # ← critical 题判错
            {"case_id": "none-rej-01", "repeat_index": 0, "model": "m",
             "selected_skills": [], "ok": True},
        ]
    ) + "\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["score_routing", "--dir", str(run)])
    score_routing.main()

    out = capsys.readouterr().out
    assert "Critical skill miss" in out
    scores = json.loads((run / "scores.json").read_text(encoding="utf-8"))
    assert scores["scores"]["critical_miss"] == pytest.approx(0.5)
    gate = {row["metric"]: row["pass"] for row in scores["gate"]}
    assert gate["critical_miss"] is False        # 门槛 = 0，错一道就该拦下
    assert "GATE FAIL" in out


def test_全部运行失败仍写出错误汇总和修复指引(tmp_path, monkeypatch, capsys):
    """不能只说『没有可评 run』，否则用户不知道该修 DNS 还是 skill。"""
    import json
    import sys

    import yaml

    from workflows import score_routing

    dataset = tmp_path / "routing_x_v1.0.jsonl"
    dataset.write_text(
        json.dumps({"id": "alpha-pos-01", "prompt": "p", "expected_skills": ["alpha"]}) + "\n",
        encoding="utf-8",
    )
    run = tmp_path / "run"
    run.mkdir()
    (run / "config.snapshot.yaml").write_text(yaml.safe_dump({
        "suite": {
            "suite_id": "t", "suite_version": "1.0", "dataset": str(dataset),
            "skills": {"mode": "routing_only"},
            "scoring": {"metrics": ["exact_set_match"], "gate": {"exact_set_match": ">= 0.8"}},
        },
        "config_hash": "sha256:test",
    }, allow_unicode=True), encoding="utf-8")
    (run / "runs.jsonl").write_text("\n".join(json.dumps(row) for row in [
        {"case_id": "alpha-pos-01", "repeat_index": 0, "model": "m", "ok": False,
         "error_kind": "network", "error_subkind": "network_dns", "error": "DNS failed"},
        {"case_id": "alpha-pos-01", "repeat_index": 1, "model": "m", "ok": False,
         "error_kind": "network", "error_subkind": "network_dns", "error": "DNS failed"},
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["score_routing", "--dir", str(run)])
    score_routing.main()

    out = capsys.readouterr().out
    assert "network=2" in out
    assert "network_dns=2" in out
    assert "network/DNS" in out
    scores = json.loads((run / "scores.json").read_text(encoding="utf-8"))
    assert scores["n_runs"] == 2
    assert scores["n_evaluable_runs"] == 0
    assert scores["failure_summary"]["by_error_subkind"] == {"network_dns": 2}
    assert scores["gate_pass"] is None
    assert "no evaluable runs" in (run / "report.html").read_text(encoding="utf-8")


def test_mock只报告管道成功不产出skill质量结论(tmp_path, monkeypatch, capsys):
    import json
    import sys

    import yaml

    from workflows import score_routing

    dataset = tmp_path / "routing_x_v1.0.jsonl"
    dataset.write_text("\n".join([
        json.dumps({"id": "alpha-pos-01", "prompt": "p", "expected_skills": ["alpha"]}),
        json.dumps({"id": "none-rej-01", "prompt": "q", "expected_skills": []}),
    ]) + "\n", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    (run / "config.snapshot.yaml").write_text(yaml.safe_dump({
        "suite": {
            "suite_id": "t", "suite_version": "1.0", "dataset": str(dataset),
            "skills": {"mode": "routing_only"},
            "scoring": {
                "metrics": ["exact_set_match"],
                "gate": {"exact_set_match": ">= 0.8"},
            },
        },
        "config_hash": "sha256:test",
        "mock": True,
    }), encoding="utf-8")
    (run / "runs.jsonl").write_text("\n".join(json.dumps(row) for row in [
        {
            "case_id": "alpha-pos-01",
            "repeat_index": 0,
            "model": "mock",
            "selected_skills": ["alpha"],
            "ok": True,
        },
        {
            "case_id": "none-rej-01",
            "repeat_index": 0,
            "model": "mock",
            "selected_skills": [],
            "ok": True,
        },
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["score_routing", "--dir", str(run)])
    score_routing.main()

    out = capsys.readouterr().out
    scores = json.loads((run / "scores.json").read_text(encoding="utf-8"))
    assert scores["scores"]["exact_set_match"] == 1.0
    assert scores["run_mode"] == "synthetic_mock"
    assert scores["quality_verdict"] == "not_evaluated"
    assert scores["gate_enforced"] is False
    assert scores["gate_pass"] is None
    assert scores["gate"][0]["observed_pass"] is True
    assert scores["gate"][0]["pass"] is None
    assert "QUALITY VERDICT NOT EVALUATED" in out
    assert "SYNTHETIC MOCK" in (run / "report.html").read_text(encoding="utf-8")


# --- F4：生成器 tags 规范化 ----------------------------------------------


def test_tags统一成四个枚举():
    from workflows.gen_cases import normalize_tags

    cases = [
        RoutingCase(id="a-pos-01", prompt="p", expected_skills=["a"], tags=["月度简报"]),
        RoutingCase(id="none-rej-01", prompt="p", tags=[]),
        RoutingCase(id="a+b-multi-01", prompt="p", expected_skills=["a", "b"]),
    ]
    normalize_tags(cases)
    assert [case.tags for case in cases] == [["positive"], ["no-skill"], ["multi-skill"]]


# --- F8：openclaw 找不到时先自查再建议 -----------------------------------


def test_openclaw提示会先查nvm再让人重装(tmp_path, monkeypatch):
    from adapters.runtimes.openclaw import OpenClawRuntimeAdapter

    runtime = OpenClawRuntimeAdapter()
    monkeypatch.setattr("adapters.runtimes.openclaw.Path.home", lambda: tmp_path)
    assert "npm i -g openclaw" in runtime._install_hint()

    installed = tmp_path / ".nvm/versions/node/v24.18.0/bin"
    installed.mkdir(parents=True)
    (installed / "openclaw").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    (installed / "node").write_text("", encoding="utf-8")
    hint = runtime._install_hint()
    assert "但它其实装着" in hint
    assert "node_bin:" in hint
    assert "npm i -g openclaw" not in hint
