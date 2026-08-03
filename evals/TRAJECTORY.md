# Trajectory 评估

## 目录与数据流

Trajectory execution eval 的前置条件是：`skills.mode: full`、`runtime: openclaw`、
`environment.backend: docker`。也就是说，四层结果都建立在 OpenClaw agent loop
实际执行完成后的 `RunResult`、Docker workspace 状态和产物证据上；routing-only /
LiteLLM 只负责 skill 路由，不生成 trajectory。

```text
contracts/trajectory.py          # 通用事件和题目期望
evaluators/
  base.py                        # Evaluator Protocol + 注册表工厂
  outcome.py                     # 最终结果统一视图
  trajectory.py                  # 通用轨迹体检 / 后续 skill-specific 扩展点
  reliability.py                 # repeat / variance / flaky
  efficiency.py                  # duration / tokens / tool calls / errors
workflows/grade_trajectory.py    # 第一阶段 LLM-as-judge
evals/datasets/*trajectory*.jsonl
evals/suites/*trajectory*.yaml
outputs/<run>/
  runs.jsonl                     # 原始归一化 RunResult，事实来源
  trajectory.jsonl               # 可观察事件投影，便于审计/Viewer
  trajectory_grading.<judge>.json
  scores.json                    # 四层 evaluation + 旧 scores 兼容字段
  report.html
```

## 四层输出

```text
Outcome
  task_completion
  artifact_correctness
  final_answer_quality

Trajectory
  tool_selection
  argument_correctness
  order_correctness
  state_persistence
  verification_rate

Reliability
  repeat_pass_rate
  variance
  flaky_rate

Efficiency
  time_seconds
  tokens
  tool_calls
  errors
```

旧的 `scores`、gate 和 `compare_runs` 字段继续保留；新增四层结果在
`scores.json.evaluation` 下。默认不把 trajectory judge 分放进 gate，除非先完成
校准。`score=None` 是证据不可见的 N/A，不是失败。

## 运行

先人工审核 dataset，然后预检：

```bash
.venv/bin/python -m pipeline plan \
  --suite evals/suites/full_trajectory_example.yaml \
  --healthcheck
```

只跑 deterministic 层：

```bash
.venv/bin/python -m pipeline run \
  --suite evals/suites/full_trajectory_example.yaml \
  --confirm --confirm-egress
```

运行独立的输出 judge 和 trajectory judge：

```bash
.venv/bin/python -m pipeline run \
  --suite evals/suites/full_trajectory_example.yaml \
  --stages run,grade,trajectory,score \
  --confirm --confirm-egress
```

## 证据粒度：哪些维度出数、哪些记 N/A

`--json` 的 `meta` 只有聚合 `toolSummary`，但 OpenClaw 把逐次调用写在
`meta.agentMeta.sessionFile` 指向的会话 JSONL 里。adapter 读它，归一成
`evidence_level="exact"` 的 `tool_call` / `tool_result`（`call_id` 配对，参数脱敏 +
截断到 500 字符）。读不到就退回 coarse toolSummary 事件 —— 保留 N/A 好过假证据。

| 维度 | 证据 | 结论 |
| --- | --- | --- |
| `tool_selection` | toolSummary + 逐次事件 | exact |
| `argument_correctness` | 逐次 `arguments` | 证据到位；确定性判定缺 gold 字段，先由 judge 判 |
| `order_correctness` | 事件顺序 + `call_id` | exact，`required_order` 直接出数 |
| `state_persistence` | workspace 前后快照 + artifact hash | 可评估 |
| `verification_rate` | 题目声明的 probe tool + read-back 顺序 | 声明了才出数，见下 |

`score=None` 是证据不可见的 N/A，不是失败。别把它当 0 参与平均。

### verification 怎么判

`verification_rate` 不靠猜「哪个 tool 算 mutate、哪个算 probe」——**题目自己说**：

```json
"required_verification": true,
"verification_tools": ["read"]
```

判定：对每个**本次 run 真的改变过**的产物路径（workspace diff 证明），
如果它先被非 probe 调用写出、之后又被 probe 工具**成功**读过一次，就算验证过；
分数 = 验证过的产物 / agent 显式写出的产物。用到的三样东西都是事实：

* 「状态确实变了」← workspace 前后 diff 的 artifact；
* 「谁指向了这个路径」← exact `arguments`（只认参数值以该路径结尾，
  避免正文里提到文件名被当成读回它自己）；
* 「先写后读」← `step_index` 顺序 + `call_id` 配对。

三种情况仍然记 N/A，不记 0：题目没声明 `verification_tools`、轨迹只有 coarse 事件、
或这次 run 没有产物。runtime 以后能直接给 `verification` 事件时，那个事件优先。

`required_verification: true` 但不给 `verification_tools`，contract 直接拒收 ——
否则这一维永远是 N/A，题目却看起来在要求验证。

## 题目如何准备

`expect_trajectory` 对所有工具通用，不写死 read/write：

```json
"expect_trajectory": {
  "goal": "完成业务目标并确认环境状态已经改变",
  "required_tools": ["some_tool"],
  "forbidden_tools": ["dangerous_tool"],
  "required_order": ["inspect", "mutate"],
  "required_state_change": true,
  "required_verification": true,
  "verification_tools": ["inspect"],
  "assertions": ["最终回答不能只靠口头声明完成"]
}
```

结构化字段先由 deterministic evaluator 判；`assertions` 由第一版通用 judge 判。
未来某个 skill 需要更细的参数 schema、状态机或副作用规则时，新增 evaluator 文件
并用 `@register("skill_name_trajectory")` 注册，不改核心 pipeline。
