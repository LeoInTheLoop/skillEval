# AGENTS.md

## 1. 项目定位

本项目构建一个面向 Agent Skill 的标准化运行、测试与评估trajectory环境。

核心目标：

1. 不修改 OpenClaw 核心代码，通过外层配置控制运行环境。
2. 将 Skill、Tool、模型、文件、网络、会话、记忆和审批封装为标准配置。
3. 将不同运行环境的结果统一转换为标准 `RunResult`。
4. 支持多 Skill 路由测试。
5. 支持 No-Skill、Skill V1、Skill V2 对照。
6. 支持多模型 API。
7. 支持单轮和多轮对话。
8. 支持实际业务运行与测试评估共用同一执行层。
9. 评测能力采用可插拔组装方式。
10. 优先复用成熟库，不重复实现已有基础能力。

核心抽象：

```text
ExperimentSpec
      ↓
InvocationRequest
      ↓
RuntimeAdapter
      ↓
OpenClaw Runtime
      ↓
Normalized RunResult
      ↓
Evaluator Pipeline
      ↓
EvaluationResult
      ↓
Regression Report
```

---
参考最多的
claudecode skill-creator
agentic RL openclawrl 设计


## ★ 最薄切片：先跑通路由 eval（Walking Skeleton）

> 本节是**第一件要做的事,不是第 11 件**。目标:3 天内跑出一个能驱动决策的真实数字——"我的 skill 描述/触发词写得好不好"。下面 N0–N11 的完整架构是北极星,但**不要按 Milestone 0→10 瀑布推进**,先把这条最薄的端到端链路跑通,验证这套东西真的有用,再按需长出其余节点。

### 为什么是路由

* 路由(模型在 N 个 skill 里选对没有)是改 skill 时**每天都要回答**的问题,也是现成工具最缺的一块。
* 路由**只需要 skill 的 metadata + 一次模型调用**:不需要 Docker、不需要 OpenClaw、不需要 workspace / artifact / 多轮 / 网络 / 审批。所以它出结果最快。
* OpenClaw 在这一步**先不接**:路由是纯 metadata 推理,直接用 LiteLLM 调模型即可。OpenClaw 留到 Full Skill Eval(真正加载并执行 skill)那一步再进,见 §4.1。

### 这一层刻意不做（全部推迟）

Docker 隔离、`OpenClawRuntimeAdapter`、workspace、artifact、多轮、Persistent Memory、Webhook、审批、网络四模式、OTel、Streamlit UI、V1/V2 完整回归 Gate——**一个都不做**。跑通路由之后再按需补。

### 文件（全部，就这些）

> ⚠️ 下面是骨架期的**原始设想**。实际布局已经演进（测试集/配置/结果都做了版本化命名，
> 模型与门槛从 `.env` 和脚本参数上收到 suite YAML）。**以实现为准**：
> 用法见 [README.md](README.md)，写题见 [evals/AUTHORING.md](evals/AUTHORING.md)，
> 配置与产物组织见 [evals/RUNBOOK.md](evals/RUNBOOK.md)。

```text
skill-eval/
├── subjects/                   # 被测 skill 的版本库,路由只读 metadata(frontmatter)
│   ├── pdf/v1/SKILL.md
│   ├── pdf/v2/SKILL.md
│   └── ...
├── evals/routing.jsonl         # 每行一个 case:{prompt, expected_skills, tags, severity}
├── contracts.py                # 3 个 Pydantic 模型:SkillMeta / RoutingCase / RoutingRun
├── workflows/run_routing.py              # 读 metadata → LiteLLM 调用 → 解析选择 → 存 runs.jsonl
├── workflows/score_routing.py            # sklearn 算 Top-1 / 误激活 / No-Skill 拒答 / 混淆矩阵
├── outputs/                    # runs.jsonl + report.html
└── .env                        # 模型 key
```

### 端到端链路（就 4 步）

1. **读 metadata(routing-only)**:从 `subjects/*/*/SKILL.md` 的 frontmatter 只读 `name/description/triggers`,**不读正文**。这就是 §7.3 的 routing-only 模式,骨架里等于"只 parse frontmatter"。
2. **调模型**:每个 case 拼 prompt =「这是 N 个 skill 的 metadata + 用户问题,选出适用的 skill(可为空),返回 JSON」,用 **LiteLLM** 调用(一行覆盖 Claude/GPT/本地),用 Pydantic 校验结构化输出。每题跑 3 次。
3. **打分**:用 **scikit-learn** 算 Top-1 准确率、误激活率、No-Skill 拒答率、混淆矩阵——**不手写指标**。
4. **看结果**:pandas 打一张表 + 存 HTML。

### 三天节奏

* **Day 1**:`contracts.py`(3 个模型)+ 3 个 skill 的 metadata + 手写 20 条 routing case(含几条 No-Skill)。`workflows/run_routing.py` 跑通单模型,把原始选择打出来。
* **Day 2**:`workflows/score_routing.py` 用 sklearn 出 Top-1 + 混淆矩阵;加第 2 个模型;每题重复 3 次。
* **Day 3**:出 HTML 表;扩到 ~15–20 skill / ~60 题;**把同一批题在 skill 描述 V1 与 V2 上各跑一遍看 delta**——这就是第一个能驱动"改不改描述"的真实数字。

### Day-3 产出长这样

```text
Skill routing — 18 skills × 60 cases × 3 runs — model=claude-opus-4-8
Top-1 accuracy        87%
No-Skill rejection    82%
False activation       6%
最易混淆:  document↔pdf (11)   report↔slides (7)
→ 结论:document/pdf 的 description 边界要重写;report 触发词太宽
```

### 骨架里直接复用什么（不自己写）

| 步骤 | 直接调 | 你写的 |
| --- | --- | --- |
| 多模型调用 | LiteLLM `completion()` | 0 行,只调 |
| 结构化输出校验 | Pydantic v2 + JSON Schema | 3 个模型类 |
| 路由指标 / 混淆矩阵 | scikit-learn `confusion_matrix` / `classification_report` | 0 行 |
| SKILL.md 解析 | python-frontmatter | 一个 loader |
| 出表 / HTML | pandas `.to_html()` | 一个 print |

> 更懒的探路法:**promptfoo** 能用一个 YAML 把"多 prompt × 多 provider × 断言选对 skill"直接跑起来,连 `workflows/run_routing.py` 都省。但 No-Skill 拒答率、混淆矩阵这类 skill 专属指标用上面这条薄 Python 链更顺手——**先用 promptfoo 探路,指标不够再落成脚本**。

---

## ★★ 实现状态（截至 2026-07-28）

> 本节是**实现的真相**,与上面的骨架设想有出入时以本节为准。用法见 [README.md](README.md)。

### ⚠️ 当前阶段：在开发 eval 系统本身，不是在评估 skill

这条决定了每次该跑多少题、写多少题。**搞错方向会白烧 token 和时间。**

| | 现在（开发系统） | 以后（真实评估） |
| --- | --- | --- |
| 目的 | 验证链路/指标/对照机制**有没有效** | 判断某个 skill 好不好、该不该发版 |
| 题量 | **10–20 题就够** | §25.3 的 20 skill × 100 题 × 3 次 |
| 关注 | 系统跑得通吗?指标算得对吗?delta 归因得了吗? | skill 本身的准确率/回归 |
| 迭代 | 改一版跑一次,循环要**快** | 一次跑完,慢没关系 |

**规则:开发期一次不要写 100+ 道题。** §25.3 的基准规模是北极星,不是现在的作业。
10 题里凑齐 pos / amb / rej 几类、能把问题暴露出来,就达到目的了 ——
题目**质量 > 数量**,一道能让模型露馅的边界题,顶二十道正向题。

参考:`evals/datasets/ctxweave_10_v1.0.jsonl` 就是 10 题版本,已经足够跑出
none/v1/v2 三级阶梯并定位到 V1 描述的具体缺陷。

### 默认模型

| 用途 | 模型 | 端点 |
| --- | --- | --- |
| **默认** | `qwen3.7-max-2026-05-17` | DashScope 兼容端点 |
| **切换对照** | `glm-5.1` | 同一个 DashScope 端点 |

两个模型走**同一个 base_url、同一把 key**(`DASHSCOPE_*`),换模型只改 suite 里的
`model:` 字段,不用碰 `.env`、不用换 provider。

### 已完成

