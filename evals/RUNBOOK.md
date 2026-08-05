# Eval 运行手册

**问题集 · 配置 · 答案 · 维度评分** 分别存哪、怎么跑、怎么比。写题规则另见 [AUTHORING.md](AUTHORING.md)。

---

## 0. 一句话原则

> **凡是会改变结果的东西，都是 eval 的一部分，必须被版本化并记进产物。**

模型、模型参数、tool 列表、skill 版本、重复次数、评分门槛 —— 换了任何一个，两次结果就不可直接比较。所以它们不能散在 `.env`、CLI 参数或某人的记忆里，必须集中在一个 **suite 文件**里，并且在跑的时候**冻结一份快照进结果目录**。

这就是 promptfoo / OpenAI Evals / lm-eval-harness 的共同做法：一个声明式配置 = 一次可复现的实验。

---

## 1. 四要素归位

| 你说的 | 存在哪 | 形态 | 为什么在这 |
| --- | --- | --- | --- |
| **问题集** | `evals/datasets/*.jsonl` | 一行一 case，文件名带版本 | 题目独立版本化，历史版本不覆盖 |
| **答案** | 内联在 case 的 `expected_*` 字段 | 跟题同一行 | 答案跟题走，改题必改答案，不会脱节 |
| 答案（大文件） | `evals/expected/{case_id}/…` | 参考产物文件 | full eval 的期望输出太大，不适合内联 |
| 输入文件 | `evals/fixtures/…` | pdf/xlsx/txt 等素材 | 只读挂载：case 写 `files: ["evals/fixtures/x.txt"]`，跑的时候按 basename 复制进 workspace 并设只读；多个 case 可共用同一份，各复制各的 |
| **配置** | `evals/suites/*.yaml` | 一个 suite = 一次实验 | 模型/tool/skill 版本/重复数/门槛全在这 |
| 密钥 | `.env` | `KEY=值` | **只有 suite 里的环境变量名进 git，值永不进** |
| 安装源 skill | `installed_skills/<slug>/SKILL.md` | 上游安装产物 | SkillHub / agent 的安装目录示例；**不直接参与评测**，需要先桥接到 `subjects/` |
| 被测 skill | `subjects/<skill-id>/vN/SKILL.md` | frontmatter + 正文 | 用户本地输入，默认被 Git 忽略；routing 只读 frontmatter，`description` 是必需的路由边界 |
| **维度评分** | suite 的 `scoring:` 段 | 指标列表 + gate 阈值 | 评什么、门槛多少，本身就是配置 |
| **判分模型** | suite 的 `scoring.judge` 段 | 模型 + **独立的 `JUDGE_*` env** | 被测模型是实验对象，judge 是量具，两者必须能分开换 |
| **语义维度** | `scoring.judge.dimensions` | 维度名列表 | 跨题通用的尺子，选了就对所有题生效，不用逐题写 |
| 维度定义 | `workflows/dimensions.py` | rubric + 评分锚点 + 来源出处 | 改 rubric = 改判定标准，所以带 `version` 并进判定产物 |
| 结果 | `outputs/{run}/` | 见 §3 | 每次 run 一个目录，自带配置快照 |
| 判定结果 | `outputs/{run}/grading.{judge_id}.json` | 逐条 pass/fail、逐维度 0–1 分 + 证据 | 按 judge 分文件，换尺子重判不覆盖上一次 |

```text
skillEval/
├── evals/
│   ├── AUTHORING.md              写题规范
│   ├── RUNBOOK.md                本文
│   ├── datasets/                 【问题集 + 内联答案】
│   │   ├── routing_example_v1.0.jsonl   ← 已跟踪 routing 示例
│   │   └── full_example_v1.0.jsonl      ← 已跟踪 full 示例
│   ├── suites/                   【配置】
│   │   ├── example_routing.yaml         ← 已跟踪 routing 示例
│   │   └── example_full.yaml            ← 已跟踪 full 示例
│   ├── fixtures/                 【输入素材】full eval 用（git 忽略）
│   └── expected/                 【大件参考答案】full eval 用（git 忽略）
├── installed_skills/<slug>/      上游安装源目录示例（不直接评测）
├── subjects/<skill-id>/vN/       被测 skill 的本地版本化快照（默认忽略）
├── outputs/{run}/                【结果 + 配置快照 + 评分】
└── .env                          仅密钥，不进 git
```

