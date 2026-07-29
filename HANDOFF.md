# Handoff

接手先读这一份。**状态：可用**，路由 eval 全链路跑通，三级对照 + 双模型切换都已实测验证。
用法 → [README.md](README.md)｜架构 → [AGENTS.md](AGENTS.md)｜过程与坑 → [DEVLOG.md](DEVLOG.md)

---

## 0.0 2026-07-29：full eval 的改进闭环跑通（最新变更）

P3 从"只出建议"补成完整闭环，并**实跑验证过一遍**：

```text
full run ──→ grade（judge 判断言 + 维度）──→ score ──→ suggest --apply
   ↑                                                         │
   └────────── 同题复验（只换 skill 版本）←── v(N+1) + reeval.suite.yaml
```

实跑记录（`meeting-and-brief`，3 题 × 1 次，OpenClaw）：

| 步骤 | 命令 | 结果 |
| --- | --- | --- |
| R1 | `pipeline run --stages run,grade,score --execution-id loop-r1` | task_completion 100%、assertion 90%、四个维度分都出来了 |
| 建议+落地 | `suggest --run-dir <r1> --model openai/qwen3.7-max --apply` | 写出 `subjects/meeting-and-brief/v3/`（只加了 2 行规则）+ `improvements/round-01/reeval.suite.yaml` |
| R2 | `pipeline run --suite <reeval.suite.yaml> --execution-id loop-r2` | 跑通，产物齐 |
| 对比 | `compare_runs <r1> <r2>` | 唯一差异是 `[对比维度] skillcfg: v1 → v1-v3`，**无污染项** |

### 改了什么

