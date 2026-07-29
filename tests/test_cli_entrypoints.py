"""每个 `python -m` 入口的 main() 冒烟测试（AGENTS.md §29.26）。

**为什么单独有这一份**：仓库曾经 192 个测试全绿，而 `python -m workflows.gen_cases`
的 CLI 第一行就 `TypeError: generate_batch() got an unexpected keyword argument
'include_skill_ids'` —— 参数放错了函数，且已经进了 main。原因很直接：测试全都直接调
内部函数，**没有一条走 argparse → 参数装配 → 落盘这条真实路径**。

所以这里的规矩是：新增任何 `python -m` 入口，就在这里加一条。模型调用可以 mock，
argparse 与参数装配不许 mock。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

MODULES = [
    "pipeline",
    "workflows.run_routing",
    "workflows.score_routing",
    "workflows.score_full",
    "workflows.compare_runs",
    "workflows.grade",
    "workflows.gen_cases",
    "workflows.import_skill",
    "workflows.suggest",
    "workflows.calibrate_judge",
]


@pytest.mark.parametrize("module", MODULES)
def test_每个入口的_help_可用(module, monkeypatch, capsys):
    """--help 走通 = argparse 定义本身没有语法/默认值错误。"""
    import importlib

    mod = importlib.import_module(module if module != "pipeline" else "pipeline.__main__")
    monkeypatch.setattr(sys, "argv", [module, "--help"])
    with pytest.raises(SystemExit) as exit_info:
        mod.main()
    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_pipeline_init子命令_help_可用(monkeypatch, capsys):
    from pipeline import __main__ as pipeline_main

    monkeypatch.setattr(sys, "argv", ["pipeline", "init", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        pipeline_main.main()
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "--source" in out
    assert "--acceptance" in out
    assert "--confirm-egress" in out


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["archive", "--help"], "--subjects"),
        (["unarchive", "--help"], "--confirm"),
    ],
)
def test_pipeline_subject归档子命令_help_可用(command, expected, monkeypatch, capsys):
    from pipeline import __main__ as pipeline_main

    monkeypatch.setattr(sys, "argv", ["pipeline", *command])
    with pytest.raises(SystemExit) as exit_info:
        pipeline_main.main()
    assert exit_info.value.code == 0
    assert expected in capsys.readouterr().out


def test_pipeline_archive与unarchive走完整CLI且默认只读(tmp_path, monkeypatch, capsys):
    from pipeline import __main__ as pipeline_main

    skill = tmp_path / "subjects" / "alpha" / "v1" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: alpha\ndescription: alpha capability\n---\nbody",
        encoding="utf-8",
    )
    dataset = tmp_path / "evals" / "datasets" / "routing_alpha_v1.0.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(
        '{"id":"alpha-pos-01","prompt":"x","expected_skills":["alpha"]}\n',
        encoding="utf-8",
    )
    archive = tmp_path / "archives" / "alpha.skilleval.tar.gz"
    monkeypatch.setattr(pipeline_main, "ROOT", tmp_path)

    monkeypatch.setattr(sys, "argv", [
        "pipeline", "archive", "--subjects", "alpha", "--output", str(archive),
    ])
    with pytest.raises(SystemExit) as exit_info:
        pipeline_main.main()
    assert exit_info.value.code == 0
    assert "read-only" in capsys.readouterr().out
    assert skill.is_file()
    assert not archive.exists()

    monkeypatch.setattr(sys, "argv", [
        "pipeline", "archive", "--subjects", "alpha", "--output", str(archive),
        "--confirm",
    ])
    pipeline_main.main()
    assert archive.is_file()
    assert not skill.exists()
    assert "ARCHIVED:" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", [
        "pipeline", "unarchive", str(archive), "--confirm",
    ])
    pipeline_main.main()
    assert skill.is_file()
    assert archive.is_file()
    assert "RESTORED:" in capsys.readouterr().out


def test_gen_cases_main_走完整条_CLI_到落盘(tmp_path, monkeypatch, capsys):
    """这条就是当初漏掉的那条：main() 的参数装配必须真的跑一遍。"""
    from workflows import gen_cases

    catalog = tmp_path / "catalog"
    for name in ("alpha", "beta"):
        directory = catalog / name / "v1"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} 的能力描述\n---\n正文",
            encoding="utf-8",
        )

    response = json.dumps(
        {
            "cases": [
                {"id": "alpha-pos-01", "prompt": "做主线任务",
                 "expected_skills": ["alpha"], "tags": ["随手写的中文标签"], "severity": "high"},
                {"id": "beta-amb-01", "prompt": "做边界任务",
                 "expected_skills": ["beta"], "tags": [], "severity": "medium"},
                {"id": "none-rej-01", "prompt": "问一个贴边但无关的问题",
                 "expected_skills": [], "tags": [], "severity": "low"},
                {"id": "alpha+beta-multi-01", "prompt": "先甲后乙",
                 "expected_skills": ["alpha", "beta"], "tags": [], "severity": "high"},
            ],
            "review_notes": ["复核 beta-amb-01"],
            "rejection_notes": [
                {"case_id": "none-rej-01", "why_not": "alpha 与 beta 都不覆盖这种请求"}
            ],
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(gen_cases, "call_litellm", lambda **_: response)

    out = tmp_path / "draft"
    monkeypatch.setattr(sys, "argv", [
        "gen_cases",
        "--skill-dir", str(catalog / "alpha"),
        "--acceptance", "alpha 把活干完就算过",
        "--count", "4",
        "--include-neighbors",
        "--output-dir", str(out),
    ])
    gen_cases.main()

    # 草稿文件名必须带 scope —— 它会原样变成 output 目录名的第一维。
    dataset = out / "routing_alpha_v0.1-draft.jsonl"
    assert dataset.is_file()
    assert (out / "suite.yaml").is_file()
    assert "已生成 DRAFT" in capsys.readouterr().out

    from contracts import load_cases, load_suite

    cases = load_cases(dataset)
    assert len(cases) == 4
    # 自由文本标签被规范成四个枚举之一
    assert {tag for case in cases for tag in case.tags} <= {
        "positive", "ambiguous", "no-skill", "multi-skill"
    }
    assert load_suite(out / "suite.yaml").skills.cfg == "v1-draft"
    review = (out / "REVIEW.md").read_text(encoding="utf-8")
    assert "alpha 与 beta 都不覆盖这种请求" in review


def test_gen_cases_拒绝没有理由的_rej_题(tmp_path, monkeypatch):
    """rej gold 是最容易错也最贵的一类，缺理由必须在写文件前拦下。"""
    from workflows import gen_cases

    catalog = tmp_path / "catalog"
    for name in ("alpha", "beta"):
        directory = catalog / name / "v1"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} 的能力描述\n---\n正文",
            encoding="utf-8",
        )
    response = json.dumps(
        {
            "cases": [
                {"id": "alpha-pos-01", "prompt": "做主线任务",
                 "expected_skills": ["alpha"], "tags": [], "severity": "high"},
                {"id": "beta-amb-01", "prompt": "做边界任务",
                 "expected_skills": ["beta"], "tags": [], "severity": "medium"},
                {"id": "none-rej-01", "prompt": "问一个贴边但无关的问题",
                 "expected_skills": [], "tags": [], "severity": "low"},
                {"id": "alpha+beta-multi-01", "prompt": "先甲后乙",
                 "expected_skills": ["alpha", "beta"], "tags": [], "severity": "high"},
            ],
            "review_notes": [],
            "rejection_notes": [],
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(gen_cases, "call_litellm", lambda **_: response)
    monkeypatch.setattr(sys, "argv", [
        "gen_cases",
        "--skill-dir", str(catalog / "alpha"),
        "--acceptance", "验收标准",
        "--count", "4",
        "--include-neighbors",
        "--output-dir", str(tmp_path / "draft"),
    ])
    with pytest.raises(ValueError, match="没有给出"):
        gen_cases.main()
    assert not (tmp_path / "draft").exists()


def test_run_routing_main_用_mock_跑完整条链路(tmp_path, monkeypatch):
    """run → 归档目录 → runs.jsonl，全部走 CLI，不调任何真实模型。"""
    from workflows import run_routing

    monkeypatch.setattr(run_routing, "ROOT", run_routing.ROOT)
    outputs = tmp_path / "outputs"

    def fake_run_dir(suite, model_id, execution_id=None):
        group = outputs / f"{suite['skills']['cfg']}__{model_id}"
        return group / execution_id if execution_id else group

    monkeypatch.setattr(run_routing, "run_dir", fake_run_dir)
    monkeypatch.setattr(sys, "argv", [
        "run_routing",
        "--suite", "evals/suites/example_routing.yaml",
        "--mock",
        "--execution-id", "smoke-01",
    ])
    run_routing.main()

    archives = list(outputs.glob("*/smoke-01/runs.jsonl"))
    assert len(archives) == 1
    lines = archives[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 14 * 2          # 14 cases × 2 repeats
    assert json.loads(lines[0])["case_id"]


def test_run_routing_main_full模式提示_score_full(tmp_path, monkeypatch, capsys):
    from workflows import run_routing

    outputs = tmp_path / "outputs"

    def fake_run_dir(suite, model_id, execution_id=None):
        group = outputs / f"{suite['skills']['cfg']}__{model_id}"
        return group / execution_id if execution_id else group

    class FakeRuntime:
        name = "fake-full"

        def capabilities(self):
            from contracts import RuntimeCapabilities

            return RuntimeCapabilities(
                runtime=self.name,
                skill_modes=["none", "routing_only", "full"],
                tools=True,
            )

        def fingerprint(self):
            return {"runtime": "fake-full"}

        def run(self, req):
            from contracts import RunResult

            return RunResult(
                case_id=req.case_id,
                repeat_index=req.repeat_index,
                model=str(req.model.get("id", "fake")),
                status="success",
                selected_skills=req.skills[:1] and [req.skills[0].skill_id] or [],
                runtime_name=self.name,
                tool_calls=[],
                artifacts=[],
                ok=True,
            )

    # This must use the tracked full example exactly as a fresh clone does.
    # A private/locally generated suite would let the public entrypoint rot.
    monkeypatch.setattr(run_routing, "build_runtime", lambda suite, cases, mock: FakeRuntime())
    monkeypatch.setattr(run_routing, "run_dir", fake_run_dir)
    monkeypatch.setattr(sys, "argv", [
        "run_routing",
        "--suite", "evals/suites/example_full.yaml",
        "--execution-id", "smoke-full-01",
    ])
    run_routing.main()
    out = capsys.readouterr().out
    assert "python -m workflows.score_full --dir" in out


def test_pipeline_plan_main_打印计划且不写盘(monkeypatch, capsys, tmp_path):
    from pipeline import __main__ as pipeline_main

    monkeypatch.setattr(sys, "argv", [
        "pipeline", "plan", "--suite", "evals/suites/example_routing.yaml", "--mock",
    ])
    with pytest.raises(SystemExit) as exit_info:
        pipeline_main.main()
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "READY. Execute only after confirming this plan" in out
    assert "no model request was made" in out


def test_pipeline_init默认只展示计划且不写文件(tmp_path, monkeypatch, capsys):
    from pipeline import __main__ as pipeline_main

    source = tmp_path / "installed" / "alpha"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: alpha 能力\n---\n正文",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_KEY", "test-key")
    monkeypatch.setenv("MODEL_BASE", "https://generator.example.test/v1")
    monkeypatch.setattr("pipeline.initialize.load_dotenv", lambda *_: False)
    monkeypatch.setattr(sys, "argv", [
        "pipeline", "init",
        "--source", str(source),
        "--acceptance", "明确任务应激活，普通聊天拒绝",
        "--dest-root", str(tmp_path / "subjects"),
        "--output-dir", str(tmp_path / "draft"),
        "--count", "3",
        "--api-base-env", "MODEL_BASE",
        "--api-key-env", "MODEL_KEY",
    ])

    with pytest.raises(SystemExit) as exit_info:
        pipeline_main.main()

    assert exit_info.value.code == 0
    assert "Skill evaluation initialization plan" in capsys.readouterr().out
    assert not (tmp_path / "subjects").exists()
    assert not (tmp_path / "draft").exists()


def test_pipeline_init确认本地写入后仍需单独确认外发(tmp_path, monkeypatch):
    from pipeline import __main__ as pipeline_main

    source = tmp_path / "installed" / "alpha"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: alpha 能力\n---\n正文",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_KEY", "test-key")
    monkeypatch.setenv("MODEL_BASE", "https://generator.example.test/v1")
    monkeypatch.setattr("pipeline.initialize.load_dotenv", lambda *_: False)
    monkeypatch.setattr(sys, "argv", [
        "pipeline", "init",
        "--source", str(source),
        "--acceptance", "明确任务应激活，普通聊天拒绝",
        "--dest-root", str(tmp_path / "subjects"),
        "--output-dir", str(tmp_path / "draft"),
        "--count", "3",
        "--api-base-env", "MODEL_BASE",
        "--api-key-env", "MODEL_KEY",
        "--confirm",
    ])

    with pytest.raises(SystemExit, match="confirm-egress"):
        pipeline_main.main()

    assert not (tmp_path / "subjects").exists()
    assert not (tmp_path / "draft").exists()


def test_pipeline_real_run_自动预检并要求单独确认外发(monkeypatch, capsys):
    from pipeline import __main__ as pipeline_main

    checked = []
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://api.example.test/v1")
    monkeypatch.setattr("pipeline.plan.load_dotenv", lambda *_: False)
    monkeypatch.setattr(
        "pipeline.plan.endpoint_health",
        lambda model: (checked.append(model["id"]) or True, "DNS-only test"),
    )
    monkeypatch.setattr(sys, "argv", [
        "pipeline",
        "run",
        "--suite", "evals/suites/example_routing.yaml",
        "--confirm",
        "--execution-id", "egress-consent-test",
    ])

    with pytest.raises(SystemExit, match="confirm-egress"):
        pipeline_main.main()

    out = capsys.readouterr().out
    assert checked == ["qwen3.7-max-2026-05-17"]
    assert "external data movement:" in out
    assert "provider auth/model availability/quota were NOT verified" in out


def test_pipeline_run_不带confirm_必须拒绝执行(monkeypatch, capsys):
    from pipeline import __main__ as pipeline_main

    monkeypatch.setattr(sys, "argv", [
        "pipeline", "run", "--suite", "evals/suites/example_routing.yaml", "--mock",
    ])
    with pytest.raises(SystemExit) as exit_info:
        pipeline_main.main()
    assert "refusing to run" in str(exit_info.value)


def test_import_skill_main_把安装目录桥接进_subjects(tmp_path, monkeypatch, capsys):
    from workflows import import_skill

    source = tmp_path / "installed_skills" / "humanizer"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: humanizer\ndescription: 去掉 AI 味\n---\n正文",
        encoding="utf-8",
    )
    (source / "scripts").mkdir()
    (source / "scripts" / "check.py").write_text("print('ok')", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text(
        "url = https://example.test/private.git", encoding="utf-8"
    )
    (source / ".env").write_text("SECRET=do-not-copy", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "import_skill",
        "--source", str(source),
        "--dest-root", str(tmp_path / "subjects"),
        "--version", "v7",
    ])
    import_skill.main()

    imported = tmp_path / "subjects" / "humanizer" / "v7"
    assert (imported / "SKILL.md").is_file()
    assert (imported / "scripts" / "check.py").is_file()
    assert not (imported / ".git").exists()
    assert not (imported / ".env").exists()
    assert (imported / "_meta.json").is_file()
    metadata = json.loads((imported / "_meta.json").read_text(encoding="utf-8"))
    assert metadata["source"] == "external:humanizer"
    assert str(tmp_path) not in (imported / "_meta.json").read_text(encoding="utf-8")
    assert "已导入被测快照" in capsys.readouterr().out


def test_suggest_apply_main_走到新版本目录与复验suite(tmp_path, monkeypatch, capsys):
    """P3 闭环的落盘那一段：建议 → v2 目录 → 同题复验 suite，全程不打真实 API。"""
    import yaml

    from contracts import RunResult
    from workflows import suggest

    skills_root = tmp_path / "subjects"
    (skills_root / "brief" / "v1").mkdir(parents=True)
    (skills_root / "brief" / "v1" / "SKILL.md").write_text(
        "---\nname: brief\ndescription: 会议纪要\n---\n把会议记录整理成纪要。", encoding="utf-8")

    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        json.dumps({"id": "brief-pos-01", "prompt": "整理成纪要",
                    "expected_skills": ["brief"], "expect_tools": ["write"]},
                   ensure_ascii=False) + "\n",
        encoding="utf-8")

    run_dir = tmp_path / "run"
    (run_dir / "inputs" / "skills" / "brief").mkdir(parents=True)
    (run_dir / "inputs" / "skills" / "brief" / "SKILL.md").write_text(
        "---\nname: brief\ndescription: 会议纪要\n---\n把会议记录整理成纪要。", encoding="utf-8")
    (run_dir / "config.snapshot.yaml").write_text(
        yaml.safe_dump({
            "suite": {"dataset": str(dataset), "repeats": 1,
                      "skills": {"dir": str(skills_root), "mode": "full",
                                 "include": ["brief"], "cfg": "v1"}},
            "resolved_model": {"model": "openai/test", "api_base_env": "TEST_BASE",
                               "api_key_env": "TEST_KEY", "params": {}},
        }),
        encoding="utf-8")
    (run_dir / "runs.jsonl").write_text(
        RunResult(case_id="brief-pos-01", repeat_index=0, model="m",
                  loaded_skills=["brief"], final_answer="整理好了").model_dump_json() + "\n",
        encoding="utf-8")
    (run_dir / "scores.json").write_text(
        json.dumps({"gate_pass": False}), encoding="utf-8")

    def fake_completion(*, prompt, json_mode=True, **_):
        if json_mode:
            return json.dumps({"suggestions": [{
                "pattern": "没写死必须落盘",
                "case_ids": ["brief-pos-01"],
                "metric": "task_completion",
                "evidence": [{"case_id": "brief-pos-01", "quote": "声明必须调用的 tool 没调"}],
                "change": "正文补一条：必须把结果写成文件。",
            }]}, ensure_ascii=False)
        return "---\nname: brief\ndescription: 会议纪要\n---\n整理成纪要，并必须把结果写成文件。"

    monkeypatch.setattr(suggest, "call_litellm", fake_completion)
    monkeypatch.setattr(sys, "argv",
                        ["workflows.suggest", "--run-dir", str(run_dir), "--apply",
                         "--confirm-egress"])
    suggest.main()

    report = json.loads((run_dir / "improvements" / "round-01" / "suggestions.json")
                        .read_text(encoding="utf-8"))
    assert report["apply_status"] == "applied"
    assert report["applied_version"] == "v2"
    assert report["source_skill_hash"] != report["applied_skill_hash"]

    new_skill = skills_root / "brief" / "v2" / "SKILL.md"
    assert "必须把结果写成文件" in new_skill.read_text(encoding="utf-8")
    # 源版本不许被动过
    assert "必须把结果写成文件" not in (
        skills_root / "brief" / "v1" / "SKILL.md").read_text(encoding="utf-8")

    reeval = yaml.safe_load((run_dir / "improvements" / "round-01" / "reeval.suite.yaml")
                            .read_text(encoding="utf-8"))
    assert reeval["skills"]["versions"] == {"brief": "v2"}
    assert reeval["skills"]["target"] == ["brief"]
    assert reeval["dataset"] == str(dataset)
    assert "pipeline run --suite" in capsys.readouterr().out