> 这里至少有三层路径，别混：
> `installed_skills/` = 上游安装源目录；
> `subjects/` = 评测真正读取的被测快照目录；
> `outputs/.../inputs/skills/` = 某次 run 归档下的运行时副本。
> 跑评测时默认指向 `subjects/`，不直接读你的日常安装目录，也不会把 `inputs/skills/`
> 当成新的被测源。
> 已安装好的 skill 可用 `python -m workflows.import_skill --source installed_skills/<slug> --version v1`
> 桥接成 `subjects/<skill-id>/v1/`。
> 已有失败 run 时，改进建议入口优先用 `python -m workflows.suggest --run-dir outputs/<...>/<execution_id>`；
> 它会先尝试从 run 快照自动推断模型与目标 skill。

---

## 2. Suite：配置长什么样

路由见 [suites/example_routing.yaml](suites/example_routing.yaml)，真实 agent 执行见
[suites/example_full.yaml](suites/example_full.yaml)。两者都通过统一 `pipeline plan/run`
入口运行。下面先看 routing 配置要点：

```yaml
suite_id: example_routing
suite_version: "1.0"

dataset: evals/datasets/routing_example_v1.0.jsonl   # 问题集
dataset_kind: capability                              # capability | regression

runtime: litellm            # 用什么跑：litellm | openclaw | mock（工厂 key）
routing_input:              # 给路由模型看什么（另一个工厂）
  strategy: direct          # direct | production_context
  options: {}
# runtime_options:          # 传给该 adapter 的构造参数，各 runtime 自己定义
#   profile: skilleval      # openclaw 专用：用独立 profile，不碰 main 配置

skills:
  dir: subjects
  target: [csv-profiler, deliverable-pack, release-notes]  # 这套实验归谁；归档/Viewer 的稳定主键
  include: [csv-profiler, deliverable-pack, release-notes] # 模型实际看见的候选 catalog
  mode: routing_only        # none | routing_only | full
  cfg: v1                   # skill 描述版本 → 进目录名

models:                     # ← 列表！N 个模型 = 同一套题跑 N 遍
  - id: qwen3-max                    # 短名，进目录名
    model: openai/qwen3-max          # LiteLLM 模型串
    api_base_env: DASHSCOPE_BASE_URL # 只写变量名
    api_key_env: DASHSCOPE_API_KEY   # 只写变量名，值在 .env
    params: {temperature: 0}

tools: []                   # routing_only 必须空；full 下是 runtime 强 allowlist

repeats: 3

scoring:
  metrics: [top1, no_skill_rejection, false_activation, ...]
  gate:                     # 发布门槛 → PASS/FAIL
    top1: ">= 0.90"
    no_skill_rejection: ">= 0.90"
    false_activation: "<= 0.05"
```

`skills.target` 与 `skills.include` 不是重复字段：

- `target` 回答“这套测试归谁”，是归档和历史索引的显式 ownership；严格契约要求必填。
- `include` 回答“模型能从谁里面选”，会改变实际路由条件。
- No-Skill 基线仍写 `target: [pdf]`，但故意不把 `pdf` 放进 `include`。这样基线仍归 PDF，
  又不会把目标 skill 暴露给模型。

`dataset_kind` 区分两种用途：

- `capability`：探索新能力，允许通过率较低，用来发现边界和指导改进。
- `regression`：已经人工确认并“毕业”的能力，目标是防止已有行为退化。

它只描述测试集的生命周期，不改变 case 的评分逻辑；默认值 `capability` 仅用于兼容旧 suite。

`target` 进入 `config.snapshot.yaml`，但不进入运行 `config_hash`，因为它不改变 prompt、
catalog 或 `runs.jsonl`；只改归属不应制造“实验条件变化”的假警报。

**三条硬规则：**

0. **`suite_id` / `suite_version` / `description` 不进 `config_hash`。** 它们只给人读，
   改了不会改变 `runs.jsonl`。别的字段改一个 hash 就变，两次结果即不可直接比。
1. **密钥只写 `*_env` 变量名，不写值。** suite 可以进 git，`.env` 不进。
2. **脚本里不留可调参数。** 想改模型/重复数/门槛，改 suite，不改代码 —— 否则改动无法被 `config_hash` 捕获。
3. **adapter 内部会影响结果的常量，必须在 `fingerprint()` 里交代。** system prompt、CLI 版本这类东西不在 suite 里，但改了就会改结果 —— 它们由 adapter 自报，一并进 `config_hash`。

> 仓库保留 `subjects/deliverable-pack/v1/SKILL.md`、`full_example_v1.0.jsonl` 和
> `example_full.yaml` 作为完整的最小 full-eval 样例。真实 skill、其版本和实际运行结果
> 都是用户私有输入：放在相同目录结构供本地运行即可，不要提交。