| 改动 | 为什么 |
| --- | --- |
| `Artifact.text_excerpt`（文本产物内容前缀，4000 字符） | workspace 跑完即删，judge 只看得见文件名 —— "报告里的数字是不是编的"永远判"证据不足" |
| `grade` 把 **case 的输入文件原文**也喂给 judge | 见下面那条坑，这是本轮最重要的一条 |
| `pipeline run --stages run,grade,score` | grade 以前只能手动单跑；它是付费外发，所以仍**不进默认 stages**，但顺序由代码保证（grade 一定在 score 前） |
| `suggest` 按 run 的 `skills.mode` 取失败证据 | full 的失败形态是"缺产物/没调 tool/断言没过"，跟 routing 的"选错 skill"完全不同 |
| `suggest --apply` | 写 `subjects/<id>/v<N+1>/`（整目录复制附件）+ 复验 suite；源版本一个字不动 |
| quote 校验忽略空白与 markdown 标记 | 模型复制证据时爱把 `` ` `` 和 `**` 剥掉，那不是编造，不该整批拒收 |

### ⚠️ 本轮最值钱的一条教训：judge 看不到输入 = 假失败 = 改错方向

R1 那次 judge 判「『赵磊』是文字稿里不存在的人名」，于是 `--apply` 照着这条写出了
v3 的"禁止脑补责任人"规则。**但文字稿第 5 行原文就是「框架8月10号之前，赵磊负责」** ——
judge 当时看不到输入文件，只能拿产物里的引文反推，判了一次彻底的假失败。

已修（输入文件进 judge prompt），但修完 qwen-plus **仍然**判错同一条。换 `qwen3.7-max`
当 judge 重判，那条假失败消失，同时冒出两条真问题（日期 `2026-07-28` 文字稿里没有依据、
`张付总→张副总` 属于静默改写）：

| judge | assertion | faithfulness | completeness | instruction_following |
| --- | --- | --- | --- | --- |
| qwen-plus（无输入文件） | 90% | 0.67 | 0.83 | 0.67 |
| qwen-plus（有输入文件） | 90% | 0.67 | 1.00 | 0.67 |
| qwen3.7-max（有输入文件） | 80% | 0.83 | 1.00 | 0.93 |

**结论：judge 的输入完整性和模型选择，比 skill 本身更能左右这批数字。**
这就是 AGENTS.md §22.6「judge 与人工标注一致率 ≥ 80% 才够格」和"judge 分默认不进 gate"的现实依据。
`subjects/meeting-and-brief/v3/` 保留作为闭环跑通的证据，**但它基于假失败，不要当下一版用**
（`PROVENANCE.md` 里已写明）。

### 还没做

- pdf V1 能力基准（系统能否自己提出「按任务类型而非文件格式激活」）
- judge 的标准语义维度校准与 calibration registry → gate 强制联动（absolute assertion
  小集已完成：qwen3.7-max 100%、qwen-plus 90%，见 `evals/calibration/`）

### 2026-07-29 补充：校准与停止条件已落地

- `workflows/calibrate_judge.py` 用版本化人工 gold 离线复算三份 grading；
  10 条 absolute assertion 上 qwen3.7-max 100%、两个 qwen-plus 配置均 90%，invalid 0%。
  默认策略已写入 `evals/MODEL_POLICY.md`，语义分仍未接 gate。
- `workflows.suggest` 在任何模型调用前检查 `scores.json.gate_pass`、整条 lineage 的累计
  token/墙钟预算和最大轮数；`--previous-report` 继承预算，命中停止条件后即使
  `--apply` 也不写新版本。
- `suggest` 新增外发 manifest + `--confirm-egress`。PDF V1 已完成 dry-run：
  9 条失败 / 3 个 case / 3868 字符 prompt，**未获授权所以没有调用 DashScope、没有写结果**。

**验证基线：271 passed / 1 skipped。**

---

## 0.1 2026-07-28：真实用户轨迹评测 → 11 条修复

拿从 skillhub 真下载的 `meeting-and-brief` 当被测对象、**只当用户不改代码**走完整条链路，
把踩到的坑逐条修掉。下面是**逐步计划与验收结果**，每一步的验收都实际跑过。

### 先改开发文档（AGENTS.md），再改代码

规范先落地，后面每一步的验收都对着规范判：

| AGENTS.md 章节 | 落了什么规范 |
| --- | --- |
| §7.3b 版本目录 | V1/V2 对照**只能**用 `skills.versions`；一个 skill 一个目录，版本是它下面的 `v1/ v2/`，不钉就取 v1 |
| §7.3c 被停用的 Skill | `disable: true` 照常评测但必须显式标出 |
| §8.3 | Hash 只覆盖会改变结果的字段；纯文档元信息不进 |
| §9.2 对照原则 | 「唯一变量」的表达手段是 versions + cfg，不是 dir |
| §11.4 文件挂载 | 输入文件由 case 的 `files` 声明；**没实现之前不许写进用户文档** |
| §21.3 路由指标 | 新增 `Critical Skill Miss Rate`；定义了却没人消费的字段等于骗人 |
| §25.2 EvalCase | `files` 字段；`tags` 只认四个枚举 |
| §29 工作规则 26–28 | 每个 `python -m` 入口必须有覆盖 `main()` 的冒烟测试；文档只写已实现的能力；报错要先自查再建议 |

### 逐步计划 · 验收标准 · 实际结果

| 步 | 问题（F 编号） | 改了什么 | 验收标准 | 实际结果 |
| --- | --- | --- | --- | --- |
| S1 | **F1** `python -m workflows.gen_cases` 直接 TypeError，而 192 测试全绿 | 参数归位；`main()` 显式传 `completion`；新增 `tests/test_cli_entrypoints.py` | 8 个入口 `--help` 全通；gen_cases 从 argparse 到落盘走一遍且**不打真实 API** | ✅ 13 条测试通过；跑真 API 的隐患一并堵掉（原本首跑 51s） |
| S2 | **F3** overlay 目录里的残留 skill 被静默带进实验 | `skills.target` 契约字段 + `plan` 的 `overlay_warnings()` | 只换目标时不告警；多换时点名是谁；声明 target 后只报目标之外 | ✅ 当轮通过；**已被后续重构取代** —— overlay 整套换成 `subjects/<skill-id>/<vN>` 版本目录后这类污染不可能发生，target 字段与告警一并删除（§7.3b） |
| S3 | **F11a** 改一句 `description` 就让 hash 变 | `_HASH_EXEMPT_SUITE_FIELDS` | 改 `suite_id`/`suite_version`/`description` hash 不变；改 `repeats` 必须变 | ✅ 单测覆盖 |
| S4 | **F4** 生成器出的 rej gold 与 catalog 邻居矛盾 | 强制 `rejection_notes`（缺一条拒绝生成）+ 顶到 REVIEW.md 开头 + `tags` 规范化 + 草稿文件名带 scope | 缺理由 → 抛错且**不落任何文件**；中文自由标签被规范成四枚举 | ✅ 两条测试覆盖 |
| S5 | **F5** RUNBOOK 承诺的 fixtures 挂载代码里零实现 | `RoutingCase.files` → `InvocationRequest.input_files` → `stage_input_files()` 只读物化；缺文件 `plan` 拦下 | 真跑一次 full eval，素材进 workspace 且只读 | ✅ 同一道题 `tool_hit 0% → 100%`（read+write 2/2，落 4 个产物） |
| S6 | **F11d** `severity` 定义了从没被读过 | routing 与 full 两侧算 `critical_miss` | 端到端进 `scores.json` 且能进 gate | ✅ 同批题 qwen3.7-max `0.0% PASS` / qwen-flash `16.7% FAIL` |
| S7 | **F11e** 作者 `disable: true` 的 skill 被静默评测 | `SkillMeta.disabled` + plan 告警 | 两种写法（`disable`/`enabled`）都识别；不泄漏进模型输入 | ✅ 实跑告警命中 `meeting-and-brief` |
| S8 | **F9/F10** 长跑零进度；管道里结果先于 plan 打印 | 逐题进度写 stderr + 父进程 flush | 能看到 `[i/n] case rN`；plan 一定在结果之前 | ✅ 实跑确认 |
| S9 | **F6/F7/F8** judge 凭据缺失/路由拒判提示顺序/openclaw 误导性报错 | 外发前查凭据并列出可用变量；适用性检查提到凭据之前；openclaw 先扫 nvm 再建议 | 三条报错都能直接照做 | ✅ 逐条实跑确认，openclaw 提示直接打出 `node_bin:` 那行 |
| S10 | **F11b** `compare.html` 被静默覆盖 | 文件名带两个 run | 连比两次留下两个文件 | ✅ |
| S11 | **F2/F11f** 文档教错方法、示例与实际输出对不上 | README/RUNBOOK/AUTHORING 全面订正 | V1/V2 段落改成 overlay+target；能力表补三行；`(正向)` → `(单skill)` | ✅ |

**验证基线：216 passed**（当轮；现为 257，见 §0.0）。

### ⚠️ 一处破坏性变更

`config_hash` 不再包含 `suite_id` / `suite_version` / `description`。**本次之后所有新 run 的
hash 都与历史归档不同**，`compare_runs` 拿新旧归档对比会报 hash 不同。历史归档本身不受影响
（`config.snapshot.yaml` 原样保留），需要跨这条线比较时看快照里的实际字段，别只看 hash。

### 这次评测顺带产出的 skill 侧结论（`meeting-and-brief` V1 → V2）

只改 frontmatter，正文一字未动，依据全部落在
`subjects/meeting-and-brief/CHANGELOG.md`：

| 指标（30 题 × 3 次） | qwen3.7-max V1 → V2 | qwen-flash V1 → V2 |
| --- | --- | --- |
| exact_set_match | 86.7% → **91.1%** | 78.9% → **84.4%** |
| no_skill_rejection | 100% → 100% | 38.1% → **66.7%** |
| false_activation | 0% → 0% | 61.9% → **33.3%** |
| type_amb | 83.3% → 80.6% ⬇ | 100% → 94.4% ⬇ |
| tokens/次 | 1308 → 1568 (+20%) | 1060 → 1326 (+25%) |

三条改动一起做的，所以 delta 只能归因到「整体重写 metadata」，拆不到某一条。
**已知回归**：新写的 exclusion「一对一深度访谈让位 interview-insight」把「焦点小组座谈会」
也扫进去了（模型原话直接引用了这条排除），`amb-02` 100% → 0%。下一轮 V3 应把这条收窄。

---

## 0.2 2026-07-28：Pipeline 归档与只评测模式

**从现在起不要把一个逻辑 output 目录当成一次执行。** 每次 pipeline run 都有唯一
`execution_id`，结果在：

```text
outputs/{dataset}__{model}__{skillcfg}/{execution_id}/
├── config.snapshot.yaml       # frozen canonical suite、hash、runtime/environment、时间
├── inputs/dataset.jsonl       # 本次实际题集副本
├── inputs/skills/*/SKILL.md   # versions/exclude 解析后模型实际看到的完整 skill 副本
├── runs.jsonl                 # 原始输出 + duration_ms + usage token
├── scores.json                # 质量指标 + time/token/tool/error mean/stddev/min/max
└── report.html
```

这样之后即使改了 dataset、V2/V3 或 suite 条件，旧 eval 仍可复现；归档已存在时 runner
拒绝覆盖。旧的扁平 `outputs/{group}/runs.jsonl` 仍可评分和对比，但新运行一律写新布局。

**推荐入口：**

```bash
# 只读：显示 suite、实际 skill hash、模型参数、矩阵、输出目录和 gate
.venv/bin/python -m pipeline plan --suite evals/suites/routing_pdf_v1.yaml

# 真实运行前显式确认；自动生成 execution_id，run → deterministic score
.venv/bin/python -m pipeline run --suite evals/suites/routing_pdf_v1.yaml --confirm

# 想让 plan 与运行用同一个可读 ID 时显式给它；同名已存在会被拦下
.venv/bin/python -m pipeline run --suite evals/suites/routing_pdf_v1.yaml \
  --execution-id round-01 --confirm
```

suite 的 `pipeline:` 目前只有安全的只评测模式：

```yaml
pipeline:
  mode: evaluate_only  # 只运行 skills 当前指向版本；绝不创建或改写 V2/V3
  iteration: 1         # 用户记录的改进轮次标签；不是推理重复次数
repeats: 3             # 每道题实际执行次数，用来量稳定性
```

**不要把 `iteration` 当自动改进循环。** `--apply` 已经有了（§0.0），但它每一轮都要人显式发起；
没有无人值守的自动迭代。要测 V2/V3，显式写 versions/cfg suite；要多跑同一条件，用 `repeats`。

`workflows.compare_runs` 现在除了质量指标，也比较 `time_seconds`、`tokens`、
`tool_calls` 和 `errors` 的平均值及基线差值；同模型 + 同 cfg 的多次执行以 execution ID
区分列名，不会再撞列。mock 验证归档与对比已通过；没有真实模型/OpenClaw 调用。

**验证基线：182 passed**（当时；现为 257，见 §0.0）。

---

## 0. 当前阶段：在开发 eval 系统，不是在评估 skill

**这条决定了每次该跑多少题。搞错方向会白烧 token 和时间。**

| | 现在（开发系统） | 以后（真实评估） |
| --- | --- | --- |
| 目的 | 验证链路/指标/对照机制**有没有效** | 判断 skill 好不好、该不该发版 |
| 题量 | **10–20 题就够** | AGENTS.md §25.3 的 20 skill × 100 题 |
| 迭代 | 改一版跑一次，循环要**快** | 一次跑完，慢没关系 |

**别一次写 100+ 道题。** 10 题里凑齐 pos/amb/rej 几类、能把问题暴露出来就够了 ——
`ctxweave_10_v1.0.jsonl` 就是 10 题，已经足够跑出三级阶梯并定位到 V1 描述的具体缺陷。
题目**质量 > 数量**：一道能让模型露馅的边界题，顶二十道正向题。

> 遗留：`routing_all_v1.1`（50 题）是这条约定之前建的，按新约定它偏大了。
> 要继续用它做 pdf 对照可以，但新实验请照 10 题的规格来。

---

## 1. 模型约定

| 用途 | 模型 id | 端点 |
| --- | --- | --- |
| **默认** | `qwen3.7-max-2026-05-17` | DashScope 兼容端点 |
| **切换对照** | `glm-5.1` | **同一个** DashScope 端点 |

两个模型**共用 `DASHSCOPE_BASE_URL` + `DASHSCOPE_API_KEY`** —— 换模型只改 suite 里
`model:` 一行，不碰 `.env`、不换 provider、不用新 key。所有 suite 的默认模型已切完。

实测可用的模型名：`qwen3.7-max-2026-05-17` ✅ ｜ `glm-5.1` ✅ ｜ `qwen3-max` ✅ ｜ `glm-4-plus` ❌

---

## 2. 三十秒验证环境没坏

```bash
cd /path/to/skillEval
.venv/bin/python -m pipeline run --suite evals/suites/routing_baseline.yaml --mock --confirm
```

看到 `ok_runs=150/150` 和一张指标表就说明链路通（mock 不花钱、不调 API）。

---

## 3. 能力现状

| 能力 | 状态 |
| --- | --- |
| 路由 eval（routing-only） | ✅ |
| 有skill / 无skill / V1 / V2 对照 | ✅ 已实测三级阶梯 |
| 多模型切换 | ✅ 已实测 qwen vs glm |
| Suite 严格校验 | ✅ Pydantic strict；未知字段/类型/gate/secret/重复 ID 运行前拒绝 |
| 确定性任务矩阵 | ✅ model × case × repeat；唯一 request/session；重跑会话隔离 |
| 多 runtime（工厂可插拔） | ✅ litellm / mock / **openclaw（已打通，healthy=✓）** |
| 维度评分 + 发布门槛 | ✅ |
| 跨 run delta + 污染检测 | ✅ `workflows/compare_runs.py`；质量 + 平均用时/token/tool/error 差值 |
| 配置指纹（per-model 粒度） | ✅ |
| Pipeline 跑前预检 + 不可覆盖归档 | ✅ suite/skill/dataset/环境/模型参数预览；每次保存实际输入副本 |
| 自动化测试 | ✅ 182 passed（本轮全量） |
| 生产上下文路由 | ✅ role + 长上下文 + 历史消息 + builtin/MCP tool 目录；模糊末句 mock 全链路已通 |
| Full eval 管道 | ✅ 注入 skill → 加载 → 调 tool → 产物归一化，四环已通 |
| 产物归一化 | ✅ `tool_calls`/`artifacts`(带 sha256)/`loaded_skills`/`resolved_model` |
| N4 Environment Backend | ✅ 从 runtime 外提注册表；local / docker 都已接编排并真实跑通 |
| N4 workspace 隔离 | ✅ **每个请求一个独立临时 workspace**（§11.2 isolated、§13.6 repeat 间零泄漏） |
| Full eval 确定性评分 | ✅ `workflows/score_full.py`：任务完成度 / 产物命中率 / tool 命中率，零 LLM 判定 |
| 错误分类 | ✅ `error_kind` 四类（task/runtime/network/harness），四类均已真实构造验证 |
| 单 case 失败不中断整批 | ✅ 编排层兜底，失败按 kind 分类计数 |
| Full eval **对照数字** | ✅ 见 §4.3 |
| 自动出题 | ✅ P2：`workflows/gen_cases.py` + 坏题校验 + 人审门；真实自动集复现 none<v1<v2、FAIL/FAIL/PASS |
| 改进闭环 | ✅ P3 链路已跑通（§0.0）：full/routing 失败证据 → 聚类建议 → `--apply` 写 v(N+1) + 复验 suite → 同题复跑 → compare 无污染。剩停止条件记录与 pdf 能力基准 |
| **跨 repeat 方差 / 效率维度** | ✅ `workflows/metrics.py`：mean ± stddev、time/tokens/tool_calls/errors、flaky 题诊断 |
| **语义 Judge（可换尺子）** | ✅ `workflows/grade.py` 判 `expect_assertions` + 维度分，judge 模型/端点/key **独立于被测模型**；输入文件原文与文本产物内容都进 judge（§0.0）。已在 3 把尺子上实跑对比 |
| **标准语义维度** | ✅ `workflows/dimensions.py` 六个内置维度（faithfulness/completeness/relevancy/instruction_following/correctness/conciseness），0–1 连续分 + 证据，**跨题通用不用逐题写**；判不了的记 N/A。已在真实 full run 上出数 |
| Judge 校准（§22.6） | ❌ 未做，且已实测出后果：qwen-plus 在 loop-r1 上判错一条断言（§0.0）。judge 分**默认一律不进 gate**，只出数不判生死。校准是接手第一件事（§7 ①） |
| Evaluator 注册表 | ❌ P4 剩下的部分，路由/full 的评分逻辑仍写死在两个 score 脚本里 |
| 多轮对话 | ❌ 契约、session 隔离与命名就位，turn 循环未接（P5） |
| Docker 隔离 / 网络策略 / 资源上限 | ✅ **端到端通**：固定镜像（`environments/openclaw.Dockerfile`）、逐 request 容器、只读 skill mount、workspace 回传、凭据按变量名注入、disabled/full、CPU/内存、清理。healthcheck 真进容器探。`evals/suites/full_deliverable_v1_docker.yaml`。剩 mock/allowlist 网络模式未做 |
| 全过程 Viewer + 开箱一条命令 | ❌ P7，现在只有单 run 的 `report.html` |

---

## 3.1 2026-07-28：生产上下文路由 + 提前启动 P6

路由现在明确分成两级，由 `adapters/routing_inputs/` 工厂切换：

1. `direct`（默认）：只给 metadata + 当前问题，先把 description/triggers 调好；
2. `production_context`：第一阶段达标后，再叠加真实上下文压力。

第二阶段的 `RoutingCase.context` / `InvocationRequest.context` 能携带：

* 生产 role prompt 与长上下文；
* 最终用户话之前的 system/user/assistant/tool 历史；
* 只读的 builtin/MCP tool 描述和 input schema。

LiteLLM runtime 不复制；suite 用 `routing_input.strategy/options` 选择输入工厂。
`production_context` 保留消息边界，并明确要求从整段会话恢复未完成意图。
`routing_contextual_v1.0.jsonl` 有 6 道合成题，最后一句均不含
skill 关键词；mock 6 × 3 已跑通。真实 DashScope 调用因会把仓库 skill metadata 外发，
权限审查要求用户再次明确批准，**当前没有真实数字，不要把 mock 18/18 当模型效果**。

P6 按用户要求提前：新增 `environments/` 注册表，编排变为
`environment.prepared(request) → runtime.run(prepared_request) → RunResult`，Evaluator
边界不变。真实 Docker smoke 已验证：固定 digest、`network=disabled`、workspace 可写、
`/skills` 只读、退出容器清理；通用 backend 已可用，但还没有一张含 OpenClaw CLI/auth
的固定镜像，因此不能宣称 Docker 内 OpenClaw full eval 已完成。

---

## 4. 已跑出的结论（别重复劳动）

### 4.1 ctxweave skill：三级阶梯 + 模型差异被 V2 抹平

10 题 × 3 次，两个模型：

| skillcfg | qwen exact | glm exact | 说明 |
| --- | --- | --- | --- |
| none | 70.0% | 60.0% | 目标 skill 缺席，两模型都硬套邻居（glm 正向题 0%） |
| v1 | 83.3% | 90.0% | 描述含糊时两模型表现**不一致**，方向还相反 |
| v2 | **100.0%** | **100.0%** | **描述写清楚后模型差异消失** |

**最有价值的一条结论：skill 描述写得好不好，比换哪个模型影响更大。**

V2 只改了 frontmatter（description 从营销文案改成"什么时候该用它"、补 7 triggers + 4 exclusions），
**正文一字未动**。

### 4.2 pdf V2：10 题专项干净对照已完成

新建了开发期小集：`evals/datasets/routing_pdf_v1.0.jsonl`（10 题 × 3 次）。
专门压 PDF 内容分析边界：用户明确说“PDF 合同/审计报告/论文”，但任务是风险判断、
业务分析、观点总结，按本项目定义不应激活 pdf skill。

| cfg | exact | no-skill rejection | false activation | gate |
| --- | ---: | ---: | ---: | --- |
| pdf-v1 | 70.0% | 0.0% | 100.0% | FAIL |
| pdf-v2 | 100.0% | 100.0% | 0.0% | PASS |

`workflows/compare_runs.py` 没报污染项，只显示 `[对比维度] suite_id/skillcfg`。

改法：把激活条件从**文件是什么格式**改写成**要对文件做什么操作**，
exclusions 里点掉"分析内容/风险判断/咨询建议"。

V1 的 9 次拒答题全部误激活到 `pdf`，理由都在按 PDF 文件格式联想；V2 同题 9/9 正确拒答。

---

## 5. 三条不能破的规矩

违反了系统照样跑，但**结果会变得不可信，且很难发现**。

**① 会改变结果的东西，必须能被 `config_hash` 捕获。**
现在 hash = suite(剔除 models) + 本 run 的那一个 model + mock + runtime fingerprint
+ skill 内容 hash + **dataset 内容 hash**。
→ 脚本里不留可调参数，要改就改 suite；adapter 内部常量在 `fingerprint()` 里自报。
→ 这条踩过五次坑（system prompt、mock、装新 skill、models 粒度、dataset 内容），每次都是"结果变了但 hash 没变"
   或"结果没变但 hash 变了"。

**②密钥只写变量名，不写值。** suite 进 git，`.env` 不进。

> 判分模型（`scoring.judge`）是这条规矩的一个特例，**故意不进 `config_hash`**：
> 它跑在 run 之后，换它一个字节都不改变 `runs.jsonl`。让它进 hash 会造成
> 「改了判分模型 → 历史 run 看起来不可比」，可两边的原始运行完全一样。
> 它真正影响的只有 `assertion_pass_rate` 一个维度 —— 追溯走 `scores.json` 的
> `judge` 字段，跨 run 比较时 `workflows/compare_runs.py` 报 `[⚠️ 尺子不同]` 并在那一行打标。
> **judge 的凭据也单独放**（默认 `JUDGE_BASE_URL` / `JUDGE_API_KEY`）：
> 拿被测模型给自己判分等于让考生改自己的卷子。

**③ 做对照时，除目标那一项外 suite 必须逐字相同。**
`workflows/compare_runs.py` 会把差异分成 `[对比维度]` / `[随之联动]` / `[⚠️ 污染]`，
出现污染项就说明 delta 混了多个原因 —— 别信那个数（§4.2 就是活例子）。

> ⚠️ 一个已知的判定盲区：`dataset` / `dataset_hash` 被归在 `[随之联动]` 里，
> 因为**路由**的 No-Skill 基线本来就要换一个 gold 不同的数据集（`..._none.jsonl`）。
> 但 **full eval 的 none 与 v1 用的是同一份数据集** —— 那里出现 dataset 差异就是污染，
> 工具却不会标红。所以看到 `[随之联动] dataset` 时先问一句：这次对照该换数据集吗？
> 不该换却换了，多半是拿两个不同版本的题在比（例如 v1.0 的旧 run 和 v1.1 的新 run）。
> 已把被取代的旧目录改名成 `outputs/SUPERSEDED_*`，`outputs/effect_*` 的通配符就扫不到它们。

---

## 6. 坑表（都已修，别再引入）

| 坑 | 现象 | 防护 |
| --- | --- | --- |
| **pandas 同名列静默覆盖** | compare 的 delta 列全错；换模型比较时表直接空 | 列名用 `model/skillcfg`，delta 列独立命名 `Δ(...)`。**咬过两次，第三次还会咬** |
| config_hash 粒度过粗 | 改 glm 端点作废了 qwen 的历史结果 | hash 改成 per-model |
| dataset 内容没进 hash | 改了题但文件名不变，历史结果看起来还能比 | `workflows/run_routing.py` 计算 `dataset_hash`，写入 snapshot 并纳入 `config_hash` |
| 装新 skill 污染旧基准 | `skills/` 多一个 skill，所有 suite 的 catalog 都变 | skill hash 进 config_hash；老 suite 显式 `exclude` |
| skill 目录名 ≠ skill_id | ctxweave 目录名和 `name:` 字段不同 | loader 一律以 frontmatter 的 `name` 为准 |
| No-Skill 基线 gold 不同 | 目标 skill 缺席时正向题应改判"拒答" | 两个数据集文件：`..._v1.0.jsonl` / `..._v1.0_none.jsonl` |
| **⚠️ 一整类：静默变空** | 不崩溃、不报错，只是某个列表恒为空 —— 看起来像"模型没做事"，实为"我们没读到"。**凡是某列表恒空，先怀疑自己的解析/过滤** | 见下三行 |
| openclaw 输出格式换代 | `selected_skills` 全空（模型其实答对了） | `_extract_text()` 兼容 2026.7 的 `payloads[]` 和旧的顶层 `text`。**升级后结果突变空先查这里** |
| artifact 恒为空 | workspace 住在 `~/.openclaw/` 下，用绝对路径 `f.parts` 排除 `.openclaw` 会把整个 workspace 滤光 | `_snapshot()` 改成按**相对 root** 的路径判断 |
| 软链注入 skill 不生效 | `loaded_skills` 纹丝不动，只在 openclaw 日志里留一行 | OpenClaw 默认跳过解析到 root 外的软链；改用**复制到 staging + `extraDirs`** |
| **healthcheck 过了但每题必挂** | `Invalid session ID` —— matrix 的 `execution_id` 带时区偏移 `+0200`，OpenClaw 不收 `+`；而 healthcheck 用的是写死的 `skilleval-health`，探得通。**探针必须用真实格式的 id** | adapter 里 `_safe_session_id()` 收敛字符集（§17.3：runtime 的字符集约束不该反向绑架命名规范） |
| full 模式 artifact 里混进脚手架 | 每个 run 平白多出 8 个 artifact，产物命中率的分母是假的 | OpenClaw 进空 workspace 会自铺 `AGENTS.md`/`SOUL.md` 等；`_snapshot()` 按根目录文件名排除 |
| **断网被记成「评测系统崩了」** | litellm 把连不上上游包装成 `InternalServerError`，类名里一个网络词都没有 | `classify_error()` 先按**来源模块**判（litellm/openai/httpx…），类名只作兜底 |
| openclaw 找不到 node | bin 是 `env node` 脚本，给绝对路径也没用 | adapter 的 `node_bin` 选项。**本机 openclaw 装在 node v24 下、默认 PATH 是 v22，必须配** |
| openclaw 刷 AWS 报错 | 它会读 `~/.aws/credentials` 探 Bedrock | adapter 把 AWS 配置路径指向 `/dev/null` |
| openclaw 配置只配了 workspace | `configure` 选 Workspace 不配任何凭据，配完仍 `missing-provider-auth` | 用 `onboard --non-interactive`，见 [OPENCLAW.md](OPENCLAW.md) §0 |
| 测 nvm 版本测不准 | `zsh -l -c` 不读 .zshrc；`zsh -i -c` 继承父 PATH | 必须 `env -i` 才等价于新终端 |

---

## 7. 下一步

**最终验收标准（六条）+ 每阶段的交付/验收/明确不做 → [AGENTS.md](AGENTS.md) §★★★。**
那一节是排优先级的唯一依据；§27 的 Milestone 顺序已过期，别照着推。

阶段速览（P0/P1/P2 = 已完成）：

| 阶段 | 干什么 | 为什么排这儿 |
| --- | --- | --- |
| **P1** | Full eval 第一个对照数字 | ✅ none/v1 对照、错误分类、不中断整批均已验收 |
| **P2** | 自动出题 v0 | ✅ 自动生成→人审→三级真实复验已完成 |
| **P3** | 改进闭环 v0 | ✅ 链路已跑通（§0.0）：full/routing 失败证据 → 建议 → `--apply` 写 v(N+1) → 同题复验 → compare。剩停止条件记录 + 能力基准 |
| **P4** | Evaluator 注册表 + Judge | 等第三类评分逻辑出现再抽；**只有一个实现的接口是负债** |
| **P5** | 多轮编排 | 契约就位，纯编排工作，不阻塞别人 |
| **P6** | Docker 环境后端 | ✅ 已提前做完：容器里跑 agent，healthcheck→mount→凭据→清理全链路验收 |
| **P7** | 全过程 Viewer + 开箱 | 最后做，否则要跟着前面反复改 |

### 接手就做这三件（按顺序，2026-07-29 定）

闭环本身已经通了，卡住结论质量的不是链路，是**尺子**。所以顺序是先校准尺子，再谈自动化。

**① judge 校准（absolute assertion 小集已完成）**

§0.0 实测：同一批 run、同一批题，换个 judge 模型 `faithfulness` 0.67→0.83、
`instruction_following` 0.67→0.93，而 qwen-plus 在一条断言上**看着输入原文仍然判错**
（文字稿里明明有「赵磊负责」）。judge 判错 → suggest 照着假失败改 skill → 整个闭环空转。

- 已完成：版本化 gold + 可重复 CLI + 结构化结果；qwen3.7-max 100%，qwen-plus 90%。
- 策略：此类题默认 qwen3.7-max；qwen-plus 虽过 80% 门槛，但漏判“张付总→张副总”，
  且曾给出关于赵磊的错误证据，不作为首选。
- 仍不做：标准语义维度校准完成之前，不把 judge 分接进 gate。

**② 迭代停止条件（已完成）**

`suggestions.json` 已记录 gate PASS / 累计 token+墙钟预算 / 最大迭代数，
`--previous-report` 严格继承 lineage 与上限；同时命中会全部记录，主原因按
gate → budget → max 排序。
**仍然不做无人值守循环** —— 停止条件是给人看的刹车，不是给机器踩的油门。

**③ P3 能力基准（验收①的最后一条）**

拿 pdf V1 当输入，看系统能否自己提出「按任务类型而非文件格式激活」这条
（§★★ 里它是人工得出的结论）。这一条通过，才算"系统真的能告诉你改哪"，
而不是"能把 judge 的话复述一遍"。
外发前提：用户需明确允许把 pdf V1 的 SKILL.md 与失败 run 原文发到 DashScope ——
此前的 ctxweave/P2 外发授权不自动扩张到这批数据，权限审查已正确拦下；不要绕过。
dry-run manifest 已验证为 9 条失败、3 个 case、3868 字符 prompt；当前仍停在授权门前。

> 顺带：`subjects/meeting-and-brief/v3/` 是闭环第一次跑通的产物，但它基于①里那条假失败，
> **不要当 V3 用**（`PROVENANCE.md` 已写明）。尺子校准好之后，从 loop-r1 重新出一版才算数。

P2 最终数字（自动集 v1.1，qwen，10 题 × 3）：none 56.7% FAIL →
V1 80.0% FAIL → V2 100.0% PASS，与人工集排序和 gate 一致。

顺手能做、不占阶段的一件事：**OpenClaw vs litellm 的路由对照** —— 同一套题、同一个 skill 版本，
一边走 agent loop 一边走纯 metadata 推理，看 delta。归因前先看 §5③。

**一个已知遗留（docker 模式下已解决）**：OpenClaw 侧的模型选择（`openclaw models set`）
在 OpenClaw 自己的配置里，**不进 skillEval 的 `config_hash`**。做 OpenClaw 模型对照时，
要在 suite 的 `models[].id` 里写清楚跑的是哪个（它进目录名）。事后追溯看
`RunResult.resolved_model`。
跑 docker 时不受这条限制：容器 profile 每次都是新的，模型必须由
`runtime_options.model` 显式指定，而它**进 fingerprint**，所以模型选择是可追溯的。

**~~第二个已知遗留 —— judge 看不到产物内容~~ 已修（2026-07-29，§0.0）**：
`Artifact.text_excerpt` 在采集时留下文本产物的内容前缀（4000 字符），
case 声明的输入文件原文也一并进 judge prompt。**仍然看不到的**：二进制产物
（docx/png）、超出截断的部分 —— 这两种情况 judge 按"产物内容不可见"判 failed，偏保守。
真要留全档（P7 Viewer 要展示原始产物时）再按 AUTHORING.md §1.4 拷到
`outputs/{run}/artifacts/{case_id}-r{repeat}/`。

**改代码的落点**：一律写在 skillEval。真要改 OpenClaw，不 fork 也不直接改源码 ——
补丁写进 [OPENCLAW.md](OPENCLAW.md) §9 的改动登记（AGENTS.md §29 规则 23）。

---

## 8. 文件地图

```
README.md      怎么用 + 五步抽象设计
AGENTS.md      架构规范（北极星）+ ★★实现状态（当前真相，含阶段约定与默认模型）
OPENCLAW.md    OpenClaw runtime 接入手册（从哪下 / 怎么接 env / 坑 / 换机器迁移清单）
DEVLOG.md      开发记录，倒序，含每次踩的坑
HANDOFF.md     本文

pipeline/            统一跑前预检 + 显式确认入口（`python -m pipeline`）
  plan.py             suite 展开、skill/dataset hash、运行矩阵、目标归档、健康检查
workflows/            全部运行/评分入口（`python -m workflows.<name>`）
contracts/           N0 契约：skill / evalcase / runtime（含 error_kind 四类分类）
  suite.py           N2 严格 Suite Contract + 规范化配置
workflows/matrix.py            N3 确定性任务矩阵 + request/session 身份
adapters/runtimes/   N5 执行环境：base(Protocol) + __init__(工厂) + litellm/openclaw/mock
workflows/run_routing.py       编排层（只编排，不含调用逻辑）；routing 与 full 共用
workflows/score_routing.py     路由打分 + 发布门槛
workflows/score_full.py        full 打分：产物/tool/skill 加载的确定性断言 + 门槛
workflows/metrics.py           跨 repeat 方差 + 效率维度 + flaky/非判别性诊断（两个 score 共用）
                     算法搬自 Anthropic skill-creator 的 aggregate_benchmark.py
workflows/dimensions.py        标准语义维度的 rubric 与评分锚点；每条注明抄自 RAGAS/G-Eval/autoevals
                     的哪个定义。改 rubric 必须 bump 该维度 version（进判定产物）
workflows/grade.py             语义判定：judge 打维度分（0–1）+ 判 expect_assertions，
                     产 grading.{judge}.json。**judge 与被测模型解耦**：独立
                     model/端点/key，可换尺子重判。`--list-dimensions` 查全部维度
workflows/calibrate_judge.py   离线对齐 versioned human gold 与 grading.*.json，
                     出 agreement / invalid rate；不调用模型
workflows/compare_runs.py      跨 run delta + 污染检测（路由与 full 指标通吃）+ 尺子差异检测

evals/AUTHORING.md   怎么写题、命名、版本
evals/RUNBOOK.md     配置存哪、怎么跑、怎么比
evals/CASEGEN.md     自动出题与改进建议的人审规范
workflows/gen_cases.py         P2 薄入口：metadata + 验收标准 → DRAFT dataset/suite；不自动运行
workflows/suggest.py           P3：失败证据（routing/full）→ 聚类建议 JSON；--apply 才写 v(N+1) + 复验 suite
evals/datasets/      问题集（答案内联）
evals/suites/        配置（一个 suite = 一次可复现实验）
  ctxweave_{none,v1,v2}.yaml    三级对照，双模型
  routing_baseline{,_v2}.yaml   6-skill 基准 + pdf V2
  routing_pdf_{v1,v2}.yaml      pdf 10 题专项干净对照
  routing_openclaw.yaml         OpenClaw runtime（已打通）
  full_deliverable_{none,v1}.yaml   full eval 对照，只差 skillcfg
subjects/<id>/<vN>/  被测 skill 的版本库：一个 skill 一个目录，每一版是它下面的
                     完整一份（SKILL.md + references/ + scripts/）。有 v1 和 v2 的：
                     pdf、meeting-and-brief、humanizer、interactive-architecture-diagram
skills/              skillhub 的下载区，**不是被测源**，已 gitignore；装完复制成
                     subjects/<skill-id>/v1/ 才进评测（§7.1）
outputs/{group}/{execution_id}/  不可覆盖执行归档：config snapshot + inputs/ + runs + scores/report
                     + grading.{judge_id}.json（跑过 workflows/grade.py 才有；按 judge 分文件）
.env                 仅密钥，不进 git。judge 的凭据单独放 JUDGE_BASE_URL / JUDGE_API_KEY
```
