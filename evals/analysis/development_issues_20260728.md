# 开发问题清单（2026-07-28）

来源：2026-07-28 普通用户视角下的 `humanizer` 安装与 eval 流程测试。
目标：把会影响“第一次上手是否顺”的问题集中记下来，供后续开发排期。

## 状态说明

- `已修`：本仓库内已完成修复或补文档。
- `外部依赖`：问题存在，但根因在 SkillHub / 外部运行环境，不在本仓库内直接修。
- `待后续`：当前已确认，但这轮没有继续做自动化或产品化闭环。

## 已确认问题

### 1. SkillHub 搜索失败时提示像“真没这个 skill”

状态：`外部依赖`

- 场景：受限网络下执行 `skillhub search humanizer`
- 现象：输出 `search request failed` 后又输出 `No skills found.`
- 风险：普通用户会误判为商店里没有这个 skill，而不是 DNS / 网络失败
- 期望：把“网络失败”和“空搜索结果”分成两种明确状态；最好非 0 退出码

说明：这次测试里无法在 `skillEval` 仓库内直接修；已作为上游 CLI 体验问题记录。

### 2. SkillHub 安装目录和 eval 目录模型不一致

状态：`已修`

- 场景：`skillhub install humanizer --dir ...` 安装成功后，eval suite 仍默认读取 `subjects/<skill>/vN/SKILL.md`
- 现象：安装产物和被测输入快照是两套目录约定
- 风险：普通用户很难立刻明白“安装到哪”和“评测读哪”不是同一件事
- 期望：README / RUNBOOK 里更直接解释“下载产物 -> 版本化快照 -> eval 输入”的关系，或提供桥接脚本

落地：

- 新增 `python -m workflows.import_skill --source skills/<slug> --version v1`
- `README.md` / `evals/RUNBOOK.md` 已补 `skills/` vs `subjects/` 的区别与桥接命令
- `tests/test_cli_entrypoints.py` 已覆盖该 CLI 冒烟

### 3. `pipeline plan --healthcheck` 很有用，但时间戳可读性有歧义

状态：`已修`

- 场景：当前测试日期按 2026-07-28 处理
- 现象：新 run 目录名生成为 `20260729T...+0200`
- 风险：用户会把“今天的测试”误认为“未来一天的测试”
- 期望：在计划输出里同时显示绝对本地日期与时区，避免只靠目录名理解时间

落地：`pipeline plan` 现已显式打印 `local time: ... (archive IDs below use this local timezone)`。

### 4. `workflows.suggest` 默认模型不可用时没有沿用最近成功模型

状态：`已修`

- 场景：V1 跑在 `glm-5.1` 成功，但 `suggest` 默认仍是 `openai/qwen3.7-max-2026-05-17`
- 现象：如果默认模型配额耗尽，用户需要自己知道要手动改 `--model`
- 风险：用户会误以为建议模块本身坏了
- 期望：默认继承 source run 的模型，或至少在报错前提示当前默认值与最近成功 run 不一致

落地：`workflows.suggest` 现在会优先从 source run 的 `config.snapshot.yaml` 继承
`resolved_model.model`、`api_base_env`、`api_key_env` 与 params；仅在快照缺失时才回退默认值。

### 5. `workflows.suggest` 参数心智负担偏高

状态：`已修`

- 场景：用户已经有一个完整 run 目录
- 现象：仍需手填 `--run-dir` 和 `--skill-file`
- 风险：用户不知道为什么程序不能从 `config.snapshot.yaml` 推断 skill 文件
- 期望：允许只给 `--run-dir`，其余从快照自动补齐；显式覆盖仍可保留

落地：

- `--skill-file` 改为可选
- 新增 `--skill-id`
- 可从 `config.snapshot.yaml`、`inputs/skills/`、`skills.versions`、`skills.include` 自动推断目标 skill
- 文档已补最短用法：只给 `--run-dir` 即可先试

### 6. LiteLLM 失败噪音较大

状态：`已修`

- 场景：网络失败或 provider 失败
- 现象：终端会反复打印 `Give Feedback / Get Help`
- 风险：高噪音覆盖真正有用的失败摘要与 remediation
- 期望：默认压低第三方库噪音，保留结构化失败总结