Docker 示例写 `environment.image_env: SKILLEVAL_OPENCLAW_IMAGE`，因为本地 build 的
固定 image ID 每台机器不同。`pipeline plan/run` 会先把它解析成实际 `sha256:` ID，再
写入快照和 `config_hash`；变量未设置、值是浮动 tag 或长度不合法都会在运行前拒绝。

full suite 的 `tools` 同时进入快照和 runtime 请求，但它不是评分 gold：

- `tools: ["*"]`：开放完整 OpenClaw toolset；仓库 Docker 示例默认采用；
- `tools: [read, write]`：OpenClaw 运行前临时设置 `tools.allow`，请求后恢复；
- `tools: []`：临时设置 `tools.deny: ["*"]`，禁止所有 tool；
- 每题 `expect_tools`：只描述该题应观察到的调用，用于确定性评分。

策略设置会回读校验，失败时拒绝执行。local profile 的“设置 → agent → 恢复”会在同机
线程和 pipeline 进程间串行化，避免并发请求互相覆盖；Docker 每个 request 使用独立
profile。`example_full.yaml` 把完整 toolset 放在逐请求 Docker 容器里；如果改回 local，
就必须重新审视 tool 列表。允许 `exec` 等于允许它通过 shell 产生更广的副作用，tool
名称 allowlist 不能替代容器网络和文件系统隔离。

suite 在进入 runner 前会先通过 `contracts/suite.py` 的严格契约：未知字段、字符串/数字
类型漂移、重复 model ID、非法 gate、routing-only 配 tool、以及疑似明文 secret 都会直接
报错。执行、快照与 hash 使用补齐默认值后的规范化配置，因此“省略默认值”和“显式写默认值”
具有相同语义。

### runtime：用什么跑

`runtime:` 是工厂 key，决定这次 eval 由谁执行。上层代码不认识具体 runtime，只认 `RunResult` 契约，所以换 runtime 不需要改评分逻辑。

| runtime | 干什么 | 支持的 skill_mode | tool | 多轮 |
| --- | --- | --- | --- | --- |
| `litellm` | 一次 completion 完成路由判定 | none, routing_only | ✗ | ✗ |
| `openclaw` | 走真实 agent loop，可执行 skill | none, routing_only, full | ✓ | ✓ |
| `mock` | 假数据验链路，结果不可用于判断 | 全部 | ✗ | ✓（只验编排） |

suite 声明了 runtime 不支持的能力（例如让 `litellm` 跑 `full`、或给它配 `tools`），**跑之前就会被拒绝**。推荐用统一入口做非侵入预检：

```bash
.venv/bin/python -m pipeline plan --suite <suite> --healthcheck
```

这里验证的是本地 runtime/environment 与 endpoint DNS，**不会**做模型 completion，
也不代表 provider 鉴权、模型 ID 或额度已经可用。真实 `pipeline run` 会自动重做这层检查。

`openclaw` 有一个已知现象：如果当前执行环境不允许它写 profile / db，healthcheck 可能报
`unable to open database file`。2026 年 7 月 28 日的本地测试里，这属于**环境权限问题**，
不是 runtime 本身损坏；同一 suite 在脱离该限制的环境中可恢复为 `runtime=healthy`。

新增 runtime：在 `adapters/runtimes/` 下写个类，加 `@register("名字")`，在 `__init__.py` 的 `_AUTOLOAD` 里列上 —— 工厂本身不用改。

### full eval 首次运行：先做 one-case baseline

只要准备的是一套新的 `skills.mode: full` 实验，**第一次真实运行必须先用一
道人工审核过的 case 跑通，作为 baseline**，再扩展到完整题集。这里的目标是验证
执行环境和 full 链路，而不是提前评价 skill 的整体质量：

```text
dataset → plan/healthcheck → OpenClaw 注入 → skill 加载 → tool → artifact → score
```

baseline case 建议选择最短的 happy path，并且至少声明一个可确定性验收的
`expect_tools` 或 `expect_artifacts`；如果需要验证拒答，再另外做一条拒答题，
不要把“没有任何断言”的题当成基线。首次 baseline 建议固定：

- `repeats: 1`
- `parallelism: 1`
- 不启用 judge（先跑 `run,score`）
- 使用独立的 `full_<skill>_baseline_v0.1.jsonl` 和
  `full_<skill>_baseline_v1.yaml`

不要在完整 dataset 上临时删行，也不要增加 `--case-id` 之类的 CLI 覆盖。dataset
是实验输入，必须有自己的路径、content hash 和 run 快照；这样 baseline 才能在
后续完整 run 或 V1/V2 对照中被准确复述。

最小 baseline case：