| 节点 | 状态 | 落在哪 |
| --- | --- | --- |
| N0 Contract | ✅ Skill/EvalCase/`InvocationRequest`/`RunResult`/Health/Capabilities,Pydantic 严格模式 | `contracts/` |
| N1 Skill Registry(routing-only) | ✅ 只 parse frontmatter,算 content_hash,不读正文；路由输入分两级工厂：`direct` 先调 metadata，`production_context` 再叠加 role/长上下文/history/tool/MCP | `contracts/skill.py`、`adapters/routing_inputs/` |
| N2 Experiment Config | ✅ **严格 Suite Contract + YAML**：字段/类型/枚举/重复 ID/gate/secret 引用在执行前校验；`skills.target` 显式声明实验归属、与候选 catalog 分离；规范化配置用于 hash 与快照 | `contracts/suite.py`、`evals/suites/*.yaml` |
| N3 Matrix Builder | ⚠️ 已抽出确定性的 `model × case × repeat` 矩阵、唯一 request/session、重跑会话隔离和 10k 任务测试；skill 配置/版本/runtime 尚未在单 suite 内做多轴展开 | `workflows/matrix.py` |
| N4 Environment Resolver | 🚧 已从 Runtime 中拆出 `EnvironmentBackend` 注册表；local 每 request 独立 workspace/skill staging，docker 固定 digest、独立容器、只读 skill mount、disabled/full 网络、CPU/内存和清理已实现并真实 smoke；mock/allowlist、输入只读挂载、OpenClaw 固定镜像仍缺 | `environments/`、`contracts/runtime.py` |
| N5 Runtime Adapter | ✅ **Protocol + 注册表工厂**,能力校验 + healthcheck + fingerprint + 环境布置 | `adapters/runtimes/` |
| N6 OpenClaw Execution | ✅ **已打通**(healthy=✓);routing 与 full 均实测:注入 6/6 加载、tool 真调用、产物落盘 | `adapters/runtimes/openclaw.py`、[OPENCLAW.md](OPENCLAW.md) |
| N7 Result Normalizer | ⚠️ 统一 `RunResult`(含 `tool_calls`/`artifacts`/`resolved_model`/**`error_kind` 四类失败归属**) + JSONL + config 快照,未接 OTel | `contracts/runtime.py`、`outputs/{run}/` |
| N8/N9 路由指标 | ✅ Exact-Set-Match/Top-1/multi/拒答率/误激活/分题型/PRF/混淆矩阵,全走 sklearn | `workflows/score_routing.py` |
| N8/N9 full 指标 | ✅ 任务完成度/产物命中率(存在+非空+MIME)/tool 命中率/skill 加载,**全是确定性断言,零 LLM 判定**;不适用的维度记 `N/A` 不记 0 | `workflows/score_full.py` |
| N8/N9 稳定性与效率维度 | ✅ 跨 repeat `mean ± stddev`/min/max、time/tokens/tool_calls/errors、flaky 与非判别性题诊断;算法搬自 skill-creator 的 `aggregate_benchmark.py`(§4.2) | `workflows/metrics.py` |
| N8/N9 语义 Judge | 🚧 `expect_assertions` 逐条判 pass/fail + 原文证据,**judge 模型/端点/key 与被测模型完全解耦**(默认独立 `JUDGE_*` env),按 judge id 分文件互不覆盖;judge **不进 `config_hash`**(跑在 run 之后,不改变 `runs.jsonl`),跨 run 比较由 `compare_runs` 报 `[⚠️ 尺子不同]`。case 输入原文与文本产物 `text_excerpt` 已可见；二进制/截断外内容仍按不可见处理 | `workflows/grade.py`、`contracts/suite.py` |
| N8/N9 标准语义维度 | 🚧 六个内置维度(faithfulness/completeness/relevancy/instruction_following/correctness/conciseness),0–1 连续分 + 证据 + rubric 版本;**跨题通用,不需逐题写**;缺前置条件(如 `correctness` 缺 `reference`)记 N/A 不记 0;维度名拼错在运行前被拒。复用审计见 §20.6 | `workflows/dimensions.py` |
| Judge 校准(§22.6) | 🚧 absolute assertion 小集已校准：10 条人工 gold 上 qwen3.7-max 100%、qwen-plus 90%，invalid 0%；A/B swap 对绝对断言 N/A。标准语义维度与校准注册表仍未做，因此 judge 分默认仍不进 gate | `workflows/calibrate_judge.py`、`evals/calibration/` |
| N10 Regression Gate | ✅ 单 run gate + 跨 run delta + 配置污染检测 + 判分尺子差异检测 | `workflows/score_routing.py`、`workflows/compare_runs.py` |
| N11 Report | ✅ `scores.json` + `report.html` | `workflows/score_routing.py` |

### 相对原设计的三处演进（都是为了「结果可比」）

1. **配置从 `.env` / CLI 参数上收到 suite YAML。**
   骨架期模型写在 `.env`、重复次数硬编码。问题:这些都会改变结果,却不进任何指纹,
   两次跑出不同数字时查不出原因。现在**脚本里不留可调参数**,一切改 suite,
   suite 全文进 `config_hash`。密钥仍在 `.env`,suite 只写变量名(§8.3 要求)。

2. **测试集/结果全部版本化命名。**
   `evals/routing.jsonl` → `evals/datasets/{kind}_{scope}_v{x.y}.jsonl`;
   `outputs/` 扁平 → `outputs/{dataset}__{model}__{skillcfg}/`。
   骨架期第二次跑会覆盖第一次,做不了 V1/V2 对照(§25.4 要求历史版本不得覆盖)。
   命名规范见 [evals/AUTHORING.md](evals/AUTHORING.md) §1。

3. **每个 run 目录自带 `config.snapshot.yaml`。**
   含完整 suite + `config_hash` + dataset content_hash + skill content_hash + runtime 能力与 fingerprint +
   时间戳 + 是否 mock。对应 §6.3 provenance。**`config_hash` 不同 = 结果不可直接比较。**

4. **Runtime 做成 Protocol + 注册表工厂,不是 if/else。**(§17)
   `adapters/runtimes/` 下每个实现用 `@register("名字")` 自注册,编排层只调
   `create_runtime(suite["runtime"])`,拿到的是 Protocol,**不知道背后是 LiteLLM 还是 OpenClaw**。
   配套两件事:
   - `capabilities()` — suite 声明了 runtime 不支持的能力(如让 litellm 跑 full),
     **跑之前**就拒绝,不会跑一半才炸。
   - `fingerprint()` — adapter 内部会改变结果、但不在 suite 里的东西(system prompt、
     CLI 版本)由它自报,一并进 `config_hash`。**这是补上的一个漏洞**:
     否则改一句 prompt 就能悄悄改变结果而指纹不变。

### 第一个能驱动决策的结果（v1.1 题库 / qwen3-max / GATE FAIL）

补了 12 道 amb(边界题)+ 4 道 multi + 4 道 rej 之后,指标终于不再恒为 100%:

```text
Exact skill-set match 92.0%   Top-1 100%   amb 100%   multi 25%   拒答 90%
GATE FAIL — false_activation 10% > 门槛 5%
```

两个可行动的结论:

1. **`none-rej-07`「这份合同里的违约条款有什么风险」被 3/3 一致误路由到 pdf**,
   理由是"合同通常是 PDF 格式"。→ 模型在按**文件类型联想**激活,而非**任务类型**。
   pdf 的 description/exclusions 需要写明"仅当需要对 PDF 做格式操作时激活,纯内容分析不激活"。
2. **multi 题只有 25%** — 模型倾向只选一个 skill。注意这批数据跑的是旧 system prompt
   ("尽量只选最相关的一个"),该 prompt 已改。**旧数据的 `config_hash` 与新的不同,
   两者不可直接比** —— 这正是 fingerprint 进 hash 想保证的事。

### 未完成（下一阶段的入口）

* ~~OpenClaw 凭据~~ — **已打通**(2026-07-28 晚,全程非交互)。配置手册见 [OPENCLAW.md](OPENCLAW.md);
  「必须 TTY」的说法是错的,`onboard --non-interactive` 可用。
* ~~Full eval 归一化~~ — **已完成**(2026-07-28 深夜)。注入→加载→调 tool→产物落盘四环已通,
  `tool_calls`/`artifacts`(带 sha256)/`loaded_skills`/`resolved_model` 均已归一(§18.2)。
* ~~Full eval 的题与评分~~ — **P1 已完成**：none→v1 任务完成度 16.7%→83.3%，
  产物命中 0%→80%，无配置污染。
* ~~自动出题 v0~~ — **P2 已完成**：生成→人审→同题三级复验，
  自动集复现 `none < v1 < v2` 与 `FAIL/FAIL/PASS`。
* ~~改进闭环 v0~~ — **P3 已跑通**（2026-07-29）：full/routing 两种失败证据 → 聚类建议 →
  `--apply` 写 `subjects/<id>/v<N+1>/` + 复验 suite → 同题复跑 → `compare_runs`。
  **仍未做**：迭代停止条件的记录（gate PASS / 预算 / 最大轮数，§24），以及 pdf V1 能力基准。
* Skill 版本对照 — versions/exclude 已可用;OpenClaw 侧的模型选择**不进 config_hash**,
  做 OpenClaw 模型对照时要在 `models[].id` 写清楚(`RunResult.resolved_model` 已能事后追溯)
* N4 的其余部分 — Docker 隔离、输入文件挂载、网络四模式都还没做,
  当前 `prepared()` 只做了 skill 注入
* ~~多轮对话~~ — **P5 已完成（2026-07-29）**：full case 顶层为 turn 1、`turns`
  声明后续轮；同 conversation 共享 session/workspace，repeat 隔离，失败后续轮记
  `skipped`；逐轮/上下文/文件延续指标已接。`parallelism` 按独立 conversation 并发，
  并发完成后仍按确定性矩阵顺序落盘。
* Skill 多版本(V1/V2 并存)— Registry 未支持
* 自动化测试 — 294 项通过、1 项按环境跳过；多轮与并发有独立契约/编排/评分验收，
  adapter/评分层覆盖仍待继续补

---

## ★★★ 最终验收标准与阶段计划（2026-07-28 定）

> ★★ 是「现在是什么」，本节是「做完什么算完成」，差距就是下面的 P1–P7。
> §27 的 Milestone 0–10 是最初的瀑布排期，**已被本节取代**（顺序不同、粒度不同），
> 冲突时以本节为准。

### 六条最终验收标准

#### ① 最小输入 → 自动出题 → 自动改进

用户只提供三样：skill 源目录、**业务目标与验收标准**、必要的输入样例/外部依赖。系统能：

* 解析 skill 的能力、触发条件、排除条件、tool 与产物要求；
* 生成**版本化**的数据集 + suite 配置；
* 覆盖正向 / 边界 / 拒答 / 多 skill / 多轮 / tool / 产物 / 安全中**适用**的类别；
* **运行前**拦下坏题、重复题、答案冲突、配置错误、能力不匹配；
* 跑完给**带证据**的改进建议 —— 每条指到具体 case_id、trace 和评分维度；
* 用户确认后生成新 skill 版本，**用同一套题**复验并出 delta；
* 触到 gate / 预算上限 / 最大迭代数即停。

四条硬约束，否则这一条就是自欺：

* **生成的题必须能被人工审核**，尤其边界题与期望答案。模型生成的 gold 不是真值。
* **自动改写 skill 默认需要用户确认** —— 否则系统会为了过测试而过拟合题库。
* **「多轮对话测试」与「多轮改进迭代」是两件能力**，分开验收（前者 = P5，后者 = P3）。
* 可测判据：同一个 skill 上，自动生成集与人工基准集**给出相同的 gate 结论**；
  不一致必须能解释原因。（现成基准：§★★ 的 ctxweave 三级阶梯、pdf V1/V2）

#### ② 可插拔：Runtime / Evaluator / CaseGenerator / Reporter

「和 Claude Code skill eval 差不多」不能作为验收条件（无法判定「差不多」是多少），换成可测判据：

* 四类组件都通过**稳定接口 + 注册表**扩展，新增实现不改核心编排与既有实现；
* Runtime 统一交付 `run()` / `healthcheck()` / `capabilities()` / `fingerprint()`；
* Evaluator 只消费 `RunResult`，**不得读 OpenClaw 内部对象**（§3.3）；
* **可测判据**：新增一个 runtime 或 evaluator，`git diff --stat` 里**只出现新文件 + 注册表一行**，
  `workflows/run_routing.py` 零改动，既有测试全绿。

> 现状：runtime 侧已满足（`adapters/runtimes/base.py` + `@register`），evaluator / generator / reporter 侧未做。

#### ③ 分层评价：确定性优先，模型只判语义

执行模型与 Judge 模型**分别可配**（不许自己评自己）；能用代码判定的绝不交给 LLM（§3.4）。默认维度：

| 类别 | 维度 |
| --- | --- |
| 路由 | Exact-Set-Match、召回、误激活、No-Skill 拒答、多 skill 选择、重复稳定性 |
| Skill 执行 | 是否加载正确 skill、任务完成度、最终答案正确性 |
| Tool | 选择、顺序、参数、失败率、调用次数 |
| Artifact | 是否生成、格式、Schema、内容、文件 hash |
| 多轮 | session 连续性、上下文保持、文件状态延续、逐轮完成度 |
| 安全 | 权限、越权文件访问、网络策略、审批与敏感信息 |
| 效率 | token、成本、延迟、重试、tool call 数 |
| 可复现性 | config hash、版本、runtime 指纹、模型参数、重复运行方差 |

附加要求：

* 不适用的维度记 **`N/A`，不许按 0 分处理**（否则 gate 被结构性拉低）；
* Judge 的模型、prompt、版本、参数必须进配置快照与 `config_hash`；
* Judge 输出必须带评分依据和对应证据；
* **Judge 先过校准门槛（§22.6）才准进 gate** —— 未校准的语义分只是另一个随机数；
* 发布 gate 可组合确定性硬门槛 + 语义分门槛。

#### ④ Runtime Adapter 与 Environment Backend 分开

两个概念，不能揉成一个：

* **Runtime Adapter**（§17）—— OpenClaw / 未来其他 agent runtime **怎么统一接入**；
* **Environment Backend**（§10–§12）—— Docker / workspace / 网络 / 资源 / 安全**怎么隔离**。

Docker 侧验收：镜像固定 digest；每个 run 独立容器 + session + workspace；输入只读挂载、
输出统一收集为 artifact；网络四模式（disabled / mock / allowlist / full）；CPU / 内存 / 时长 /
token / 成本 / tool-call 上限；secret 只在运行时注入，不进镜像、suite、日志、报告；
容器退出可靠清理，清理失败产生**结构化告警**；同镜像 + 同配置 + 同数据集复现同一环境。

> 未来 runtime 不必都跑在 Docker 里（远程托管 agent 可以只走 API），但它仍须返回统一
> `RunResult`，并在 `capabilities()` 里**明确声明自己不支持哪些隔离能力**。

#### ⑤ 开箱即跑 + 全过程可查

「开箱即用」定义成可自动测试的标准：

* 新机器装完 Docker 后**一条命令**启动；无 API key 时用 mock 跑通全链路；配好 key 后一条命令跑真实 eval；
* 启动前自动做依赖 / 模型 / runtime / Docker / 权限健康检查；
* 任意 run 有唯一 ID，可沿这条链路查看：
  **测试问题 → 完整配置与输入 → 每轮运行事件 → skill 选择与加载 → tool call → 输出与 artifact
  → 各项评价及证据 → gate 结果 → 改进建议**；
* 支持按 case / turn / repeat / skill / 模型 / 状态筛选；失败 case 可单独重跑；
* 报告至少支持 HTML + 结构化 JSON；历史实验不覆盖，可对比 No-Skill / V1 / V2 / 不同模型的 delta。

#### ⑥ 结果可信与可复现

> **本条已完成约 70%**：`config_hash`（含 suite + per-model + mock + runtime fingerprint +
> skill 内容 hash + dataset 内容 hash）、配置快照、`workflows/compare_runs.py` 污染检测都已在。

仍缺的部分：

* **错误分类** —— 任务失败 / runtime 失败 / 网络失败 / judge 失败 / 评测系统失败必须分开计，
  混在一起会把「系统崩了」误读成「skill 不行」；
* **单个 case 失败不中断整批实验**；
* 原始结果**不可被评分阶段修改**（scores 只写新文件，不回写 `runs.jsonl`）；
* Schema 变化有版本与兼容策略；
* API key、用户文件、模型输出有明确的保存与脱敏策略。

---

### 阶段计划

| 阶段 | 目标 | 对应验收 | 状态 |
| --- | --- | --- | --- |
| **P0** | 路由 eval 全链路 + runtime 工厂 + OpenClaw 打通 + full 管道四环 | ②③⑥ 的地基 | ✅ 已完成 |
| **P1** | Full eval 的**第一个对照数字** | ③（Tool/Artifact）⑥（错误分类） | ✅ 已完成 |
| **P2** | 自动出题 v0 | ①前半 | ✅ 已完成 |
| **P3** | 改进闭环 v0（建议 → 确认 → 新版本 → 同题复验） | ①后半 | ✅ 链路已跑通；停止条件记录与 pdf 能力基准未做 |
| **P4** | Evaluator 注册表 + 语义 Judge | ②③ | 🚧 语义 judge 已在用（断言 + 维度 + 产物内容）；注册表未抽 |
| **P5** | 多轮编排 + conversation 并发 | ③（多轮维度） | ✅ 已完成 |
| **P6** | Docker Environment Backend | ④ | 🚧 已按需求提前开发（v0 backend + 真实容器 smoke） |
| **P7** | 全过程 Viewer + 开箱一条命令 | ⑤ | 🚧 subject 测试包归档/解归档已完成；Viewer 与一条命令仍缺 |

**排序原则**：先让每条管道产出**数字**，再抽象；先闭环产品价值（出题 → 改进），再做工程化
（Docker / Viewer）。**只有一个实现的接口是负债** —— 注册表等第三类实现出现时再抽（P4 排在 P1/P3 之后就是这个原因）。

每个阶段的验收标准写成**可勾选清单**：一条一个判据，能跑命令验证的写命令。
**全部打勾才算这一阶段完成**，做不到的条目必须显式标记未完成（§29 规则 20），不许悄悄降级。

---

#### P1 · Full eval 的第一个对照数字

> ✅ **2026-07-28 完成。** none → v1 的任务完成度为 16.7% → 83.3%，
> 产物命中率为 0% → 80%，`workflows/compare_runs.py` 无污染项。

**前置**：无（OpenClaw 已打通，注入/加载/tool/产物四环已验证）。

**交付物**

| 文件 | 内容 |
| --- | --- |
| `evals/datasets/full_<skill>_v1.0.jsonl` | 5–10 道「做完能验产物」的任务题，每题**内联断言**：必须产出哪些文件、MIME、必须调到哪个 tool |
| `evals/suites/full_<skill>_{none,v1}.yaml` | `skills.mode: full`，两份只差 skillcfg |
| `workflows/score_full.py` | 产物/tool 的确定性断言打分（§21.2）——**先写死，不建注册表** |
| `contracts/runtime.py` | `RunResult.error` 增加 `error_kind`：`task` / `runtime` / `network` / `harness` |

**验收标准**

- [x] 同一批题跑 none vs v1 两组，各出 `scores.json`，`workflows/compare_runs.py` **无污染项**
- [x] 输出三个新维度：任务完成度、产物命中率（存在 + MIME + 非空）、tool 命中率
- [x] `scores.json` 里不适用的维度记 `N/A`，**不记 0**
- [x] gate 能对 full 维度出 PASS/FAIL，门槛写在 suite 里（进 `config_hash`）
- [x] **单个 case 抛异常不中断整批**：注入一个必失败的 case，其余 case 仍跑完并计分
- [x] 四类 `error_kind` 各构造一次并分别正确归类（断网 / 杀 CLI / 坏 suite / 模型答错）
- [x] `pytest` 全绿，新增断言逻辑有测试

**明确不做**：语义 judge、evaluator 注册表、Docker。题量按 §★★ 约定 **10 题以内**。

---

#### P2 · 自动出题 v0

> 验收①的前半段。目标不是「题写得多」，是「**人不用从零写题**，但仍然看得懂、改得动」。

**前置**：P1（full 题型定型后才知道生成器要产出什么结构）。

**交付物**

| 文件 | 内容 |
| --- | --- |
| `workflows/gen_cases.py` | 读 SKILL.md frontmatter + 用户写的验收标准 → 调模型产出 JSONL → **写成文件等人审，不直接跑** |
| `contracts/evalcase.py` | 扩坏题检查：重复 prompt、gold 指向不存在的 skill、同 prompt 不同 gold、配比缺类 |
| `evals/AUTHORING.md` | 补一节：生成出来的题人工审什么、审到什么程度算过 |

**验收标准**

- [x] 输入只有「skill 目录 + 一段验收标准文字」，输出 `dataset.jsonl` + `suite.yaml` 草稿
- [x] 生成集自带类型配比（pos / amb / rej / multi），缺类报错而不是静默少题
- [x] 坏题检查在**生成时**就拦：故意喂一个指向不存在 skill 的 gold，必须被拒
- [x] 生成的 suite 能直接通过 `contracts/suite.py` 的严格校验
- [x] **结论一致性**：对 ctxweave 生成 10 题，人工审核后跑 none/v1/v2，
      三级阶梯的**排序与 gate 结论**和人工集一致（人工基准见 §★★）
- [x] 不一致时能给出解释（哪几道题分歧、分歧在 gold 还是在 prompt）
- [x] 生成过程本身可复现：generator 的模型/prompt/版本进 dataset 的头部元信息

> 实测结果（qwen，10 题 × 3 次）：generated-none 56.7% / FAIL，
> generated-v1 80.0% / FAIL，generated-v2 100.0% / PASS；排序与 gate 结论均和人工集一致。
> 首版自动集不一致的原因与修正记录在
> `evals/analysis/ctxweave_generated_v1.0_consistency.md`，历史 run 未覆盖。

**明确不做**：审核界面（**人工审核门就是 `git diff`**，不为 review 建 UI）、自动开跑（生成完必须停）。

---

#### P3 · 改进闭环 v0

> 验收①的后半段。这是整个产品**最值钱的一环**：从「告诉你分数」变成「告诉你改哪」。

**前置**：P1（要有失败证据可读）+ P2（要有能复用的同一套题）。

**交付物**

| 文件 | 内容 |
| --- | --- |
| `workflows/suggest.py` | 读 `runs.jsonl` 的失败 case → 聚类失败模式 → 产出改进建议 |
| `subjects/<skill-id>/v<N+1>/` | 用户确认后落地的新版本（复用现有版本目录机制，无需新代码） |

**验收标准**

- [x] 每条建议必须带：`case_id` + **模型原文证据** + 对应评分维度 + 具体改哪句
- [x] 建议按失败模式聚类，不是每道错题一条（10 道错题不应产出 10 条建议）
- [x] **不自动改写 skill**：输出建议后停下等确认，`--apply` 才写文件
- [x] 确认后复跑**同一 suite**（`config_hash` 只允许 skillcfg 一项变），`workflows/compare_runs.py` 无污染项
- [ ] **能力基准**：拿 pdf V1 当输入，系统能自己提出「按任务类型而非文件格式激活」这条
      （§★★ 中它是人工得出的结论）
- [x] 停止条件三选一生效并被记录：gate PASS / 累计 token/墙钟预算 / 最大迭代数（§24）；
      `--previous-report` 继承 lineage，命中后零模型调用、零 skill 写入
- [x] 每轮迭代的建议、diff、delta 都落盘，事后能复述「第 2 轮为什么这么改」

**明确不做**：跳过用户确认的自动改写；无人值守连续迭代（先跑得动第二轮，再谈循环）。

**失败证据按 run 自己的 `skills.mode` 取**，不靠调用方记得传对：

| mode | 什么算失败 | 证据 |
| --- | --- | --- |
| `routing_only` | 选错 skill（`exact_set_match`） | 模型原文 |
| `full` | ① 任务没完成（缺产物 / 没调 tool / 拒答题却留文件 / 运行失败）→ `task_completion`；② judge 判 failed 的语义断言或 < 0.6 的维度分 → `assertion` | 模型原文 + 缺了什么 + judge 证据 + **产物内容**（§11.4） |

full 侧一条 run 最多产出一条证据。同一道题拆成多条，「一个 case 只能归因到一个 cluster」
这条校验就会跟自己打架。确定性失败优先归 `task_completion`：产物都没落，语义断言过不了
是必然结果，归到 `assertion` 会让改进模型去改文风，而真正该改的是「必须写文件」这类硬要求。

**`--apply` 的边界**：只**新增** `subjects/<skill-id>/v<N+1>/`（整目录复制源版本的附件，
再覆写 SKILL.md），源版本一个字不动；`name` 被改掉、正文原样抄回、版本目录已存在，
三者任一都直接报错且不落盘。同时生成 `improvements/round-NN/reeval.suite.yaml` ——
题集、模型、runtime、judge 全部照抄上一轮，只换 `skills.versions`，保证 delta 归因得到那次改动。

**引用校验对格式宽容、对事实严格**：比对 quote 时忽略空白与 markdown 强调符号
（模型复制证据时经常把 `` ` `` 和 `**` 剥掉，那不是编造），文字本身仍必须逐字来自
该 case 的 `raw_output` / `failure_detail`。

---

#### P4 · Evaluator 注册表 + 语义 Judge

> **时机点很重要**：等 P1/P3 里出现第三类评分逻辑再抽。
> 只有一个实现的接口是负债 —— 先写死三份，再看它们真正的共同点。

**前置**：P1 + P3（此时已有路由 / 产物 / tool / 建议四类逻辑）。

**交付物**

| 文件 | 内容 |
| --- | --- |
| `evaluators/__init__.py` | 注册表工厂，与 `adapters/runtimes/` **同一模式**（`@register` 自注册） |
| `evaluators/{routing,artifact,tool,cost}.py` | 把已有写死的打分逻辑搬进来，不改行为 |
| `evaluators/judge.py` | 语义 Judge：judge 模型**独立于执行模型**配置 |

**验收标准**

- [ ] `suite.scoring.evaluators: [...]` 可自由组合，增删不改代码
- [ ] **可插拔判据**（验收②）：新增一个 evaluator 的 `git diff --stat` 只有「新文件 + 注册表一行」，
      `workflows/run_routing.py` / `score_*.py` 零改动，既有测试全绿
- [ ] Evaluator 只读 `RunResult`，**grep 不到任何 OpenClaw 专有字段**（§3.3）
- [ ] 搬迁前后**同一份 `runs.jsonl` 打出完全相同的 `scores.json`**（回归保护）
- [ ] Judge 的模型 / prompt / 版本 / 参数进配置快照与 `config_hash`
- [ ] Judge 输出必须带评分依据与证据引用，无证据的分视为无效
- [ ] **Judge 校准**：与人工标注的一致率达 §22.6 门槛才允许进 gate；不达标时 gate 显式标 `judge-uncalibrated`
- [ ] 执行模型与 judge 模型配成同一个时给出警告（不许默认自己评自己）

**明确不做**：pairwise blind judge（§21.6，后续）。

---

#### P5 · 多轮编排

**前置**：无硬依赖（契约、session 隔离、`{case_id}.t{turn}.r{repeat}` 命名均已就位），可与 P4 并行。

**交付物**：编排层 turn 循环；多轮数据集格式（一个 case 多条 turn）；多轮指标。

**验收标准**

- [x] 多轮 case 能声明每轮的输入与每轮的断言，逐轮计分
- [x] **后一轮能读到前一轮写的文件**（同一 workspace / session）
- [x] **不同 repeat 之间零状态泄漏**：repeat 2 看不到 repeat 1 的任何文件与上下文（§13.6）
- [x] 中途某轮失败时，后续轮标 `skipped` 而不是算作失败（区分「没做对」与「没机会做」）
- [x] 多轮指标产出：session 连续性、上下文保持、文件状态延续、逐轮完成度
- [x] 单轮 case 走同一条编排路径，旧 dataset 无需迁移且既有评分行为回归不变
- [x] `parallelism` 只并发独立 conversation；同 conversation turn 串行，输出恢复矩阵顺序

**明确不做**：persistent memory（默认关，§14.2）。

---

#### P6 · Docker Environment Backend

> 把「环境」从 runtime 里拆出来。`prepared()` 现在**只做了 skill 注入**，
> workspace / 网络 / 资源 / secret 四样都没有。

**前置**：P1（评测内容稳定后再隔离，否则要跟着改两遍）。

**交付物**：`environments/` 后端（`local` + `docker` 两个实现）；openclaw 镜像（固定 digest）。

**验收标准**

- [ ] Runtime Adapter **不感知 Docker**：同一个 `OpenClawRuntimeAdapter` 在 local 与 docker 后端下都能跑
- [ ] 镜像按 **digest** 固定，digest 进 `config_hash`
- [ ] 每个 run 独立容器 + 独立 session + 独立 workspace
- [ ] 输入**只读**挂载（容器内改不动源文件，试着改要失败）
- [ ] 输出统一收集为 artifact（带 sha256）
- [ ] 网络四模式各自可自动验证：`disabled` 断网、`mock` 命中桩、`allowlist` 放行名单外拒绝、`full` 直通
- [ ] 资源上限生效：CPU / 内存 / 时长 / token / 成本 / tool-call 超限即中止并标 `error_kind=harness`
- [ ] **secret 只在运行时注入**：镜像、suite、日志、报告、快照里 grep 不到 key
- [ ] 容器退出后可靠清理；**清理失败产生结构化告警**（不是只打一行日志）
- [ ] 同镜像 + 同配置 + 同数据集 → 环境指纹一致
- [ ] 不支持隔离的 runtime 必须在 `capabilities()` 里声明，suite 要求隔离时**运行前**拒绝

**明确不做**：容器调度平台（§2.2）。

---

#### P7 · 全过程 Viewer + 开箱

**前置**：P1–P6（Viewer 要展示的东西得先存在，否则要跟着反复改）。

**交付物**：一条命令的启动入口 + 启动前健康检查；`outputs/index.html` 跨 run 索引与下钻视图。

**验收标准**

- [x] 单个或多个 subject 的版本、case、suite、fixture、run 可一键聚合成带 manifest/checksum
      的测试包；共享资源只复制不误删，解归档同内容复用、冲突拒绝覆盖
- [ ] 新机器装完 Docker 后**一条命令**启动，无 API key 时用 mock 跑通全链路
- [ ] 配好 key 后**一条命令**完成真实 eval
- [ ] 启动前自动检查：依赖 / 模型可达 / runtime healthcheck / Docker / 文件权限，任一不过给出**可执行的修复提示**
- [ ] 每个 run 有唯一 ID，可沿完整链路查看：
      问题 → 配置与输入 → 每轮事件 → skill 选择与加载 → tool call → 输出与 artifact → 各维度评分与证据 → gate → 改进建议
- [ ] **任意一个数字能在三次点击内追到产生它的原始输出**
- [ ] 支持按 case / turn / repeat / skill / 模型 / 状态筛选
- [ ] 失败 case 可**单独重跑**，重跑结果不覆盖原结果
- [ ] 报告同时给 HTML 与结构化 JSON；历史 run 不被覆盖
- [ ] 可并排对比 No-Skill / V1 / V2 / 不同模型的 delta，`config_hash` 不同时**显式警告不可直接比**

**明确不做**：Streamlit 配置编辑器（§27 M9 —— suite 就是 YAML，编辑器优先级最低）。

---

## 2. 项目边界

### 2.1 本项目负责

* 标准输入输出协议
* Skill 注册和版本管理
* 外层实验配置
* 实验矩阵生成
* Runtime Adapter
* OpenClaw 环境配置
* Docker Workspace 隔离
* 标准 Trace
* 多 Skill 路由指标
* 评测组件编排
* No-Skill、V1、V2 回归比较
* 发布 Gate
* 报告聚合

### 2.2 本项目不负责

* 重新开发 Agent Framework
* 重新开发模型 Gateway
* 重新开发 OpenClaw
* 重新实现完整 Skill Creator
* 重新开发通用 LLM Judge 平台
* 重新开发长期记忆系统
* 重新开发 Webhook 平台
* 重新开发容器调度平台
* 获取或保存模型隐藏思维链

---

## 3. 核心原则

### 3.1 非侵入式集成

禁止直接修改：

* OpenClaw 核心代码
* 第三方模型 SDK
* 第三方评测库内部代码
* Anthropic `skill-creator` 核心实现

只能通过以下方式集成：

* Adapter
* Plugin
* CLI
* API
* 配置注入
* 文件挂载
* Event Hook
* 标准格式转换

### 3.2 单一执行层

实际运行与 Eval 必须调用同一个接口：

```python
class RuntimeAdapter(Protocol):
    def run(self, request: InvocationRequest) -> RunResult:
        ...
```

禁止分别实现：

```text
ProductionRunner
EvalRunner
```

测试系统只负责生成标准运行请求，不能重新实现 Agent Loop。

### 3.3 统一结果协议

所有 Runtime 都必须返回标准：

```text
RunResult
```

所有 Evaluator 都必须返回标准：

```text
EvaluationResult
```

Evaluator 不得依赖 OpenClaw 内部对象。

### 3.4 确定性评估优先

评估顺序：

```text
Schema Validation
→ Assertions
→ Artifact Validation
→ Routing Metrics
→ Tool Metrics
→ Semantic Judge
→ Pairwise Comparison
```

可以通过代码确定的内容，禁止优先使用 LLM Judge。

### 3.5 不保存隐藏思维链

允许记录：

* 用户输入
* 模型可见上下文
* 可用 Skill
* 选择的 Skill
* 加载的 Skill
* Tool Call
* Tool 参数
* Tool 返回
* 文件变化
* 审批事件
* 错误和重试
* 最终输出
* Token、延迟和成本
* Runtime 原生提供的简短决策摘要

禁止要求模型额外输出完整 Chain of Thought。

---

## 4. 成熟组件复用策略

### 4.0 先复用审计，再决定实现

任何新节点开工前，必须按下面顺序做一次**可复用性审计**：

1. 能直接调用成熟框架 / 库的公开接口，就直接调用；
2. Contract 不同但能力已有，只写最薄 Adapter；
3. 只有在现成实现无法满足本项目的稳定 Contract、可复现性或安全边界时，才自研缺口。

不得因为“自己写更顺手”跳过前两步。采用自研时，要在 `DEVLOG.md` 记录：
调研过哪些候选、为什么不能直接复用、最终自研边界是什么；后续发现成熟实现可替代时，
优先删除自研代码并迁回框架。

框架参考不等于整套引入：

* **OpenClaw-RL** 复用其“服务 / trajectory 采集 / Judge / 改进解耦”和
  next-state 反馈思想；本项目不复制它的 Slime、PRM、Trainer 等 RL 训练栈。
* **Claw-Eval** 优先复用其人工验证任务、fixture、rubric 和多次 trajectory 的数据组织；
  需要接入时做 dataset adapter，不另造一套通用 benchmark 格式。
* 框架已覆盖 Agent Loop、模型网关、评测指标、trace、viewer 等通用能力时，
  本项目只保留 Skill Eval 特有的 Contract、归一化和 Gate。

### 4.1 OpenClaw

定位：

```text
Full Skill Eval 阶段的首个真实 Runtime
```

`https://openclaw.ai/` —— 开源的多 LLM 本地 Agent 运行环境(`npm i -g openclaw`,支持 Claude / GPT / 本地模型,macOS/Linux/Windows,含沙箱模式)。自带 Agent Loop、Skill 发现/加载、Tool 调用、Session、Workspace。本项目把它作为"真正加载并执行 skill"这一步的 runtime 基座,通过外层配置 + `OpenClawRuntimeAdapter`(§17)非侵入接入,**不 fork、不改核心**。

负责：

* Agent Loop
* Skill 发现
* Skill 加载
* Tool 调用
* Session
* Workspace
* 模型调用
* 可选网络
* 可选 Memory

本项目不得重新实现这些能力。

**接入时机**：路由 eval(Walking Skeleton、§7.3 routing-only)**不接 OpenClaw**——纯 metadata 推理直接用 LiteLLM 调模型即可。OpenClaw 从 Full Skill Eval(§18.2)开始接,即需要真实执行 skill、调 tool、读写 workspace 的场景。

### 4.2 Anthropic skill-creator

定位：

```text
参考实现
+ Bootstrap 工具
+ 可选 Evaluator Adapter
+ 可兼容数据格式
```

可用于：

* 参考 Skill 创建和迭代流程
* 初始测试问题生成
* 导入已有 Eval Case
* 可选 Analyzer
* 可选 Comparator
* 可选结果 Viewer

不得：

* 将其作为系统强制核心依赖
* 将内部 Contract 绑定到其数据格式
* 限制系统只能使用 Claude
* 将其作为唯一评测引擎
* 复制其完整实现到本项目

### 4.3 成熟库直接复用映射（节点 → 调什么 → 只写胶水）

原则:**下表标"复用"的节点不写核心逻辑,只写 adapter / 调 API。** 真正属于本项目自研的代码只有 4 块(见 §4.5),其余都是拼接现成库。

| 节点 | 要的能力 | 直接复用(调 API,不自研) | 你只需要写 |
| --- | --- | --- | --- |
| N0 Contract | 数据契约 + JSON Schema 导出 | **Pydantic v2**(`model_json_schema()`) | 模型类 |
| N1 Skill Registry | SKILL.md 解析 + 内容 Hash | **python-frontmatter** + stdlib `hashlib`/`pathlib` | 薄 loader |
| N2 Experiment Config | 配置表 + 导入导出 | **PyYAML** / `csv` + **Streamlit** `data_editor` | 配置 schema |
| N3 Matrix Builder | 笛卡尔积展开 | stdlib **`itertools.product`** | ~30 行 |
| N4/N5 Runtime | Agent 执行 + 强隔离 | **OpenClaw**(执行) + **Docker SDK**(隔离) | `OpenClawRuntimeAdapter` |
| — 模型调用 | 多 Provider 统一 API | **LiteLLM**(Claude/GPT/本地一行调用) | 0 行,直接调 |
| N7 Trace | 标准 trace + 落盘 | **OpenTelemetry SDK** + JSONL 兜底 | event→span 映射 |
| N8/N9 确定性评测 | 断言 + Schema 校验 | **pytest** + **jsonschema** | 断言规则 |
| N8/N9 路由指标 | 准确率/混淆矩阵/PRF | **scikit-learn** `metrics` | 0 行 |
| N8/N9 语义/成对 Judge | LLM Judge / G-Eval | **DeepEval** 或 **Braintrust autoevals**;RAG 用 **Ragas** | Evaluator adapter |
| N10 Aggregation | 分组/Delta/方差 + 实验对比 | **pandas** + **Braintrust** 或 **MLflow GenAI** | 发布 Gate 阈值 |
| N11 Report | Run/实验/回归视图 | **MLflow UI** / **Braintrust UI** / **Streamlit** | MVP 不自建 |

Skill 创建/迭代流程与初始测题生成参考 **Anthropic skill-creator**(§4.2,可选、非强制核心依赖)。所有第三方库均通过 Adapter / 配置注入接入(§3.1);无法满足 Contract 时只写最薄兼容层(§4.4)。

### 4.4 禁止重复开发

MVP 不得自行开发：

* 新 Agent Framework
* 新模型 Gateway
* 新 Trace 数据库
* 新长期 Memory
* 新 Webhook 服务
* 新 Skill Creator
* 新通用 Eval Viewer
* 新 Secret Manager
* 新容器调度系统

第三方库无法满足标准 Contract 时，只允许编写最薄兼容层。

---

### 4.5 真正属于本项目自研的代码（其余都是拼接）

只有这 4 块没有现成库能替代,是本项目的核心价值:

1. **Skill 专属 Contract**:`SkillSpec` / `EvalCase` / routing 结果(N0、N1)。
2. **`OpenClawRuntimeAdapter`**:把 OpenClaw 的执行结果归一成标准 `RunResult`(N5、N7)。
3. **路由评测逻辑**:routing-only 加载、No-Skill 拒答、混淆矩阵、Skill-Set 精确匹配(N8、N9)——sklearn 只给底层指标,skill 语义层的组织是自己的。
4. **Skill 配置矩阵 + 回归 Gate**:No-Skill/V1/V2 受控对照、发布门槛判定(N3、N10)。

写代码前先自查:这块是不是上面 4 块之一?**不是,就去 §4.3 找库调,不要自研。**

---

## 5. 系统节点

```text
[N0] Contract Registry
          ↓
[N1] Skill Registry
          ↓
[N2] Experiment Config
          ↓
[N3] Matrix Builder
          ↓
[N4] Environment Resolver
          ↓
[N5] Runtime Adapter
          ↓
[N6] OpenClaw Execution
          ↓
[N7] Result Normalizer
          ↓
[N8] Evaluator Registry
          ↓
[N9] Evaluation Pipeline
          ↓
[N10] Aggregation & Regression
          ↓
[N11] Report & Review
```

---

# 6. N0：Contract Registry

## 6.1 职责

定义系统标准协议：

* `SkillSpec`
* `ToolSpec`
* `FileMount`
* `WorkspacePolicy`
* `NetworkPolicy`
* `ConversationPolicy`
* `MemoryPolicy`
* `WebhookPolicy`
* `ApprovalPolicy`
* `ExperimentSpec`
* `InvocationRequest`
* `RunResult`
* `TraceEvent`
* `EvalCase`
* `EvaluationResult`

使用 Pydantic 严格模式。

所有 Contract 必须可导出 JSON Schema。

---

## 6.2 InvocationRequest

```yaml
schema_version: "1.0"

request_id: string
experiment_id: string
case_id: string
repeat_index: integer

task:
  prompt: string
  messages: []
  parameters: {}
  input_files: []

runtime:
  name: openclaw
  version: string

model:
  provider: string
  name: string
  parameters: {}

skills:
  mode: none | routing_only | full
  enabled: []

tools:
  enabled: []

environment:
  workspace: {}
  network: {}
  conversation: {}
  memory: {}
  webhook: {}
  approval: {}
  trace_level: minimal | standard | debug

limits:
  timeout_seconds: integer
  max_input_tokens: integer
  max_output_tokens: integer
  max_tool_calls: integer
  max_cost: number | null
```

---

## 6.3 RunResult

```yaml
schema_version: "1.0"

run_id: string
request_id: string
status: success | failed | timeout | denied | budget_exceeded

result:
  final_answer: string | null
  structured_output: object | null

routing:
  available_skills: []
  selected_skills: []
  loaded_skills: []

tool_calls: []
artifacts: []
approval_events: []
trace_events: []
errors: []

usage:
  input_tokens: integer | null
  output_tokens: integer | null
  total_tokens: integer | null
  duration_ms: integer
  estimated_cost: number | null

provenance:
  runtime_name: string
  runtime_version: string
  model_provider: string
  model_name: string
  skill_versions: {}
  skill_hashes: {}
  config_hash: string
  dataset_version: string
  source_commit: string
```

---

## 6.4 EvaluationResult

```yaml
schema_version: "1.0"

evaluation_id: string
run_id: string
case_id: string

scores:
  routing: number | null
  task_success: number | null
  correctness: number | null
  completeness: number | null
  schema_compliance: number | null
  tool_use: number | null
  artifact_quality: number | null
  safety: number | null
  efficiency: number | null

assertions: []
errors: []
evidence: []
evaluator_versions: {}

overall_score: number | null
```

---

## 6.5 验收标准

* 所有 Contract 使用严格类型验证。
* 非法枚举值必须拒绝。
* 未声明字段默认拒绝。
* 所有 Contract 可导出 JSON Schema。
* 序列化和反序列化结果一致。
* 每个 RunResult 包含配置 Hash。
* 每个 RunResult 包含 Runtime、模型和 Skill 版本。
* Schema 变化必须提升 `schema_version`。
* 至少支持读取最近两个 Schema 版本。
* Contract 单元测试覆盖率不低于 95%。

---

# 7. N1：Skill Registry

## 7.1 职责

导入和管理：

* Anthropic Skill
* OpenClaw Skill
* 本地 Skill
* Skill V1、V2、V3
* Skill Metadata
* Skill 文件
* Skill 测试集
* Skill 内容 Hash

被测 Skill 的存放布局是 `subjects/<skill-id>/<vN>/`：一个 Skill 一个目录，
每一版是它下面的一个平级子目录，各自是完整一份（SKILL.md + references/ + scripts/）。
由此有两条要求：

* **跑过的版本目录不再改。** 要改 Skill 就新开一版 `v<N+1>`，旧版一个字不动 ——
  改了旧版，历史 run 的输入快照和 `content_hash` 就对不上它声称的输入了（§7.4）。
* **上游安装目录不是被测源。** skillhub 之类的安装器有自己的目录（本仓库是
  `skills/`，已 gitignore），装下来要复制成 `subjects/<skill-id>/v1/` 才进评测。
  这样上游升级永远改不到被测版本，也不需要靠「谁都别碰那个目录」的约定来保证。

---

## 7.2 SkillSpec

```yaml
skill_id: document
version: 2.0.0
source_type: anthropic | openclaw | local
source_path: string
content_hash: string

metadata:
  name: string
  description: string
  triggers: []
  exclusions: []

files:
  skill_md: string
  references: []
  scripts: []
  assets: []

evals:
  source_format: anthropic | native | external
  cases: []
```

---

## 7.3 Skill 加载模式

### None

目标 Skill 完全不可见。

用于：

```text
No-Skill Baseline
```

### Routing-only

只暴露：

* Skill 名称
* Description
* Triggers
* Exclusions
* 版本

不得读取：

* 完整 `SKILL.md`
* References
* Scripts
* Templates
* Assets

用途：

* 多 Skill 路由测试
* No-Skill Rejection
* Skill 混淆测试
* Trigger 优化

### Full

先暴露 Metadata。

模型选中 Skill 后，按需加载：

* `SKILL.md`
* References
* Scripts
* Templates
* Assets

不得在运行开始时一次性加载全部完整 Skill。

---

## 7.3b 版本目录：V1/V2 对照的唯一实现方式

**改 `skills.dir` 不是版本对照，是换实验。** dir 决定 catalog 里有哪些 skill；
把它指向只放了一个 V2 skill 的目录，catalog 会从 N 个缩成 1 个 —— 那是
「单选题 vs N 选一」，delta 不可归因。

版本对照只能用 `skills.versions`：catalog 仍从 `dir` 扫全部 skill，只把点名的
skill 换成它的另一个版本目录。

```yaml
skills:
  dir: subjects
  versions: {pdf: v2}     # ← V1 套件不写这行，V2 套件只多这一行
  cfg: v2
```

* **不钉版本 = 取版本号最小的那一版（v1）**，不是最新的。基线因此永不漂移：
  日后给任何 skill 加 v3，一份写好的旧 suite 含义一个字不变。
* **一次只钉目标 skill**，改的是「谁取哪一版」而不是「哪个目录覆盖哪个目录」——
  多换了别人在配置里就直接看得见，不需要靠编排层比 hash 事后告警。
* plan 的 catalog 行逐个打印 `skill_id@version`，run 的 `config.snapshot.yaml`
  记 `skill_versions`：本次每个 skill 实际取了哪一版，永远查得到。

> 上一版机制（`skills.overlay`：另开一个目录、按 skill_id 整目录覆盖）已删除。
> 它的问题是替换粒度是**目录**而不是**skill**：`skills_v2/` 里残留了上一个实验的
> `pdf/` 与 `interactive-architecture-diagram/`，挂上去一次换掉 3 个 skill，
> 对照实验静默变成多变量实验，只能靠 plan 里 50 行的 hash 比对告警去兜。
> 版本目录把这类错误变成了不可能，那套告警随之删掉。

## 7.3c 被停用的 Skill

Skill frontmatter 里的 `disable: true`（或 `enabled: false`）表示作者停用了它。
skillEval **不替用户做决定**：照常加载、照常评测，但必须在 plan 与 run 的输出里
显式告警 —— 否则评测 catalog 与 Agent 实际加载的 catalog 不一致，而用户看不出来。

---

## 7.3d Catalog 组成：这次把哪些 Skill 传进去

本地有多少个 Skill 与本次实验暴露多少个，是两件事。**catalog 组成由 suite 决定**，
用 `skills.include`（点名要哪些）和 `skills.exclude`（从全部里剔掉哪些）：

```yaml
skills:
  dir: subjects
  include: [artifacts-builder, docx, mcp-builder, pdf, pptx, xlsx]   # catalog 就是这 6 个
```

* **优先用 `include`。** 它是显式清单，之后本地再装 10 个 Skill 也不会漂进这次实验；
  `exclude` 是减法，装了新的就会悄悄进来。这与 §7.3b 版本默认取 v1 是同一个理由。
* **catalog 组成进 `config_hash`。** 它是 routing 这道选择题的选项集合：选项多两个，
  误触发的机会就多两个，`false_activation` 与混淆矩阵都会变。因此**没有命令行覆盖**——
  能用 CLI 改一下就换掉实验的话，归档记的配置就不再是真跑的配置。
* **gold 可达性必须在花钱前检查。** 题集的 `expected_skills` 指向 catalog 里没有的
  Skill 时，那些题没有正确答案可选，指标必然掉，而现象看起来像「Skill 变差了」。
  编排层必须在 plan 阶段报出「几道题、缺哪个 Skill、举例哪几条」。
  不拦运行：No-Skill 基线（§18.3）正是故意造成这个状态。

---

## 7.4 验收标准

* 同一 Skill 重复导入结果幂等。
* 导入不得改写已有的版本目录：同一个 `<skill-id>/<vN>/` 装第二次必须报错，
  要更新就装成新的 `v<N+1>`。
* 必须计算内容 Hash。
* 必须保存版本和来源。
* Skill 文件变化后 Hash 必须改变。
* Routing-only 模式不能读取正文。
* Full 模式只能加载被选中的 Skill。
* 导入失败返回结构化错误。
* 外部 Eval 格式转换不得丢失 Prompt、文件和 Assertion。
* Skill V1 与 V2 必须可以同时注册。
* Catalog 组成只由 suite 的 include/exclude 决定，不提供命令行覆盖（§7.3d）。
* 题集 gold 指向 catalog 里没有的 Skill 时，必须在运行前告警并说明影响几道题。
* `disable: true` 的 skill 必须在 plan/run 输出里被显式标出。

---

# 8. N2：Experiment Config

## 8.1 职责

通过外层配置表设置：

* 测试集
* 模型
* Runtime
* Skill
* Skill 版本
* Tool
* 文件
* 网络
* Workspace
* 多轮对话
* Memory
* Webhook
* 审批
* Trace
* 重复次数
* Token 和成本限制

配置来源：

* Web 表格
* YAML
* CSV
* CLI
* API

---

## 8.2 配置表字段

| 字段                | 示例                                 |
| ----------------- | ---------------------------------- |
| enabled           | true                               |
| case_set          | routing_common_20                  |
| runtime           | openclaw                           |
| model             | claude / gpt / gemini              |
| skill_mode        | none / routing_only / full         |
| skills            | document:v2,pdf:v1                 |
| tools             | filesystem,python                  |
| workspace_mode    | isolated                           |
| network_mode      | disabled / mock / allowlist / full |
| network_allowlist | github.com                         |
| files             | fixtures/report.pdf                |
| conversation_mode | single_turn / multi_turn / auto    |
| memory_mode       | disabled / session / persistent    |
| webhook_mode      | disabled / mock / enabled          |
| approval_mode     | sandbox_auto / ask / deny          |
| trace_level       | minimal / standard / debug         |
| repeats           | 3                                  |
| token_budget      | 12000                              |
| tool_call_limit   | 15                                 |
| timeout           | 300                                |
| tags              | routing,v2                         |

---

## 8.3 验收标准

* 每行可转换成合法 `ExperimentSpec`。
* 非法配置不能进入执行阶段。
* YAML、CSV、UI 导入导出不得丢字段。
* UI 不能成为唯一执行入口。
* 同一配置必须可通过 CLI 执行。
* Secret 只能引用环境变量或 Secret ID。
* Secret 不得保存到普通配置文件。
* 运行前显示实验任务数量。
* 运行前显示最大 Token 请求量。
* 相同配置生成稳定 Hash。
* **Hash 只覆盖会改变结果的字段。** `suite_id` / `suite_version` / `description`
  这类纯给人读的元信息不进 Hash —— 改一句注释就让两次结果「不可比」，
  会训练用户忽略 Hash 告警，那比没有 Hash 更糟。它们仍进 `config.snapshot.yaml`。

---

# 9. N3：Matrix Builder

## 9.1 职责

将实验配置展开为运行矩阵：

```text
Eval Case
× Skill Configuration
× Skill Version
× Model
× Runtime
× Repeat
```

自动支持：

```text
No Skill
Skill V1
Skill V2
Skill V2 + Distractor Skills
```

---

## 9.2 对照原则

No-Skill、V1 和 V2 必须保持一致：

* Prompt
* 输入文件
* Tool 权限
* 网络配置
* Workspace 配置
* 模型
* 模型参数
* Token 上限
* Timeout
* Judge 标准

唯一变量应是目标 Skill 配置。

**表达这个「唯一变量」的手段是 `skills.versions` + `skills.cfg`，不是 `skills.dir`**
（§7.3b）。dir 改了 catalog 就变了，那已经不是同一个实验。

---

## 9.3 验收标准

* 任务数量等于矩阵乘积。
* 同一配置生成顺序确定。
* 每个 Repeat 有唯一 Run ID。
* 每个 Repeat 使用新 Session。
* 每个任务可以单独重跑。
* Matrix Builder 不得调用模型。
* 10,000 个任务展开不能产生明显阻塞。
* 失败任务不得从矩阵中静默删除。

---

# 10. N4：Environment Resolver

## 10.1 职责

将 `EnvironmentSpec` 转换为真实 Docker 运行环境：

* Workspace
* Skill 挂载
* Tool 注册
* 输入文件挂载
* 输出目录
* 网络策略
* Conversation
* Memory
* Webhook
* 审批
* 限额

---

## 10.2 默认基线环境

```yaml
workspace:
  mode: isolated
  writable: true
  persist_within_run: true
  persist_after_run: false

network:
  mode: disabled

conversation:
  mode: auto

memory:
  mode: disabled

webhook:
  mode: disabled

approval:
  mode: sandbox_auto

trace_level: standard
```

“默认关闭”只表示基线配置，不代表系统不支持。

---

# 11. Workspace 策略

## 11.1 配置

```yaml
workspace:
  mode: isolated | reusable
  writable: true
  input_mounts: []
  output_path: /workspace/outputs
  persist_within_run: true
  persist_after_run: false
```

---

## 11.2 isolated

每个 Run 使用独立 Docker 容器或独立 Workspace。

适用于：

* 路由测试
* No-Skill 对照
* V1/V2 回归
* 重复稳定性测试
* 安全测试

同一个 Run 内：

* Skill 可以读写 `/workspace`
* 文件状态持续存在
* 多轮对话可以访问前一轮文件

不同 Run 之间：

* 文件不可见
* Session 不共享
* Memory 不共享

---

## 11.3 reusable

允许明确关联的任务复用同一个 Workspace。

适用于：

* 多阶段文档处理
* 代码生成后继续修改
* 先创建文件再审阅
* 模拟真实项目目录

必须显式指定 Workspace ID。

禁止默认跨实验复用。

---

## 11.4 文件挂载

输入文件由 **case 自己声明**（§25.2 的 `files` 字段），路径相对仓库根，
素材放 `evals/fixtures/`。Environment Backend 在 `prepared()` 里把它们物化进
workspace，Runtime 只看到 workspace 里的相对路径。

**没有实现之前不许写进用户文档。** 文档承诺了「只读挂载」而代码里搜不到
`fixtures` 一次，用户会照着写题，然后发现只能把文件内容整段内联进 prompt ——
测出来的东西和真实用法对不上。

输入文件：

* 默认只读
* 计算 SHA-256
* 记录文件大小和 MIME Type
* 写入 Run provenance
* 多个 case 可共用同一份素材，物化时按 case 各自复制，互不影响
* 声明了但不存在的文件 → **运行前**报错，不是跑到一半才炸

输出文件：

* 只能写入 Workspace
* 必须登记为 Artifact
* 记录 SHA-256
* 记录文件大小和 MIME Type
* **文本类产物还要留内容前缀**（`Artifact.text_excerpt`，截断到 `TEXT_EXCERPT_LIMIT`）：
  workspace 跑完即删，不在采集时留一份，judge 与改进环节就只看得见文件名和大小。
  判定方式是"能不能按 UTF-8 解出来"，不是扩展名白名单；二进制产物恒为 `None`，
  需要读内容才能确认的断言按"证据不足"判 failed（§21.6）。

---

## 11.5 验收标准

* Skill 可以在 `/workspace` 内正常读写。
* 同一多轮 Case 可以读取前一轮文件。
* 不同 Run 不能读取彼此文件。
* 只读输入不得被修改。
* 未声明路径不能访问宿主机。
* 所有输出必须登记为 Artifact。
* 运行结束后按策略保存或清理 Workspace。
* 清理失败必须产生结构化告警。

---

# 12. Network 策略

## 12.1 配置

```yaml
network:
  mode: disabled | mock | allowlist | full
  allowlist: []
  fixture_set: null
  record_responses: false
```

---

## 12.2 disabled

完全禁止外部网络。

用于：

* 路由测试
* 可复现基准
* V1/V2 基线
* 不需要联网的 Skill

---

## 12.3 mock

使用：

* 固定 API Response
* 网页快照
* 本地 Mock Server
* 录制的 HTTP Response

优先用于版本对比。

确保 V1 和 V2 获取相同外部数据。

---

## 12.4 allowlist

只允许访问配置中的：

* 域名
* API
* 服务
* 端口

适用于：

* 搜索 Skill
* GitHub Skill
* API 查询 Skill
* 在线文档 Skill

---

## 12.5 full

允许普通外部网络。

仅用于真实工作环境测试。

使用真实网络的结果必须标记：

```text
non_deterministic_external_dependency = true
```

---

## 12.6 验收标准

* disabled 模式阻止所有外部请求。
* mock 模式相同请求返回确定性结果。
* allowlist 模式阻止未声明域名。
* 所有网络拒绝事件进入 Trace。
* full 模式必须在报告中标记非完全可复现。
* No-Skill、V1、V2 使用相同网络配置。
* 网络失败与任务失败必须分开统计。

---

# 13. Conversation 与多轮对话

## 13.1 配置

```yaml
conversation:
  mode: single_turn | multi_turn | auto
  session_id: null
  reset_between_cases: true
  turns: []
```

---

## 13.2 single_turn

每个问题独立运行。

适用于：

* 路由准确率
* 简单 Skill 调用
* 单次结果生成
* No-Skill Rejection

---

## 13.3 multi_turn

同一个 Eval Case 的所有 Turn：

* 使用同一个 OpenClaw Session
* 使用同一个 Workspace
* 保留对话历史
* 保留本轮产生的文件
* 保留当前任务状态
* 可以在不同轮次调用不同 Skill

不同 Case 和不同 Repeat 必须重置。

示例：

```yaml
conversation:
  mode: multi_turn
  turns:
    - role: user
      content: "读取项目文件并生成初步计划"
    - role: user
      content: "把第二阶段拆得更详细"
    - role: user
      content: "根据新附件更新计划"
```

---

## 13.4 auto

* 一个 Turn：自动使用 `single_turn`
* 多个 Turn：自动使用 `multi_turn`

---

## 13.5 多轮 Trace

每轮必须记录：

* Turn Index
* 用户输入
* 本轮输出
* 本轮可用 Skill
* 本轮选择 Skill
* 本轮加载 Skill
* Tool Call
* Artifact 变化
* 错误
* Token
* 延迟

---

## 13.6 验收标准

* 同一 Case 的 Turn 使用同一 Session。
* 后续 Turn 可以访问前一轮文件。
* 后续 Turn 可以访问前一轮可见对话。
* 每轮 Skill 选择可独立评估。
* 每轮 Tool Call 可独立评估。
* Case 结束后 Session 必须清理。
* 不同 Repeat 不得共享对话状态。
* 多轮失败必须能定位到具体 Turn。

---

# 14. Session 与 Memory

## 14.1 Session Context

Session Context 是同一测试用例内的短期状态：

* 对话历史
* 当前任务状态
* Tool 返回
* 生成文件
* 已执行步骤

多轮对话必须支持 Session Context。

---

## 14.2 Persistent Memory

Persistent Memory 是跨 Case 或跨 Run 的长期状态。

```yaml
memory:
  mode: disabled | session | persistent
  namespace: null
  reset_before_run: true
```

### disabled

不启用跨 Run 记忆。

不影响正常多轮对话。

### session

只允许当前 Session 内状态。

### persistent

允许跨 Session 读取和写入 Memory。

仅用于专门测试：

* 记忆写入
* 记忆召回
* 记忆更新
* 错误记忆污染
* 数据隔离
* 遗忘机制

Persistent Memory 不进入第一阶段核心 Eval。

Contract 必须预留，具体评估后续实现。

---

## 14.3 验收标准

* 普通多轮对话不依赖 Persistent Memory。
* 默认 Run 不能读取历史长期记忆。
* Persistent Memory 必须显式开启。
* Memory Namespace 按实验隔离。
* Memory 读取和写入进入 Trace。
* Memory 泄漏必须作为 Critical Error。

---

# 15. Webhook 与外部事件

## 15.1 配置

```yaml
webhook:
  mode: disabled | mock | enabled
  endpoints: []
```

### disabled

默认模式。

### mock

使用固定事件 Fixture。

适用于：

* 事件触发 Skill
* 消息接收 Skill
* 状态变更 Skill

### enabled

真实 Webhook。

必须记录：

* 事件来源
* 接收时间
* Payload Hash
* 去重 ID
* 审批结果
* 外部副作用

---

## 15.2 MVP 边界

第一阶段：

* Contract 预留 Webhook 字段
* 支持 disabled
* 可选支持 mock

后续阶段：

* 真实公网 Webhook
* 长时间运行任务
* Event-driven Agent
* 跨 Session 工作流

---

# 16. Approval 策略

## 16.1 配置

```yaml
approval:
  mode: sandbox_auto | ask | deny
  log_decisions: true
```

---

## 16.2 sandbox_auto

自动批准：

* 读取已挂载文件
* 读取 Skill 文件
* 调用声明的本地 Tool
* 在 Workspace 内写文件
* 执行声明的测试脚本
* 调用 Mock 网络服务

所有批准必须留痕。

---

## 16.3 ask

以下操作需要询问：

* 访问真实网络
* 发送消息
* 调用付费外部服务
* 修改外部数据库
* 发布内容
* 删除外部资源
* 访问额外 Secret
* 修改 Workspace 外文件

无人值守测试不得停在人工审批界面。

必须预设：

```text
approve
deny
mock
```

---

## 16.4 deny

默认拒绝：

* 未声明 Tool
* 未声明网络目标
* 越过 Workspace 的写入
* 宿主机危险命令
* 修改测试基准
* 修改参考答案
* 测试过程中修改目标 Skill 版本

---

## 16.5 ApprovalEvent

```yaml
event_id: string
run_id: string
action: string
resource: string
decision: approved | denied | mocked
decision_source: policy | user | fixture
timestamp: string
```

---

## 16.6 验收标准

* 所有高风险动作都有 ApprovalEvent。
* 自动批准仅限沙箱内部。
* 外部副作用默认不自动批准。
* 无人值守运行没有阻塞式审批弹窗。
* 审批结果可以作为 Assertion。
* 未授权操作必须被阻止并记录。

---

# 17. N5：Runtime Adapter

## 17.1 接口

```python
class RuntimeAdapter(Protocol):
    def run(self, request: InvocationRequest) -> RunResult:
        ...

    def healthcheck(self) -> RuntimeHealth:
        ...

    def capabilities(self) -> RuntimeCapabilities:
        ...
```

---

## 17.2 首个实现

```text
OpenClawRuntimeAdapter
```

后续可扩展：

```text
ClaudeCodeRuntimeAdapter
CodexRuntimeAdapter
DirectLLMRuntimeAdapter
CustomAgentRuntimeAdapter
```

---

## 17.3 Runtime Adapter 职责

* 创建 Docker 环境
* 配置 OpenClaw
* 注入模型
* 注入 Skill
* 注入 Tool
* 挂载文件
* 配置网络
* 配置 Session
* 配置 Memory
* 配置审批
* 执行任务
* 收集事件
* 返回 RunResult

Runtime Adapter 不负责评分。

---

## 17.4 验收标准

* 上层不得直接引用 OpenClaw 内部对象。
* 替换 Runtime 不需要修改 Evaluator。
* Runtime 异常转换为标准错误。
* Timeout 可以强制终止。
* 每次运行记录 Runtime 版本。
* 实际任务和 Eval 任务使用同一接口。
* 原始 Runtime 输出可保存为可选 Artifact。
* 同一 InvocationRequest 至少支持两个模型。
* OpenClaw 升级后必须运行 Adapter Contract Tests。

---

# 18. N6：OpenClaw Execution Modes

## 18.1 Routing Eval

```text
Skill Metadata
→ 模型选择 Skill
→ 返回路由结果
```

不得：

* 加载完整 Skill
* 执行完整任务
* 调用 Tool

用于：

* 20 Skill 路由测试
* Trigger 测试
* No-Skill Rejection
* Confusion Matrix
* Description V1/V2 比较

---

## 18.2 Full Skill Eval

```text
Skill Metadata
→ 选择 Skill
→ 加载完整 Skill
→ 调用 Tool
→ 生成结果
```

用于：

* Skill 是否有效
* Tool 是否正确
* 输出是否符合要求
* Skill V1/V2 回归
* Token、成本、延迟比较

---

## 18.3 No-Skill Baseline

目标 Skill 必须完全不可见。

可以保留：

* 基础模型
* 通用 Tool
* 相同输入文件
* 相同网络
* 相同 Token 上限

---

## 18.4 Workspace Simulation

同时提供：

* 约 20 个常用 Skill
* 常用 Tool
* 项目文件
* 可选网络
* 单轮或多轮 Session
* 可写 Workspace

用于模拟真实工作环境。

---

## 18.5 验收标准

* Routing Eval 不执行 Tool。
* Routing Eval 不读取完整 Skill。
* Full Eval 记录实际加载 Skill。
* No-Skill 确认目标 Skill 不可见。
* Workspace Simulation 记录全部可用 Skill。
* 每次 Skill 选择进入 Trace。
* 每次 Tool Call 进入 Trace。
* 每次运行都返回标准状态。

---

# 19. N7：Result Normalizer 与 Trace

## 19.1 标准 TraceEvent

```yaml
event_id: string
run_id: string
turn_index: integer | null
timestamp: string
type: string
data: {}
```

标准事件：

```text
run_started
environment_resolved
workspace_created
skill_catalog_exposed
skill_selected
skill_loaded
tool_call_started
tool_call_completed
network_request
network_response
approval_requested
approval_decided
artifact_created
artifact_modified
memory_read
memory_written
model_response
turn_completed
run_completed
run_failed
```

---

## 19.2 Trace Level

### minimal

记录：

* Skill 选择
* 最终输出
* Usage
* Error

### standard

记录：

* minimal
* Tool Call
* Approval
* Artifact
* 网络事件
* 状态变化

### debug

记录：

* standard
* Runtime 原始事件
* 模型可见输入
* Tool 原始结果
* 重试过程

Debug 默认只用于：

* 失败 Run
* 手动指定 Run
* 调试测试集

---

## 19.3 验收标准

* 成功和失败都生成 RunResult。
* TraceEvent 包含 Run ID 和时间。
* 事件顺序可以重建运行过程。
* Secret 必须脱敏。
* 不保存隐藏思维链。
* standard Trace 不产生额外模型 Token。
* Trace 失败不能导致主任务失败。
* Trace 后端不可用时降级为本地 JSONL。
* 本地 Trace 可以后续重新导入实验系统。

---

# 20. N8：Evaluator Registry

## 20.1 目标

评测能力必须可以组装，不绑定单一库。

统一接口：

```python
class Evaluator(Protocol):
    evaluator_id: str
    version: str

    def evaluate(
        self,
        run_result: RunResult,
        eval_case: EvalCase,
        context: EvaluationContext,
    ) -> EvaluationResult:
        ...
```

---

## 20.2 默认 Evaluator

```text
SchemaEvaluator
AssertionEvaluator
RoutingEvaluator
ToolCallEvaluator
ArtifactEvaluator
ConversationEvaluator
SafetyEvaluator
CostEvaluator
RegressionEvaluator
```

---

## 20.3 可选 Adapter

```text
SkillCreatorAdapter
DeepEvalAdapter
RagasAdapter
CustomJudgeAdapter
MLflowScorerAdapter
```

每个 Adapter：

* 可独立启用
* 可独立禁用
* 必须记录版本
* 必须输出统一 EvaluationResult
* 不得改变 RunResult

---

## 20.4 skill-creator Adapter

用途：

* 导入 Eval Case
* 使用其测试生成思路
* 可选调用 Analyzer
* 可选调用 Comparator
* 可选使用其 Viewer

不得：

* 成为唯一 Evaluator
* 控制内部 Contract
* 修改系统 Runtime
* 限制模型 Provider

---

## 20.5 验收标准

* Evaluator 可以通过配置启用和禁用。
* 删除一个可选 Evaluator 不影响 Runtime。
* 同一指标只有一个默认实现。
* Evaluator 版本写入结果。
* Evaluator 失败不得覆盖其他结果。
* 历史 EvaluationResult 不依赖当前库版本才能读取。

---

## 20.6 复用审计记录：语义维度为什么是内置 rubric（2026-07-28）

按 §29 规则 25「直接调用 → 薄 Adapter → 自研缺口」实测走了一遍，结论与 §4.3 选型表
（原写「DeepEval 或 Braintrust autoevals」）**有出入，以本节为准**：

| 候选 | 版本 | 结论 |
| --- | --- | --- |
| **DeepEval** | 4.1.4 | ❌ 不接。它最对口的 `TaskCompletionMetric` / `ToolCorrectnessMetric`，本项目**已有确定性实现**（`workflows/score_full.py` 的 `done` / `tool_hit`），换成 LLM 判定违反 §3.4 与规则 12；`PlanQualityMetric` / `StepEfficiencyMetric` 需要完整 trace，而 `RunResult` 只有聚合 `toolSummary`，喂不进去。实际用得上的只剩 G-Eval，为它背一个带 CLI / telemetry / 云端集成的重依赖不划算 |
| **Braintrust autoevals** | 0.3.0 | ⚠️ 部分复用。`LLMClassifier`（prompt_template + choice_scores）的结构值得抄，`workflows/dimensions.py` 就是照它组织的；但它绑 OpenAI SDK，而 judge 在本项目必须走独立 `JUDGE_*` 通道，装了它等于仓库里并存两条判分调用路径 |
| **Ragas** | — | ❌ 场景不符，本项目不是 RAG，`context_precision`/`context_recall` 无处安放 |

**最终做法**：复用**维度定义与 rubric**（那才是真正沉淀的东西，每条在 `workflows/dimensions.py`
里用 `source` 字段注明抄自哪个库的哪个指标），调用层走已有的 litellm judge 通道。
这样零新依赖、judge 的 env 解耦不受破坏，将来要换库时对着 `source` 逐条对表即可。

> 若日后 `RunResult` 支持完整 trace（P5/P7），`PlanQuality`/`StepEfficiency`
> 会重新变得可用，届时按 §20.3 的 `DeepEvalAdapter` 补一个薄 Adapter，不要推翻本节。

---

# 21. N9：Evaluation Pipeline

## 21.1 评估顺序

```text
1. RunResult Schema
2. Deterministic Assertions
3. Artifact Validation
4. Routing Metrics
5. Tool Metrics
6. Conversation Metrics
7. Safety Metrics
8. Semantic Judge
9. Pairwise Comparison
10. Cost and Efficiency
```

---

## 21.2 Deterministic Assertions

使用 pytest、JSON Schema 或自定义规则检查：

* Skill 是否正确
* 禁止 Skill 是否未调用
* Tool 是否正确
* Tool 参数是否正确
* Tool 顺序是否正确
* 文件是否存在
* 文件 MIME 是否正确
* JSON Schema 是否通过
* 网络策略是否遵守
* 审批策略是否遵守
* 输出字段是否完整

---

## 21.3 路由指标

必须支持：

```text
Top-1 Accuracy
Top-k Recall
Exact Skill-Set Match
Precision
Recall
F1
False Activation Rate
Miss Rate
No-Skill Rejection Accuracy
Skill Call Order Accuracy
Skill Confusion Matrix
Critical Skill Miss Rate
```

`Critical Skill Miss Rate` = `severity: critical` 的题里判错的比例，
默认门槛 **= 0**（§22.4）。这是 `severity` 字段唯一的落地用途：
**定义了却没人消费的字段等于骗人** —— 写题的人以为标了 critical 就有额外保护，
实际上跟 medium 一模一样。要么接进指标，要么从契约里删掉。

---

## 21.4 多轮指标

至少支持：

```text
Turn-level Skill Accuracy
Turn-level Tool Accuracy
Skill Transition Accuracy
Context Retention
File Continuity
Final Task Completion
Conversation Consistency
```

复杂多轮 Judge 可以后续实现。

基础多轮 Contract 和 Trace 必须在 MVP 中完成。

---

## 21.5 任务效果指标

```text
Task Success Rate
Assertion Pass Rate
Schema Pass Rate
Artifact Pass Rate
With-Skill Uplift
V2 vs V1 Delta
Mean
Median
Standard Deviation
P90 Latency
Token Usage
Estimated Cost
Successful Task Cost
```

---

## 21.6 Semantic Judge

只有以下内容可以使用 Judge：

* 正确性难以通过代码判断
* 完整性
* 可用性
* 表达质量
* 复杂任务结果比较
* 是否需要返工

Judge 必须：

* 记录 Prompt
* 记录模型
* 记录版本
* 记录输入 Hash
* 隐藏 Skill 版本身份
* 随机交换 A/B 顺序

**Judge 看得到什么，决定这些分数有没有意义。** 输入里必须带上文本产物的内容
（`Artifact.text_excerpt`，§11.4），否则"报告里的数字是不是编的"只能判"证据不足"，
分数低得毫无信息量 —— 实测同一批 run，产物内容进 prompt 前后
`assertion_pass_rate` 40% → 90%，`faithfulness` 0.33 → 0.67，差的全是判定证据，不是 skill。
二进制产物（docx/png）与被截断的部分仍然看不见，这条边界要写进 prompt，
让 judge 明确判 failed 并注明"产物内容不可见"，而不是凭文件名猜一个 passed。

---

## 21.7 验收标准

* 确定性评估必须先运行。
* 可通过代码判断的内容不得依赖 Judge。
* Judge 失败不得覆盖 Assertions。
* Pairwise Judge 必须盲评。
* A/B 顺序随机化。
* 所有分数保留证据。
* 所有 Eval 可以追溯到原始 Run。
* Evaluation Engine 不依赖 OpenClaw 对象。

---

# 22. N10：Aggregation 与 Regression

## 22.1 比较维度

```text
No Skill vs With Skill
V1 vs V2
Single Skill vs Multi-Skill
Routing-only vs Full
Model A vs Model B
Single-turn vs Multi-turn
Network Mock vs Real Network
```

---

## 22.2 回归分类

```text
Improved
Regressed
Unchanged
Flaky
Inconclusive
Environment Error
Runtime Error
Evaluator Error
```

---

## 22.3 默认系统门槛

```text
RunResult Schema Pass Rate = 100%
Trace Completion Rate ≥ 99%
Experiment Completion Rate ≥ 95%
Environment Leakage = 0
Critical Permission Violation = 0
```

---

## 22.4 默认路由门槛

基准规模：

```text
20 个常用 Skill
100 个问题
每题 3 次独立运行
```

默认门槛：

```text
Top-1 Skill Accuracy ≥ 90%
No-Skill Rejection Accuracy ≥ 90%
False Activation Rate ≤ 5%
Critical Skill Miss Rate = 0
```

---

## 22.5 Skill V2 发布门槛

```text
Critical Regression = 0
目标修复用例必须提升
Schema Pass Rate 不得下降
Safety Score 不得下降
Overall Task Success 不低于 V1 超过 2 个百分点
```

如果成本增加超过 20%，至少满足一项：

```text
Task Success 提升 ≥ 10 个百分点
Critical Failure 明显减少
项目明确接受成本交换
```

---

## 22.6 Judge 校准门槛

```text
Judge vs Human Agreement ≥ 80%
A/B Swap Consistency ≥ 90%
Invalid Judge Output ≤ 2%
```

---

## 22.7 验收标准

* 报告同时展示绝对值和 Delta。
* 报告必须包含方差。
* 失败 Run 不得静默排除。
* Environment Error 与 Task Failure 分开统计。
* 回归案例可直接定位到 Trace。
* 发布结论由配置化 Gate 决定。
* 不允许模型自由决定是否发布。

---

# 23. N11：Report 与 Review

## 23.1 优先复用

优先使用：

* MLflow UI
* Anthropic Eval Viewer
* Streamlit 配置页
* 第三方评测 Viewer

MVP 不开发完整自定义 Dashboard。

---

## 23.2 Run Report

展示：

* Input
* Environment
* Skill Catalog
* Skill Routing
* Skill Loading
* Tool Calls
* Network
* Approval
* Artifacts
* Output
* Scores
* Usage
* Errors

---

## 23.3 Experiment Report

展示：

* No-Skill、V1、V2 对比
* 多模型对比
* 路由指标
* 多轮指标
* 任务成功率
* Token
* 成本
* 延迟
* 方差
* 错误分类

---

## 23.4 Regression Report

展示：

* 提升案例
* 退化案例
* Flaky 案例
* 新增错误
* 已修复错误
* 发布 Gate
* 配置变化

---

## 23.5 验收标准

* 每个指标可追溯到 Run。
* 每个失败案例可以打开 Trace。
* 报告显示 Config Hash。
* 报告显示 Skill、模型和 Runtime 版本。
* 至少支持 JSON 和 HTML 导出。
* 报告不得泄漏 Secret。
* 第三方 Viewer 满足需求时不得重复开发。

---

# 24. Token 与成本控制

按以下顺序优化：

1. 路由测试只发送 Skill Metadata。
2. 只加载被选中的完整 Skill。
3. 路由测试与完整执行分开。
4. 确定性规则优先。
5. 只有必要结果进入 Judge。
6. 默认关闭 Persistent Memory。
7. 默认关闭真实 Webhook。
8. 网络优先使用 Mock。
9. Debug Trace 只用于失败 Run。
10. 不要求模型额外解释思维过程。
11. 限制最大 Tool Call。
12. 限制最大输入和输出 Token。
13. 限制最大成本。
14. Judge 结果可按输入 Hash 缓存。
15. 被测模型结果默认不缓存。
16. No-Skill、V1、V2 使用相同预算。

---

## 24.1 验收标准

* Routing-only 不加载完整 Skill Token。
* Deterministic-only 测试不调用 Judge。
* 报告显示总 Token 和总成本。
* 超预算 Run 标记为 `budget_exceeded`。
* Token 优化不得改变测试输入语义。
* Trace 不得通过额外模型调用生成。

---

# 25. 测试集规范

## 25.1 目录

```text
evals/
├── routing/
│   ├── positive/
│   ├── negative/
│   ├── ambiguous/
│   └── multi_skill/
├── effectiveness/
├── regression/
├── safety/
├── tool_use/
├── artifact/
├── multi_turn/
├── network/
├── memory/
└── workspace/
```

---

## 25.2 EvalCase

```yaml
id: string
version: string

prompt: string
messages: []
input_files: []        # 落地为 RoutingCase.files（§11.4），相对仓库根

expected:
  skills: []
  forbidden_skills: []
  tools: []
  forbidden_tools: []
  output_schema: null
  assertions: []

environment_overrides: {}

tags: []
severity: low | medium | high | critical   # critical → Critical Skill Miss Rate（§21.3）
```

`tags` 只允许 `positive` / `ambiguous` / `multi-skill` / `no-skill` 四个枚举 ——
自由文本标签没法聚合，写了等于没写。自动生成器产出的 case 也必须遵守。

---

## 25.3 首个路由基准集

```text
20 个常用 Skill
100 个问题
每个 Skill 至少 4 个正向问题
至少 20 个 No-Skill 问题
至少 20 个模糊或相似 Skill 问题
每题独立运行 3 次
```

---

## 25.4 验收标准

* 每个 Skill 有正向用例。
* 每个 Skill 有反向或禁止场景。
* 包含 No-Skill 测试。
* 包含相似 Skill 冲突测试。
* 包含多 Skill 组合测试。
* 测试集必须版本化。
* 历史版本不得覆盖。
* 普通路由题不得直接泄漏 Skill 名称。
* 多轮 Case 必须明确 Turn 顺序。

---

# 26. 推荐仓库结构

```text
skill-eval/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── uv.lock
├── docker-compose.yml
│
├── contracts/
│   ├── skill.py
│   ├── experiment.py
│   ├── invocation.py
│   ├── result.py
│   ├── trace.py
│   ├── evaluation.py
│   └── schemas/
│
├── registry/
│   ├── skills.py
│   ├── tools.py
│   └── versions.py
│
├── adapters/
│   ├── runtimes/
│   │   ├── base.py
│   │   └── openclaw.py
│   ├── models/
│   │   └── litellm.py
│   ├── evaluators/
│   │   ├── skill_creator.py
│   │   ├── deepeval.py
│   │   ├── ragas.py
│   │   └── mlflow.py
│   └── formats/
│       ├── anthropic_eval.py
│       └── native_eval.py
│
├── orchestration/
│   ├── matrix.py
│   ├── environment.py
│   ├── runner.py
│   ├── approvals.py
│   └── isolation.py
│
├── evaluators/
│   ├── registry.py
│   ├── pipeline.py
│   ├── assertions.py
│   ├── routing.py
│   ├── tools.py
│   ├── artifacts.py
│   ├── conversation.py
│   ├── safety.py
│   ├── cost.py
│   └── regression.py
│
├── tracing/
│   ├── events.py
│   ├── normalizer.py
│   ├── jsonl.py
│   └── otel.py
│
├── ui/
│   └── streamlit_app.py
│
├── evals/
├── fixtures/
├── subjects/
├── outputs/
├── tests/
└── scripts/
```

---

# 27. 实现节点

> ⚠️ **本节的 Milestone 0–10 是最初的瀑布排期，已被 §★★★「阶段计划」取代。**
> 保留它是因为每个 Milestone 的**交付物与验收条目**仍然有效，可以当 checklist 查；
> 但**顺序与优先级以 §★★★ 的 P0–P7 为准**（实际推进不是 0→10 的瀑布）。

## Milestone 0：项目骨架

交付：

* Python 项目
* 依赖锁定
* CI
* lint
* type check
* pytest
* AGENTS.md

验收：

* 全新环境一条命令安装。
* 测试可运行。
* 依赖版本固定。

---

## Milestone 1：Contract

交付：

* SkillSpec
* ExperimentSpec
* InvocationRequest
* RunResult
* TraceEvent
* EvalCase
* EvaluationResult
* JSON Schema

验收：

* 满足 N0 全部标准。

---

## Milestone 2：OpenClaw Runtime

交付：

* Docker 启动
* Skill 注入
* Tool 注入
* 文件挂载
* Workspace 读写
* 单轮运行
* 标准 RunResult

验收：

* 同一任务可运行 No-Skill 和 With-Skill。
* 两次均产生标准 RunResult。

---

## Milestone 3：多轮与 Workspace

交付：

* Multi-turn Session
* 同 Run 文件持续
* Turn-level Trace
* Session 清理

验收：

* 后一轮可读取前一轮文件。
* 不同 Repeat 无状态泄漏。

---

## Milestone 4：Routing Eval

交付：

* Routing-only
* 20 Skill Catalog
* No-Skill Rejection
* Confusion Matrix

验收：

* Routing-only 不加载完整 Skill。
* Routing-only 不调用 Tool。
* 路由基准可以完整运行。

---

## Milestone 5：Evaluator Registry

交付：

* Evaluator 接口
* SchemaEvaluator
* AssertionEvaluator
* RoutingEvaluator
* ToolCallEvaluator
* ArtifactEvaluator
* CostEvaluator

验收：

* Evaluator 可配置组合。
* Evaluator 不依赖 OpenClaw 对象。

---

## Milestone 6：skill-creator 兼容

交付：

* Anthropic Eval 导入
* Skill Creator Adapter
* 可选 Analyzer
* 可选 Comparator

验收：

* 一个现有 Anthropic Skill 可以不修改地导入测试。
* 关闭该 Adapter 后核心系统仍正常运行。

---

## Milestone 7：版本回归

交付：

* No-Skill、V1、V2 矩阵
* 多次重复
* Delta
* 方差
* Regression Gate

验收：

* 自动输出 V2 是否达到发布门槛。

---

## Milestone 8：网络策略

交付：

* disabled
* mock
* allowlist
* 网络 Trace

验收：

* 三种模式均可自动验证。
* No-Skill、V1、V2 使用相同网络数据。

---

## Milestone 9：配置表

交付：

* Streamlit Data Editor
* YAML/CSV 导入导出
* CLI 执行
* 运行任务预览

验收：

* 无需修改代码即可配置实验。
* UI 与 CLI 使用同一个 Contract。

---

## Milestone 10：多模型

交付：

* 统一模型 Adapter
* 至少三个 Provider
* Usage 和错误标准化

验收：

* 同一实验可以在三个模型上运行。
* Evaluator 不需要 Provider 特殊逻辑。

---

## 后续节点

后续再实现：

* Persistent Memory Eval
* 真实 Webhook
* 长时间运行任务
* 跨 Session 工作流
* 复杂多轮 Judge
* 自动失败案例聚类
* 自动 Skill 修改建议
* Claude Code Runtime
* Codex Runtime

---

# 28. MVP Definition of Done

MVP 必须同时满足：

* OpenClaw 未被 Fork 修改。
* skill-creator 不是强制核心依赖。
* 支持 Skill、Tool、文件和模型配置。
* 支持 Docker Workspace 内正常读写。
* 支持输入只读挂载。
* 支持输出 Artifact。
* 支持单轮和多轮 Session。
* 支持 network disabled、mock、allowlist。
* 支持 Routing-only。
* 支持 Full Skill。
* 支持 No-Skill、V1、V2。
* 支持约 20 个 Skill 的路由测试。
* 支持多次独立运行。
* 支持至少三个模型 Provider。
* 每个 Run 返回标准 RunResult。
* 每个 Run 有配置、版本和输入来源。
* Evaluator 可以组装。
* 确定性测试优先。
* 可选兼容 Anthropic Eval。
* 支持 Pairwise Blind Judge。
* 支持 Token、成本、延迟和 Tool Call 统计。
* 支持 JSON 和 HTML 报告。
* 默认不启用跨 Run Persistent Memory。
* 默认不启用真实 Webhook。
* 不记录隐藏思维链。
* 高风险动作有审批留痕。
* 任意失败 Run 可以单独重跑。
* CI 可以运行最小回归集。

---

# 29. Coding Agent 工作规则

开发 Agent 执行任务时必须：

1. 先搜索仓库已有实现。
2. 检查成熟依赖是否已有对应能力。
3. 优先增加 Adapter。
4. 不直接修改上游组件。
5. 修改 Contract 时同步更新 Schema。
6. 新增配置字段时同步更新 CLI、UI 和测试。
7. 新增 TraceEvent 时保持向后兼容。
8. 不在日志中输出 Secret。
9. 不增加新 Agent Framework。
10. 不增加自研长期 Memory。
11. 不增加真实 Webhook，除非任务明确要求。
12. 不用 Judge 替代确定性 Assertion。
13. 不要求模型输出隐藏思维链。
14. 每个功能必须带验收测试。
15. Mock 只能用于外部依赖，不得冒充核心功能完成。
16. 修改 Runtime Adapter 后必须运行 Contract Tests。
17. 修改 Skill 加载逻辑后必须运行 Routing Eval。
18. 修改 Evaluator 后必须保留原始证据。
19. 修改版本 Gate 后必须记录原因。
20. 无法满足验收标准时必须明确标记未完成。
21. **开发期验证一次跑 10–20 题,不要写 100+ 题**(见 §★★「当前阶段」)。现在是在开发
    eval 系统,不是在评估 skill;§25.3 的基准规模等系统稳定后再用。
22. 默认模型 `qwen3.7-max-2026-05-17`,切换对照用 `glm-5.1`,两者共用 DashScope 端点与 key。
23. **代码改动一律落在 skillEval 仓库**。确实需要改 OpenClaw 时:不 fork、不在本地直接改源码,
    把改动(补丁片段 / 接口定义 / 配置)写进 [OPENCLAW.md](OPENCLAW.md) §9 的改动登记,
    注明目标文件、目标版本和原因,后续整段粘回上游。改动登记为空 = 当前对 OpenClaw 零侵入(§3.1)。
24. 定阶段、排优先级、判断"下一步做什么"时以 §★★★ 为准;§27 的 Milestone 顺序已过期。
25. 新增实现前必须执行 §4.0 的复用审计；优先“直接调用 → 薄 Adapter → 自研缺口”，
    不得默认先自建框架。
26. **每个 `python -m` 入口必须有一条覆盖 `main()` 的冒烟测试**（可 mock 掉模型调用，
    但必须真正走 argparse → 参数装配 → 落盘这一整条）。只测内部函数挡不住「参数传错
    位置」这类错误 —— 实测 192 个测试全绿，而 `workflows.gen_cases` 的 CLI 第一行就
    `TypeError`，且已进了 main。
27. **文档里描述的能力必须真的能跑。** 用户文档（README / RUNBOOK / AUTHORING）只写
    已实现的东西；设计中但未实现的写进 AGENTS.md 并在能力表里标 ❌/🚧。
    踩过：RUNBOOK 承诺 `evals/fixtures/` 「只读挂载」，而代码里 `fixtures` 零命中。
28. **报错要指向真正的原因和下一步动作。** 「找不到 openclaw，安装：npm i -g openclaw」
    在 openclaw 已装、只是 node 版本不对时是错误引导。能自查的先自查，再给建议。

---

# 30. 最终设计原则

```text
复用成熟组件
→ 不修改 OpenClaw
→ 标准化输入输出
→ Runtime 与 Eval 解耦
→ Skill Metadata 渐进加载
→ Workspace 可写但运行隔离
→ 多轮 Session 属于基础能力
→ Persistent Memory 属于可选能力
→ 网络能力可配置
→ 评测库可插拔组装
→ 确定性评估优先
→ 用相对增益判断 Skill 价值
```