落地：新增 `workflows/litellm_support.py`，在 `gen_cases` / `suggest` / `grade` / `litellm runtime`
统一走 `quiet_completion()`，压掉 LiteLLM 的重复支持横幅。

### 7. disabled skill 的告警是对的，但生产含义还不够直白

状态：`已修`

- 场景：catalog 中含 `disable: true` 的 skill
- 现象：`plan` 会告警，但普通用户仍不确定这次测的是“评测 catalog”还是“真实 agent 加载 catalog”
- 风险：把 routing 结果误读成生产可用性结论
- 期望：在告警里更直接写清“本次结果不等同于真实加载集”

落地：`pipeline plan` 现在会明确写出“本次 routing-only 测的是仍把它暴露给模型的 catalog，
不等同于 Agent 实际加载集；如果生产遵循 disable，通常不会加载这些 skill”。

## 本轮新增待验证

### 8. `gen_cases` 的最小题量和邻居模式存在隐性耦合

状态：`已修`

- 场景：用户想做最薄的 3-case smoke，同时带 `--include-neighbors`
- 现象：脚本直接报 `case_count=3 小于必需题型数 4`
- 风险：用户直到真正执行时才知道 3 题不合法，不是 plan-time 或参数校验时提前提示
- 期望：CLI 直接提示“带 neighbors 时至少 4 题”，或自动把最小值提升到 4

落地：

- `workflows.gen_cases` 现在在真正发模型请求前就做参数校验
- 报错文案直接说明：带 `--include-neighbors` 时 `--count` 至少设为 `4`
- `README.md` / `evals/CASEGEN.md` 已补这个约束
- `tests/test_gen_cases.py` 已覆盖

### 9. `gen_cases` 产物头部日期与当前测试日期不一致

状态：`已解释`

- 场景：本轮测试按 2026-07-28 记录
- 现象：草稿头部 `generated_at` 写成了 `2026-07-29T00:41:30+02:00`
- 风险：用户复盘时会混淆“题是什么时候生成的”
- 期望：明确统一本地时区日期，或在报告里额外显示绝对日期解释

说明：这不是时间写错，而是本地时区已进入 2026-07-29。根因是之前测试记录按“7 月 28 日晚间”
口径描述，但产物按真实本地绝对时间落盘。当前已通过 `pipeline plan` 的本地时区提示降低歧义；
若后续仍频繁误解，再考虑在更多 CLI 输出中重复打印本地绝对日期。

### 10. OpenClaw full healthcheck 在沙箱内会出现假失败

状态：`已修`

- 场景：`pipeline plan --suite ... --healthcheck`，suite 使用 `runtime: openclaw`
- 现象：沙箱内报 `unable to open database file`；同一命令脱沙箱后立即 `runtime=healthy`
- 风险：普通用户会把环境权限问题误判成 OpenClaw 本身不可用
- 期望：在 healthcheck 输出里区分“沙箱权限导致的 profile/db 访问失败”和“runtime 真故障”

落地：

- `pipeline plan` 现在会在 healthcheck 详情里追加权限解释
- `OPENCLAW.md` / `evals/RUNBOOK.md` 已同步写明该报错常见于环境写权限限制，而非 runtime 坏掉

### 11. full 模式执行结束后的“下一步”提示还是 `score_routing`

状态：`已修`

- 场景：`workflows.run_routing` 实际跑的是 full suite
- 现象：运行结束时打印的下一步是 `python -m workflows.score_routing --dir ...`
- 风险：用户会跟着跑错 scorer，尤其在手动分步运行时更明显
- 期望：根据 `skills.mode` 打印正确的下一步命令；full 模式应提示 `workflows.score_full`

落地：`workflows.run_routing` 已按 `skills.mode` 区分后续 scorer，并有 CLI 测试覆盖。

## 本轮新增待验证

- `workflows.suggest --apply`、自动写出 `subjects/<skill-id>/v<N+1>/`、以及同题复验闭环仍未实现
- `skillhub search` 的错误语义仍依赖上游修复