```jsonl
# review_status: APPROVED — manually reviewed one-case baseline
{"id":"<skill>-baseline-pos-01","prompt":"<最短的真实业务任务>","expected_skills":["<skill>"],"expect_tools":["write"],"expect_artifacts":["out/result.md"],"tags":["full","baseline"],"severity":"high"}
```

对应 suite 只需要把 `dataset:` 指向这份一题数据，并将 `repeats` 和
`parallelism` 设为 1。运行顺序固定为：

```bash
.venv/bin/python -m pipeline plan \
  --suite evals/suites/full_<skill>_baseline_v1.yaml \
  --healthcheck

.venv/bin/python -m pipeline run \
  --suite evals/suites/full_<skill>_baseline_v1.yaml \
  --confirm --confirm-egress
```

只有同时满足以下条件，才把 baseline 标记为通过并开始扩题：`plan` 无阻塞、
run 产生 `runs.jsonl`、目标 skill 被加载、声明的 tool 被调用、声明的 artifact
存在且非空、`scores.json` 成功生成。若失败，先修环境、tool policy、输入挂载或
skill 注入问题；不要直接扩大题量。

baseline 不等于 No-Skill 对照，也不等于正式质量结论。正式 full eval 仍应新建或
使用完整版本化 dataset，并保持 suite 其余条件一致；baseline run 目录保留不覆盖。
这条是当前的运行流程门槛，由人工在 `plan` 和 `scores.json` 上确认，暂不由 runner
强制拦截；仓库里已经验证过的 `example_full.yaml` 是保留的两题公共 smoke 示例。
如果以后要机器强制执行，应该新增 suite 中的 `pipeline.baseline` 声明并把它纳入
快照/hash，而不是添加一个不入配置的 `--case-id` 临时参数。

### routing_input：两级路由怎么切

第一阶段先用 `direct`：模型只看 N 个 skill metadata 和当前问题。这一层快，适合反复修改
description/triggers。达到 gate 后复制 suite，改成第二阶段：

```yaml
routing_input:
  strategy: production_context
  options:
    include_role: true
    include_long_context: true
    include_messages: true
    include_tools: true
```

第二阶段从每道 case 的 `context` 读取生产 role、长上下文、历史对话和只读 Tool/MCP 描述。
它仍不执行 tool。两级共用 LiteLLM runtime，输入由 `adapters/routing_inputs/` 工厂组装；
strategy/options 与策略 fingerprint 都进入 `config_hash`。

### 「N 个 skill 跑 N 遍」怎么表达

| 想对比什么 | 怎么做 |
| --- | --- |
| 多个模型 | 同一 suite 的 `models:` 写多条 → 一次命令跑完，各出一个目录 |
| skill 描述 V1 vs V2 | 复制 suite，加 `skills.versions: {<skill>: v2}` + 改 `skills.cfg: v2`。**`skills.dir` 一个字都不要动** —— 改它等于换 catalog，那不是版本对照；未钉版本固定取 v1 |
| No-Skill 基线 | 复制 suite，改 `skills.mode: none` + `cfg: none` |
| 简单路由 vs 生产上下文 | 复制 suite，改 `routing_input.strategy` 和 dataset |
| 换题目集 | 改 `dataset:` |

**对照原则**：做 V1/V2 或 No-Skill 对比时，除了目标那一项，suite 其余字段必须完全一致 —— 否则分不清是 skill 变好了还是别的变量在起作用。

---

## 3. 结果目录

```text
outputs/{dataset}__{model_id}__{skillcfg}/
└── {execution_id}/          ← 每次 pipeline run 新建，历史永不覆盖
    ├── config.snapshot.yaml ← canonical suite + config_hash + runtime/environment + 时间
    ├── inputs/
    │   ├── dataset.jsonl    ← 本次实际题集副本
    │   └── skills/*/SKILL.md ← 这次 run 实际看到的 skill 副本；是归档，不是新的被测源
    ├── runs.jsonl           ← 每题 × repeat 的原始输出、duration_ms、usage token
    ├── scores.json          ← 质量分 + time/token/tool/error 的 mean/stddev/min/max
    └── report.html          ← 人读版（含效率表）
```

```text
outputs/routing_all_v1.0__qwen3-max__v1/20260728T120000+0200/  # V1 一次执行
outputs/routing_all_v1.0__qwen3-max__v2/20260728T121000+0200/  # V2 一次执行
outputs/routing_all_v1.0__qwen3-max__none/20260728T122000+0200/# No-Skill 基线
```

三层记录，各管各的：

| 层 | 记什么 | 作用 |
| --- | --- | --- |
| **目录名** | dataset / model / skillcfg / execution_id | 人一眼看出条件与哪一次执行 |
| **config.snapshot.yaml** | 全部配置 + `config_hash` | 机器判定两次能不能比 |
| **inputs/** | 原始 dataset + 实际 resolved SKILL.md | 后来改 V2/V3 仍能复现当时输入 |
| **runs.jsonl 每行** | case_id / repeat / 选择 / 原始输出 / 用时 / token usage | 单题可追溯、可重跑 |
| **scores.json** | 质量 + 效率统计 | `compare_runs` 可同时比较正确率、平均用时和 token |

> **`config_hash` 不同 = 不可直接比较。** 比 delta 前先对 hash：只有目标那一项不同才是有效对照。

### 3.1 按 subject 收纳 / 恢复完整测试包

单次 run 已经自带冻结输入，但同一个 subject 的版本、题集、suite、fixture 和多轮历史仍散落在
不同目录。完成某个 skill 的阶段性测试后，可按一个或多个 subject 聚合成校验包：

```bash
# 默认只展示计划：不写包、不删除文件
.venv/bin/python -m pipeline archive --subjects pdf

# 可同时归档共享同一套题的多个 subject
.venv/bin/python -m pipeline archive --subjects pdf docx xlsx --confirm

# 默认同样只展示恢复计划；确认后才恢复
.venv/bin/python -m pipeline unarchive \
  archives/pdf__docx__xlsx__<timestamp>.skilleval.tar.gz
.venv/bin/python -m pipeline unarchive \
  archives/pdf__docx__xlsx__<timestamp>.skilleval.tar.gz --confirm
```

归档包包含 `manifest.json` 和原始相对路径下的 payload。manifest 记录 subject、文件类型、
大小、mode 和 SHA-256；执行顺序固定为“写临时包 → 校验成员与所有 checksum → 原子落盘
→ 再清理源文件”。中途失败不会拿未验证的包换掉原文件。

资源归属优先读取 suite / run `config.snapshot.yaml` 中写死的 `skills.target`；dataset
case scope / `expected_skills` 只负责找关联候选。PDF 专项题里的 docx/xlsx 边界 case
因此不会让邻居共同拥有这套实验。没有 `target` 的旧历史快照才退回版本、dataset 和精确
文件名规则兼容；新 suite 缺 `target` 会在 pipeline 运行前被严格契约拒绝。
`routing_all...` 这类明确写了多个 target 的公共集，只有相关 subject 一起归档时才从
工作区移走。单 subject 归档仍会把共享资源复制进包，但在计划中标为 `retain_shared`。
Git 已跟踪的产品示例同样只收包、不删除。

解归档只做两种操作：目标不存在就恢复；目标内容 hash 相同就复用。任何同路径不同内容、
符号链接或路径越界都会在写盘前阻断，没有覆盖开关。包本身在恢复后保留。

> `archives/` 默认被 Git 忽略，因为包内含私有 skill、题目和模型输出。同一块磁盘上的归档
> 解决的是工作区整洁和迁移，不等于异地备份。

---

## 4. 怎么跑

### 4.0 首次导入自己的 skill

推荐先走统一初始化入口：

```bash
.venv/bin/python -m pipeline init \
  --source installed_skills/<skill> \
  --acceptance-file acceptance.md
```

默认只打印只读计划。确认 snapshot 目标、生成模型和 external data movement 后，给同一命令
加 `--confirm --confirm-egress`。它复用 `workflows.import_skill` 与
`workflows.gen_cases`，创建不可变 snapshot 和 DRAFT dataset/suite，然后停在人审门。
生成调用失败时 snapshot 保留；修复网络/模型后重跑，内容一致的 snapshot 会被复用。
外发计划按最多三次申报：首次生成、仅在结构/契约错误时的一次 repair、rej gold 盲判复审。
repair 仍不合格时不会生成可运行的 dataset/suite，但已付费的 raw response、可解析 candidate
和校验错误会版本化保留在 `generation_failures/`，原命令可直接重跑。详见
[CASEGEN.md](CASEGEN.md) §1.4。
当前新草稿默认生成模型见 [MODEL_POLICY.md](MODEL_POLICY.md)；已从确认额度耗尽的
`qwen3.7-max-2026-05-17` 切到 `qwen3.7-flash-2026-07-15`。这只影响没有显式传模型的新
init，不会改任何已有 suite。provider 额度/鉴权失败时普通 CLI 只给压缩后的分类、恢复命令
和根因；需要第三方完整 traceback 时原命令加 `--debug`。
这里的“一致”按真正会进入评测的文件逐一计算 SHA-256：根目录 `_meta.json` 是
SkillHub/skillEval 的安装与导入元数据，不是 skill 内容，因此 source 与 destination 两边都
排除；`SKILL.md`、references、scripts 或其他附件只要有一字节变化，仍会拒绝复用并要求新
version。导入记录会保存评测内容的 manifest hash，以及上游 `_meta.json` 的 hash（若存在）。

这条入口刻意不带 `--include-neighbors`：首次初始化不应自动把 `subjects/` 里所有个人 skill
当成生产 catalog。需要 multi/邻接边界时，人工决定 catalog 后再调用
`workflows.gen_cases --include-neighbors`。

### 4.1 运行已审核 suite

推荐先用 pipeline 查看不可变的实验计划；它不写结果、不发模型请求：

```bash
.venv/bin/python -m pipeline plan --suite evals/suites/example_routing.yaml
# 确认显示的 dataset / skill / model / output / gate 后：
.venv/bin/python -m pipeline run --suite evals/suites/example_routing.yaml \
  --confirm --confirm-egress
```

plan 会额外列出外部 endpoint、计划请求数和发送的数据类别。`--confirm` 表示确认实验配置，
`--confirm-egress` 表示单独确认这些评测输入将离开本机；mock run 不需要后者，且
`scores.json.gate_pass` 固定为 `null`，只验证管道，不判 skill 质量。

`pipeline run` 默认只跑 `run → score`。judge 可能产生额外调用和费用，保留为
`python -m workflows.grade ...` 的显式操作。下面的底层模块仍可单独使用，方便调试。

每个 suite 的默认策略是：

```yaml
pipeline:
  mode: evaluate_only  # 只执行当前 skills 指向的版本；绝不写 V2/V3
  iteration: 1         # 本轮改进编号；不是模型重复次数
repeats: 3             # 同一题实际执行 3 次，用来测稳定性
parallelism: 4         # 独立 conversation 并发数；同一 conversation 的 turns 不并发
```

想测 V2/V3，显式在 suite 的 `skills.versions` 选择对应版本并更新 `skills.cfg`；pipeline 本身
不会改 skill 文件。某个 `skill@version` 一旦被历史 run 引用，其内容就不可再改；plan 会把
当前 hash 与同一 `skills.dir` 下的历史快照比较，发现漂移就阻断并要求创建 `vN+1`。
以后 P3 接入人工确认后的改进循环时，仍以这个 `iteration` 逐轮归档。

并发单位固定为 `model × case × repeat` 的一段 conversation。多轮题内部必须按
turn 1 → turn 2 → … 串行，共享同一 session/workspace；不同 repeat 永远拿不同环境。
结果完成顺序可以不同，但 `runs.jsonl` 会恢复为确定性的矩阵顺序。本机 OpenClaw 因共享
profile 配置而把 agent 调用串行化；Docker backend 每 conversation 一个容器，可真正并发。

```bash
# 1. 跑（读 suite，遍历 models，各出一个目录）
.venv/bin/python -m workflows.run_routing                                   # 默认 suite
.venv/bin/python -m workflows.run_routing --suite evals/suites/xxx.yaml     # 指定
.venv/bin/python -m workflows.run_routing --mock                            # 不调 API 验链路

# 2. 评（默认评最新一次；门槛从该 run 的 config.snapshot.yaml 读）
.venv/bin/python -m workflows.score_routing
.venv/bin/python -m workflows.score_routing --dir outputs/routing_all_v1.0__qwen3-max__v1

# full eval 不用 score_routing，改用 score_full
.venv/bin/python -m workflows.score_full --dir outputs/effect_xxx__openclaw-default__v1

# 2b. 语义评估 —— judge 是**独立的一把尺子**，模型/端点/key 都跟被测模型分开配
.venv/bin/python -m workflows.grade --list-dimensions                    # 有哪些标准维度、锚点是什么
.venv/bin/python -m workflows.grade --dir outputs/xxx --dry-run          # 先看会外发什么，不调模型
.venv/bin/python -m workflows.grade --dir outputs/xxx                    # 用 suite 里配的 judge + 维度

# 维度是跨题通用的，题里一个字不用改就能评：
.venv/bin/python -m workflows.grade --dir outputs/xxx --dimensions faithfulness completeness relevancy

# 换一把尺子再判一遍（换模型必须换 id，否则拒绝执行、防止覆盖）
.venv/bin/python -m workflows.grade --dir outputs/xxx \
    --judge-id qwen --judge-model openai/qwen3.7-max-2026-05-17
.venv/bin/python -m workflows.score_routing --dir outputs/xxx --judge-id qwen   # 指定并入哪把尺子的分

# 2c. judge 的凭据默认走独立的 JUDGE_BASE_URL / JUDGE_API_KEY。**.env 里没有它们时
#     grade.py 会在外发之前就报错**，并列出当前已配好的 key 变量供你改指向。
#     共用端点没关系，共用同一个 model 才有关系（考生不能改自己的卷子）。

# 2d. 标准维度进 gate 前，离线对齐人工连续分（此命令不调用模型）
.venv/bin/python -m workflows.calibrate_dimensions \
    --gold evals/calibration/<human-dimension-gold>.json \
    --grading outputs/xxx/grading.<judge-id>.json \
    --output evals/calibration/<dimension-calibration>.results.json \
    --registry-output evals/calibration/<dimension-registry>.json
# 不要用模型生成 gold；默认每个维度至少 10 条人工标注。

# 2e. 组合或加载自定义确定性 evaluator
# 见 evals/EVALUATORS.md；suite 用 module.path:registered-name 引用，参数写 evaluator_options。
# 标量 metrics 自动进入 scores/gate；量具 version + 源码 SHA + options 会写入评分产物。

# 3. 比两次（对齐 config_hash 后看 delta）
.venv/bin/python -c "
import json
a=json.load(open('outputs/routing_all_v1.0__qwen3-max__v1/scores.json'))
b=json.load(open('outputs/routing_all_v1.0__qwen3-max__v2/scores.json'))
print('config_hash:', a['config_hash'], 'vs', b['config_hash'])
for k in a['scores']:
    print(f\"{k:<22} {a['scores'][k]:6.1%} → {b['scores'][k]:6.1%}  ({b['scores'][k]-a['scores'][k]:+.1%})\")"
```

如果这次 run 失败了，想让系统先把失败模式聚类成“该改哪句 skill”的建议：

```bash
.venv/bin/python -m workflows.suggest --run-dir outputs/<dataset>__<model>__<skillcfg>/<execution_id>
```

默认行为：

- 要求 run 已有 `scores.json`，因为 gate 是第一停止条件；缺评分会直接提示先运行 scorer
- 先从 `config.snapshot.yaml` 继承 source run 的 `model` / `api_base_env` / `api_key_env`
- 先从 `inputs/skills/`、`skills.versions`、`skills.include` 等快照信息推断目标 skill
- 无法唯一推断时，才要求补 `--skill-id <skill>` 或 `--skill-file <path>`
- 先打印外发 manifest；没有 `--confirm-egress` 时不调模型也不写文件
- 确认外发后只写建议报告，不改 `subjects/<skill-id>/vN/`

full eval 的 run 还会额外收这些失败形态（routing-only 只有"选错 skill"一种）：

- 任务没完成：声明的产物没落下来/为空/类型不符、该调的 tool 没调、拒答题却留了文件
- judge 判 failed 的语义断言，以及 < 0.6 的维度分（**需要先跑过 `grade`**，否则这部分为空）
- 产物原文（`Artifact.text_excerpt`）一并作为证据，改进模型才知道产物到底写成了什么样

产物落在：

```text
outputs/<...>/<execution_id>/improvements/round-01/suggestions.json
```

### 把建议落成下一版并复验

```bash
# 1) 出建议 + 写新版本 + 生成复验 suite（会外发一次模型调用，会新增 subjects/<id>/v<N+1>/）
.venv/bin/python -m workflows.suggest --run-dir outputs/<...>/<execution_id> \
  --apply --confirm-egress

# 2) 同题复验：题集/模型/runtime/judge 全部照抄上一轮，只换 skill 版本
.venv/bin/python -m pipeline run \
    --suite outputs/<...>/<execution_id>/improvements/round-01/reeval.suite.yaml \
    --stages run,grade,score --confirm --confirm-egress

# 3) 比两轮（基线放第一个）
.venv/bin/python -m workflows.compare_runs outputs/<v1 run> outputs/<v2 run>
```

`--apply` 只**新增**版本目录，绝不改写源版本；`name` 被模型改掉、正文原样抄回、版本目录
已存在，这三种情况都会直接报错而不落盘。**没有自动循环** —— 每一轮都要人显式发起，
`pipeline.mode: evaluate_only` 的含义不变。

第一轮可用 `--max-total-tokens` / `--max-total-seconds` 写死累计预算；复验评分后用
`--previous-report <上一轮 suggestions.json>` 继承 lineage、预算和最大迭代数。
预算、`max_iterations` 不允许中途修改；触发 gate PASS、任一预算上限或最大轮数后，
报告会记录原因且不再调用模型。

### 4.2 重新评分，不重新执行

改 rubric、换 Judge 或只想用新版 deterministic evaluator 重算时，不要重新运行 Agent：

```bash
# 默认只展示只读计划；不调用 runtime、模型，也不写文件
.venv/bin/python -m pipeline rescore \
  --run-dir outputs/<group>/<execution-id> \
  --grading-id rubric-v2 \
  --stages score

# 确认后只做确定性重算，仍然不会调用 Agent 或 Judge
.venv/bin/python -m pipeline rescore \
  --run-dir outputs/<group>/<execution-id> \
  --grading-id rubric-v2 \
  --stages score --confirm

# 需要重新跑输出 Judge / trajectory Judge 时显式声明并单独批准外发
.venv/bin/python -m pipeline rescore \
  --run-dir outputs/<group>/<execution-id> \
  --grading-id judge-v2 \
  --stages grade,trajectory,score \
  --judge-id judge-v2 --judge-model openai/<judge-model> \
  --confirm --confirm-egress
```

rescore 禁止调用 runtime，也禁止写 `runs.jsonl`。执行前后都会核对原始 runs、snapshot 和
归档 dataset 的 SHA-256；题目优先读取 `inputs/dataset.jsonl`，不会被工作区后来修改的
同名题库偷换。新产物永不覆盖旧结果：

```text
grading/<judge-id>/<grading-id>.json
grading/<judge-id>/<grading-id>.trajectory.json
scores/<grading-id>.json
scores/<grading-id>.trajectory.jsonl
reports/<grading-id>.html
```

每份 JSON 都记录 `source_runs_sha256`、dataset/snapshot hash、Judge 模型与参数、rubric /
evaluator 版本和 `grading_hash`。同一 source runs、同一量具的 deterministic 分应完全一致。
比较两把量具可直接把版本化 scores 文件交给 compare：

```bash
.venv/bin/python -m workflows.compare_runs \
  outputs/<run>/scores/rubric-v1.json \
  outputs/<run>/scores/rubric-v2.json
```

比较器会标记“同一执行，只是换尺子”：确定性分可比，Judge/rubric 不同的语义分不可解释为
skill delta。二进制产物若历史 run 没保存可读内容，重评仍为证据不足/N/A；rescore 不会为了
补证据偷偷重跑 Agent。

---

## 5. 现在支持到哪、还差什么

| 能力 | 状态 |
| --- | --- |
| 问题集版本化 + 命名规范 | ✅ |
| suite 配置（runtime/模型/skill版本/重复/门槛） | ✅ |
| Suite 严格契约（类型/交叉字段/secret） | ✅ |
| Runtime 抽象 + 注册表工厂 | ✅ litellm / openclaw / mock |
| 能力校验（跑前拦截不合法 suite） | ✅ |
| 多模型一次跑完 | ✅ |
| 确定性任务矩阵（model × case × repeat） | ✅ 唯一 request/session；重跑不复用旧会话 |
| config 快照 + hash（含 runtime fingerprint） | ✅ |
| 维度评分 + gate PASS/FAIL | ✅ |
| Exact skill-set match / multi 题 / 分题型指标 | ✅ |
| **OpenClaw 执行** | ✅ 已打通，真实 full run 有实测归档 |
| **多轮对话** | ❌ 契约(`session_id`)与命名已就位，编排未接 |
| **tool 调用 / full eval** | ✅ 注入 → 加载 → 调 tool → 产物归一化 |
| **artifact 产物校验** | ✅ 存在/非空/MIME 三条确定性判定；文本产物内容进 judge 输入 |
| **语义 judge（断言 + 维度分）** | ✅ 独立模型、独立凭据；`--stages run,grade,score` 一条命令串起来 |
| **改进闭环（建议 → v(N+1) → 同题复验）** | ✅ `suggest --apply`；每轮人工发起，不自动循环 |
| **No-Skill 基线** | ✅ 目标 skill 通过 `exclude` 完全不可见，已有 none/v1/v2 实测 |
| **跨 run delta + 污染检测** | ✅ `workflows/compare_runs.py`；HTML 落在 `outputs/compare__<A>__vs__<B>.html`，不会互相覆盖 |
| **Case 输入文件挂载** | ✅ `files:` → workspace 只读物化；缺文件 `plan` 阶段拦下 |
| **Critical Skill Miss Rate** | ✅ 由 `severity: critical` 驱动，可进 gate |
| **单变量对照护栏** | ✅ `skills.versions` 只钉目标 skill；其余默认固定 v1，skill 被 `disable` → `plan` 告警 |
| **每个 CLI 入口的 main() 冒烟测试** | ✅ `tests/test_cli_entrypoints.py` |

接下一块能力时：**先在 suite 里加字段（或在 adapter 的 `fingerprint()` 里交代），再改 runner**，不要走 CLI 参数 —— CLI 参数不进 `config_hash`，会造成"结果对不上但看不出为什么"。
