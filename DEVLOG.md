# 开发记录

倒序。每条记：做了什么、为什么、踩了什么坑。架构规范见 [AGENTS.md](AGENTS.md)，用法见 [README.md](README.md)。

---

## 2026-08-05 (UX-02) · SkillHub `_meta.json` 让失败后的 init 永远无法续跑

第 5 次更新后的普通用户模拟里，`meeting-and-brief@1.5.0` 明明与已有 V1 byte-identical，
`pipeline init` 却把 snapshot 判成 conflict；生成调用失败后的提示还承诺“重跑同一命令会安全
复用”，使恢复路径与真实行为相反。

根因是比较口径不对称：source manifest 包含 SkillHub 根目录 `_meta.json`，destination
manifest 排除了 skillEval 自己覆盖写入的同名文件。现在 `workflows.import_skill` 提供唯一的
`snapshot_content_manifest()`，source/destination 都使用它；只排除根目录控制元数据，嵌套
同名文件仍算普通内容。import provenance 额外保存实际评测内容 manifest hash 和上游 meta
hash。真实模拟留下的两份临时 snapshot 已回放为 `reusable`；修改 `SKILL.md` 仍明确
`conflict`。

这次只修幂等/续跑契约，不顺手混入生成器 repair、默认模型或 Docker 提示；它们按 handoff
的更新 7–9 分开提交。

**验证基线：368 passed / 1 skipped。**

---

## 2026-07-28 (UX-01) · 拿一个真下载的 skill 走一遍全流程，把踩到的 11 个坑全修了

**怎么发现的**：不写代码、只当用户 —— 从 skillhub 装 `meeting-and-brief`（1.5.0，622 行
SKILL.md），然后照 README 走完「生成题 → 小规模验 → 加难题 → 30 题 → full eval →
LLM judge → 改 metadata 出 V2 → 同题复验」。轨迹本身是通的，但**入口和文档**处处硌人。

### 最贵的一条：CLI 第一行就崩，而 192 个测试全绿

`python -m workflows.gen_cases` 直接 `TypeError: generate_batch() got an unexpected
keyword argument 'include_skill_ids'` —— 参数放错了函数（属于 `build_suite_draft`），
且已经随 `7b7f2ae` 进了 main。原因是**测试全都直接调内部函数，没有一条走 argparse →
参数装配 → 落盘**。修参数只花一分钟，真正的修复是 `tests/test_cli_entrypoints.py`：
8 个 `python -m` 入口各一条 `--help` + 关键入口的完整 main() 冒烟（AGENTS.md §29.26）。

写这条测试时又逮到一个：`generate_batch(completion=call_litellm)` 的默认值在 def 时
就绑死了，测试替换模块属性完全不生效 —— 冒烟测试差点变成"每跑一次 CI 就真打一次 API"
（第一次跑就花了 51 秒）。main() 改成显式传 `completion=call_litellm`。

### 文档承诺了代码里没有的东西

RUNBOOK §1 白纸黑字写着输入文件"只读挂载"，而 `grep -rn fixtures --include=*.py` **零命中**。
对 `meeting-and-brief` 这种「输入就是一份文字稿」的 skill，只能把整段文字稿内联进 prompt，
跟真实用法差得远。补上 `RoutingCase.files` → `InvocationRequest.input_files` →
`environments/filesystem.stage_input_files`（按 basename 复制进 workspace + chmod 只读），
缺文件在 `pipeline plan` 阶段就拦。端到端验证：同一道题从 `tool_hit 0%`（模型只会反问）
变成 `read+write 2/2、落 4 个产物`。

同类还有 README/RUNBOOK 教的 V1/V2 对照方法：**改 `skills.dir`**。照做的话 catalog 会从
8 个 skill 缩成 1 个，变成「单选题 vs 八选一」，delta 完全不可归因，且没有任何提示。
正确做法是 `skills.overlay`，而这件事只写在 `contracts/skill.py` 的 docstring 里 ——
用户读的三份文档里只有源码注释是对的。

### overlay 是目录级替换，而仓库自带一个陷阱

按惯例把 V2 放进现成的 `skills_v2/`，结果那里还躺着上个实验的 `pdf/` 和
`interactive-architecture-diagram/` —— 一次换掉 3 个 skill。`plan` 把 8 个 hash 都打出来了，
但**没有一句提示**，要靠人肉比对 64 位 hash 才发现。现在：`skills.target` 声明本次要动谁，
`overlay_warnings()` 报出目标之外的变更；没声明 target 且一次换掉 >1 个时直接告警。

### 自动出题在 rej 上会产出自相矛盾的 gold

`--include-neighbors` 时，生成器站在"目标 skill 该不该激活"的视角出 rej 题，30 题里有 3 道
gold 是错的：「Word 字体统一改微软雅黑 + 自动目录」标 `∅`，而 catalog 里明明有 `docx`。
照单全收的话，模型答对反而被记成误激活，`false_activation` 结构性虚高，**整批结论是反的**。
`REVIEW.md` 对这三道一个字没提。这是语义问题、代码判不了，所以改成强制模型逐题写
`rejection_notes`（缺一条就拒绝生成），并把它们顶到 `REVIEW.md` 最前面。

### severity 定义了却没人读

`critical` 在契约里校验、在生成器 prompt 里要求、AUTHORING 说它"对应 README 的
Critical Skill Miss Rate" —— 而 README 没这个指标，评分代码里 `severity` 一次都没被读过。
写题的人以为标了 critical 就多一层保护，其实跟 medium 一模一样。现在 routing 与 full
两侧都算 `critical_miss`，可进 gate。实测立刻起作用：同一批题 qwen3.7-max 0.0% PASS，
qwen-flash 16.7% FAIL。

### 其余五条

* **纯文档字段进了 `config_hash`**：只改 suite 的 `description` 一句话，hash 就变，
  工具随即报「配置动过、不可直接比」。假警报会训练用户忽略真警报。
  `suite_id` / `suite_version` / `description` 移出 hash（仍进快照）。
  ⚠️ **这是破坏性变更**：本次之后所有新 run 的 `config_hash` 都与历史归档不同。
* **judge 开箱用不了**：`.env.example` 给了 `JUDGE_*`，真实 `.env` 里往往没有，
  照 README 跑必然失败，且报错是 provider 那边一句难懂的认证错误。现在外发前先查，
  并列出当前已配好的 key 变量供改指向。
* **"路由不能用 judge"藏在第二层**：先让你去配 judge，配完才告诉你路由 run 压根不能判。
  把适用性检查提到凭据检查前面。
* **`--healthcheck` 说"没装"其实装了**：openclaw 在 nvm 的 node v24 下，PATH 上的 node 是
  v22。原提示是"安装：npm i -g openclaw"，照做只会再装一遍。现在先扫 nvm 再给建议，
  直接把 `node_bin:` 那一行打出来。
* **长跑零进度 + 输出顺序反了**：180 个任务跑 6 分钟一个字不吭（`grade.py` 一直有进度，
  `run_routing` 反而没有）；`pipeline run` 管道重定向时结果先于 plan 出现。
  逐题进度写 stderr，父进程 print 加 flush。
* **`compare_runs` 固定写 `outputs/compare.html`**：连着比两个模型，第二次静默覆盖第一次。
  文件名改成带两个 run。

### 顺带发现的 skill 侧真问题（评测本身的产出）

* V1 的 description 完全没提正文 §2.5 那一整节 ASR 校对能力 —— routing-only 只读
  frontmatter，**正文里的能力等于不存在**。模型原话：「现有 skill 无专门针对纯文本校对的
  模块，故不激活」。补进 description + triggers 后该题 0% → 100%。
* V1 的 exclusions 是空的，`qwen-flash` 拒答率 0%、误激活 100%（翻译、破冰问题、
  方法论咨询全被吞）。补 5 条 exclusions 后拒答率 38.1% → 66.7%。
* 但新 exclusions 写宽了：「一对一深度访谈让位 interview-insight」把「焦点小组座谈会」
  也扫进去了（模型原话直接引用了这条排除），`amb-02` 100% → 0%。**改 metadata 是有代价的，
  这正是同题复验存在的意义。**
* judge 抓到两处只看确定性断言发现不了的问题：模型编造了会议日期 `2026-07-27`
  （原文只说"昨天"）；把"张付总"静默改成"张副总"，**违反 skill 自己写的红线**
  （姓名职务只标不改）。

**验证基线：216 passed**（新增 `test_cli_entrypoints.py` 13 条 + `test_ux_fixes.py` 11 条）。

---

## 2026-07-28 (P6 收尾) · Docker 端到端跑通：agent 真的在容器里跑

v0 那版容器起得来、mount 对、清理干净，但**没有任何 suite 在用它**，而且真接上去也跑不动。
补掉四个断点，现在 `full_deliverable_v1_docker.yaml` 从 plan 到 healthcheck 全绿。

**① 固定镜像不存在。** 加 `environments/openclaw.Dockerfile`（node:24-slim + 写死版本的
openclaw + qwen provider 插件）。

**② 本地 build 的镜像永远进不了 suite。** 校验写的是 `"@sha256:" in image`，可本地 build 的
镜像根本没有 registry digest，只有 image ID。不 push 到 registry 就没法用 —— 而 image ID
同样是内容寻址。改成认 `(^|@)sha256:<64位>`，两种都收（`contracts.PINNED_IMAGE`，
suite 和 backend 共用一个，免得两处漂移）。

**③ 凭据进不了容器。** 容器默认是干净的，agent 拿不到 key 只会在里面 401，而 401 会被
`classify_error` 记成 `network` —— 看上去像上游抖动，其实是 key 压根没进去。加
`environment.env_passthrough`：只写变量名，值在容器创建时注入（`docker exec` 继承，
所以 key 不出现在任何命令行上）；支持 `容器里的名字=宿主机的名字` 改名，因为容器里
onboard 只认 `QWEN_API_KEY` 而 `.env` 的真相源叫 `DASHSCOPE_API_KEY`。
变量**名**进 fingerprint，值绝不进 —— 换 key 不该改变 `config_hash`，换传哪些变量才该。
缺变量在 healthcheck 就拦，不等跑到一半。

**④ healthcheck 探错了机器。** 它探的是宿主机装没装 openclaw，容器模式下两个方向都会
给假答案：宿主机没装报不健康（其实容器里有），宿主机装了报健康（其实容器里没凭据）。
`healthcheck(environment=None)` 加一个可选参数，容器模式下真起容器、真 onboard、
真跑一次 agent。

**最大的坑：onboard 不能放进 Dockerfile。** 本来想 build 时用占位 key onboard、运行时用真
key 覆盖 —— 实测 401。`onboard` 把 key 写进 profile 的 auth store，而 **auth store 优先于
环境变量**，OPENCLAW.md §4 那句「运行时认三个 env 变量」只在 auth store 没记录时成立。
但 onboard 又跳不掉：它还注册 `qwen/*` 模型命名空间，不跑就
`FailoverError: Unknown model`。结论是 onboard 必须带真 key 在运行时做（约 6s/容器），
好处是真 key 一层都不进镜像。

顺带解掉一个遗留：容器 profile 每次全新，模型必须由 `runtime_options.model` 显式指定，
而它进 fingerprint —— 本机模式下「模型选择藏在 OpenClaw 自己配置里、不可追溯」那条不再适用。

验收：186 tests（含一个真起容器的集成测试，验只读 mount 真拒写、workspace 真回传、
凭据真按改名注入、容器和临时目录真清理；没镜像自动 skip）。
`pipeline plan --healthcheck` 报 `environment=healthy; runtime=healthy (容器内探通)`。
**还没跑真实 eval 数字** —— 那要花 API 预算且会把 skill 正文发给模型，等明确授权。

---

## 2026-07-28 · 生产上下文路由 + Docker Environment Backend v0

路由被明确拆成两级输入策略，而不是用生产上下文替换原来的简单题：

* `direct`（默认）：`N 个 skill metadata + 当前问题`，先快速调好 description/triggers；
* `production_context`：第一阶段达标后，再加入 role、长上下文、历史消息和 builtin/MCP
  tool 目录，专门测“用户只说继续，模型能否从前文恢复真正要用的 skill”。

两者通过 `create_routing_input(strategy, **options)` 工厂创建，共用同一个 LiteLLM runtime。
历史 tool result 被稳定渲染成文本，不触发真实 tool calling；生产策略各上下文维度还能
单独关闭做消融。

同时把 Environment 从 Runtime 轴拆出：`local` / `docker` 都实现同一
`EnvironmentBackend.prepared()`，prepared request 仍交给原来的
`RuntimeAdapter.run() -> RunResult`，没有修改评分边界。Docker v0 直接复用 Docker SDK，
实现固定 digest、独立容器/workspace、skill 只读 mount、disabled/full 网络、CPU/内存与
退出清理。用本机已有固定 digest 镜像做真实 smoke，workspace 写入成功、只读 mount 写入
失败、容器退出后不存在。

当前限制：mock/allowlist 网络模式与 OpenClaw 专用固定镜像未完成。6 道合成上下文题的
mock 链路已通；真实 Qwen 调用因包含仓库 skill metadata，被权限审查要求新的明确外发授权，
所以尚无模型效果数字。

## 2026-07-28 (P2 启动) · 复用优先 + 自动出题薄切片

先在 `AGENTS.md` §4.0 固化“直接调用 → 薄 Adapter → 自研缺口”的复用审计。
联网核对后，OpenClaw-RL 可复用的是 serving / trajectory / judge / improvement 解耦与
next-state 反馈思路，不引入它依赖 Slime/GPU/PRM/Trainer 的 RL 训练栈；Claw-Eval 的
人工验证 task / fixture / rubric 结构留作后续 dataset adapter 参考。

P2 没有新建 CaseGenerator 框架，只加一层薄胶水：

* `workflows/gen_cases.py` 直接复用 LiteLLM、Pydantic 和现有 Suite Contract；
* `contracts/evalcase.py` 增加跨题坏题检查与按 case id 的 prompt/gold 差异解释；
* 输出固定为 `DRAFT`，写完停止，人工审核后才允许进入 gate；
* generator model / params / prompt hash / skill hash / acceptance hash 进入数据集头部。

用户明确授权后完成真实 ctxweave 生成与两轮人审复验。v0.1 自动集因 multi 偏多、rej
直接写“不需要画图”而得到错误的 `FAIL/PASS/PASS`；原因和逐题差异落在
`evals/analysis/ctxweave_generated_v1.0_consistency.md`。generator v0.2 把 10 题配比改成
`4/3/2/1` 并禁止 rej 泄漏否定提示，最终 v1.1 得到：

```text
none 56.7% FAIL → V1 80.0% FAIL → V2 100.0% PASS
```

排序与 gate 结论均和人工集一致，P2 完成。真实生成还顺手抓出两个契约 bug：
ID scope 缩写未校验、scope 正则不接受带连字符的真实 skill_id，均已补测试。

P3 随后启动：`workflows/suggest.py` 复用 LiteLLM + Pydantic，按 OpenClaw-RL 的 next-state 思路把
`RunResult.raw_output` 当改进信号；系统故障不进建议，quote 必须是原文子串，失败 case
必须被聚类覆盖，默认只写 JSON、绝不改 skill。真实 PDF V1 建议调用属于新的外发 payload，
未获单独授权，权限审查已拦下；当前 95 tests passed。

---

## 2026-07-28 (P1) · Full eval 的第一个对照数字 + 错误分类

管道之前只是「通了」，现在有**数字**了。三件事：确定性断言评分、四类错误分类、
以及一路挖出来的三个真 bug。

### 1. 三个新维度，零 LLM 判定（`workflows/score_full.py`）

任务完成度 / 产物命中率（存在 + 非空 + MIME）/ tool 命中率。全是代码能确定的事 ——
§3.4 要求确定性优先，语义层等 P4 的 Judge，**不提前混进来**。

两条不肯让步的规矩：

* **不适用的维度记 `N/A`，不记 0**（★★★ ③）。可判维度全为 N/A 时 gate 报
  「无法判定」而**不是 PASS** —— 那是「没测」，不是「通过」。
* **系统故障不进分母。** `runtime`/`network`/`harness` 的失败单列并大声打印。
  留在分母里，就等于「我们的 CLI 崩了」→「这个 skill 不行」。

### 2. `RunResult.error_kind`：四类失败分开计（★★★ ⑥）

`task` 模型没做对 ｜ `runtime` CLI 挂了 ｜ `network` 上游 API 挂了 ｜ `harness` 我们自己挂了。

四类都**真实构造并验证**过：断网（指向没人监听的端口）→ `network`；
杀 CLI（可执行文件不存在）→ `runtime`；模型不回 JSON → `task`；
坏 suite（明文 secret）→ **运行前就被拒绝，不产生 RunResult**（如实记录，不假装它有归类）。

有 `error` 却没归类时默认算 `harness` —— 宁可先怀疑评测系统，也不要把自己的 bug
记到被测 skill 头上。

配套：**单个 case 抛异常不中断整批**。编排层兜住 adapter 违约的情况，
一道题炸掉不该让另外 N-1 道白跑。

### 3. 挖出来的三个 bug（都进了 HANDOFF 坑表）

| bug | 为什么难发现 |
| --- | --- |
| `Invalid session ID` | matrix 的 `execution_id` 带时区 `+0200`，OpenClaw 不收 `+`。**healthcheck 用写死的 `skilleval-health` 探得通，真跑每题必挂** —— 探针必须用真实格式的 id |
| artifact 混进脚手架 | OpenClaw 进空 workspace 会自铺 8 个文件（`AGENTS.md`/`SOUL.md`…），全被算成产物，命中率的分母是假的 |
| 断网被记成 harness | litellm 把连不上上游包装成 `InternalServerError`，**类名里一个网络词都没有**。改成先按**来源模块**判（litellm/openai/httpx…），类名只作兜底 |

第三个正是 `error_kind` 想防的事，结果自己先踩了一次。

### 4. 两处偏离原计划

* **新建 `skills_full/`。** `skills/` 下除 contextweave 外全是 stub（正文写着"略"），
  full 模式下正文不产生任何行为差异，做不出对照。放独立目录是为了不动 `skills/` ——
  否则所有历史路由基准的 `config_hash` 一起作废（坑表里那条「装新 skill 污染旧基准」）。
* **No-Skill 基线用 `mode: full` + `exclude`，不是 `mode: none`。**
  写 `none` 会让 adapter 跳过产物追踪，artifacts 恒为空、命中率恒为 0 ——
  那是「我们没在看」，不是「模型没做」。又一例「静默变空」。

### 5. 跑出来才发现的两个测量错误（v1.0 → v1.1）

第一版数字出来后有两处对不上，查下来都是**我们量错了**，不是模型的问题：

1. **`loaded_skills` ≠ 模型激活了 skill。** 它是 OpenClaw **注入进 system prompt**
   的目录。实测拒答题里模型什么都没做（tools=[]、files=[]），目标 skill 照样在
   `loaded_skills` 里。所以我加的 `skill_load` 维度测的是**可见性**不是激活 ——
   **从 gate 和任务完成度里摘掉**，降级成 `skill_injected` 体检项
   （它仍有用：能抓「extraDirs 静默失效」那类坑）。
2. **拒答题三个维度全是 N/A**，等于只做了冒烟 —— 模型写一屋子文件也算它过。
   补了 `forbid_artifacts: true`：**留空的语义是「该维度 N/A」，不能拿它表达「必须为零」**，
   两者混用会让「没让你落文件」和「没在看这个维度」变成同一个数字。

改了断言就按 AUTHORING §4 **bump 数据集到 v1.1 并重跑**，没有拿旧 runs 重新打分 ——
那会让 snapshot 里的 `dataset_hash` 和文件对不上，正是 §5① 要防的事。

---

## 2026-07-28 · 定最终验收标准 + 阶段计划 P0–P7（只动文档）

把「做完什么算完成」从脑子里搬到 [AGENTS.md](AGENTS.md) §★★★。零代码改动。

### 为什么现在做

已完成的东西（路由链路、runtime 工厂、OpenClaw、full 管道）**没有一个共同的终点**，
每次决定「下一步做什么」都在重新讨论。§27 的 Milestone 0–10 又是最初的瀑布排期，
顺序早就不是实际推进的顺序了 —— 照着它推会先做 Docker 再做评分，正好反了。

### 六条最终验收标准

① 最小输入 → 自动出题 → 自动改进 ｜ ② 四类组件可插拔 ｜ ③ 分层评价（确定性优先）
｜ ④ Runtime Adapter 与 Environment Backend 分开 ｜ ⑤ 开箱即跑 + 全过程可查 ｜ ⑥ 结果可信可复现

**对初版草案改了四处**，都是因为原表述「通过了也不能信」：

| 原表述 | 问题 | 改成 |
| --- | --- | --- |
| 「自动生成题目」 | 系统跑通 ≠ 题是对的，模型生成的 gold 不是真值 | 加可测判据：自动生成集与人工基准集**给出相同 gate 结论** |
| 「用模型验证各维度」 | 未校准的 judge 分只是另一个随机数 | Judge 必须先过 §22.6 校准门槛才准进 gate |
| 「跑多轮提出改进」 | 自动循环 = 烧钱不收敛 | 停止条件三选一：gate PASS / 预算上限 / 最大迭代数 |
| 「和 claude code skill eval 差不多」 | 无法判定「差不多」是多少 | 换成 `git diff --stat` 判据：新增实现只出现新文件 + 注册表一行 |

第 6 条（可信与可复现）是原五条最大的缺口，但**本仓库已完成约 70%** ——
`config_hash`、dataset hash、runtime fingerprint、污染检测都在。真正缺的是
**错误分类**（任务失败 / runtime 失败 / judge 失败 / 评测系统失败混在一起，
会把「系统崩了」误读成「skill 不行」）和「单 case 失败不中断整批」。

### 阶段计划

```text
P0 已完成 → P1 full eval 数字 → P2 自动出题 → P3 改进闭环
                    → P4 Evaluator 注册表+Judge → P5 多轮 → P6 Docker → P7 Viewer
```

排序有两条原则，都和直觉相反：

1. **先出数字，再抽象。** P4（注册表 + Judge）排在 P1/P3 之后 ——
   只有一个实现的接口是负债。等出现第三类评分逻辑，再看它们真正的共同点。
2. **先闭产品价值，再工程化。** P2/P3（出题 + 改进）只依赖已有的路由链路，
   不必等 full eval 和 Docker。Docker/Viewer 放最后，否则要跟着前面反复改。

每阶段写成**可勾选清单**（前置 / 交付物 / 验收标准 / 明确不做），一条一个判据。
「明确不做」和验收条目同样重要 —— P2 不做审核界面（`git diff` 就是审核门）、
P7 不做配置编辑器，这些写下来是为了防止下次又从头讨论一遍。

### 顺带定的一条规矩

**改 OpenClaw 的落点**（AGENTS.md §29 规则 23）：代码一律写在 skillEval；
真要改 OpenClaw，不 fork 也不在 `node_modules` 里直接改（下次 `npm i -g` 就没了、
换机器复现不了），把补丁写进 [OPENCLAW.md](OPENCLAW.md) §9 的改动登记，
一条改动一节，注明目标版本、目标文件、**为什么外面接不出来**、以及
**没有这个补丁时 skillEval 怎么退化**。最后一项不能省 —— 否则等于偷偷 fork 了一个
「必须打补丁才能用的 OpenClaw」。当前登记为空：对 OpenClaw 零侵入。

---

## 2026-07-28 (深夜) · Full eval 管道：产物归一化 + N4 环境注入

Full eval 的四个环节打通：**注入 skill → OpenClaw 加载 → 调 tool 执行 → 产物归一化**。
还差一个带验收标准的 full suite 才能出对照数字。

### 1. 契约：`ToolCall` / `Artifact`（§6.3、§11.4）

`RunResult` 新增 `tool_calls`、`artifacts`、`resolved_model` 三个字段。

**设计上的克制：给不出来的就留空，不编。** OpenClaw 只暴露聚合的 `toolSummary`
（用了哪些 tool、共几次、失败几次），拿不到逐次调用的参数与返回，所以
`ToolCall.arguments` 留 `None`；整轮的 `calls`/`failures` 放进 `usage`，
**不摊到某个 tool 上** —— 否则会造成"write 被调了 3 次"的错觉。

`Artifact` 按 §11.4 记 `sha256` / `size_bytes` / `mime_type`，路径存**相对 workspace**
（绝对路径换台机器就失效）。

`resolved_model` 顺手解决了一个已知遗留：OpenClaw 侧用哪个模型不在 suite 里、
不进 `config_hash`，以前只能靠人记，现在每条 RunResult 都带 `qwen/qwen3.5-plus`。

### 2. N4 Environment Resolver：`prepared()` 上下文管理器（§10）

给 `RuntimeAdapter` Protocol 加了环境布置能力，**工厂造出来的任何 runtime 都自带**：

```python
def run(self, request):
    with self.prepared(request):      # 进入布置，退出必然还原
        result = self._run_impl(request)
```

用 context manager 而不是 `setup()`/`teardown()` 一对方法，是因为它对异常天然安全。
实测强制抛异常后：`extraDirs` 已清空、staging 已删、零残留（§11.5「运行结束后按策略清理」）。

base 给默认空实现 —— litellm/mock 只发一次请求，没有 workspace 概念，不需要布置。

### 3. skill 注入：复制到 staging + `extraDirs`

| 方案 | 结论 |
| --- | --- |
| 写 `<workspace>/skills/` | ✗ 那是用户目录，跑 eval 不该往里塞 |
| 软链到 workspace | ✗ **OpenClaw 默认跳过解析到 root 外的软链**（除非配 `allowSymlinkTargets`） |
| 复制到 staging + `skills.load.extraDirs` | ✅ 零污染、不改原始 skill(§7.1)、精确可控 |

选复制而非直接把 `extraDirs` 指向 `skills/`，是因为 suite 的 `exclude`/`overlay`
已经在 `request.skills` 里解析完了 —— **staging 里放什么就是模型能看见什么**，
None/V1/V2 三种条件天然精确，不用再想别的过滤办法。

还原时**恢复原值**而不是无脑 `unset`：用户本来可能就配了 `extraDirs`，吃掉别人的配置很难查。

### 验证

```text
注入 6 个 skill → loaded_skills 18→24，我们的 6/6 全部加载 ✅
跑完      → extraDirs 已还原、staging 已删除、零残留 ✅
异常路径  → 同样干净 ✅
artifacts → data.csv (61B, text/csv) + q3_summary.md (125B, text/markdown)，均带 sha256 ✅
tool_calls→ ['read','write','exec']，usage 带 tool_calls_total/failures ✅
pytest 53 passed · mock 150/150
```

### 坑：两个都是"静默变空"，不报错

- **artifact 恒为空** —— `_snapshot()` 用绝对路径的 `f.parts` 排除 `.git`/`.openclaw`，
  而 workspace 本身就住在 `~/.openclaw/` 下面，于是**整个 workspace 的文件被全部过滤掉**。
  改成按相对 root 的路径判断。
- **软链注入不生效** —— OpenClaw 跳过外部软链时只写日志，`loaded_skills` 纹丝不动，
  看起来像"注入没写对"，实际是被安全策略挡了。

> 这两个和之前的 `payloads` 解析坑同类：**不崩溃、不报错，只是结果静默变空**。
> 在 eval 系统里这类 bug 最危险 —— 空结果看起来像"模型没做事"，
> 而不像"我们没读到"。凡是"某个列表恒为空"，先怀疑自己的解析/过滤。

---

## 2026-07-28 (晚) · OpenClaw runtime 打通 🦞

配置手册单独成篇：[OPENCLAW.md](OPENCLAW.md)（含新机器迁移清单与全部坑）。

### 做了什么

**1. 非交互式配通凭据 —— 推翻了「必须 TTY」的说法**

`configure` 确实要 TTY，但 `onboard` 有完整的非交互模式。四个参数缺一不可：

```bash
QWEN_API_KEY="$DASHSCOPE_API_KEY" openclaw --profile skilleval onboard \
  --non-interactive --accept-risk --auth-choice qwen-standard-api-key --skip-health
```

| 参数 | 不给会怎样 |
| --- | --- |
| `--non-interactive` | 要 TTY，脚本里跑不了 |
| `--accept-risk` | 前者的强制前置，直接拒绝 |
| `--skip-health` | 卡在等 Gateway → exit 1。**我们走 `agent --local`，根本不需要 Gateway** |
| `--auth-choice qwen-standard-api-key` | Standard(按量) 端点；Coding Plan 是另一个 choice，模型范围不同 |

前置：`openclaw --profile skilleval plugins install @openclaw/qwen-provider`
（qwen 不在 core 里，是官方外部插件）。

**2. 修掉一个静默吞结果的解析 bug**

第一次真实调用：

```
selected:  []                                    ← 解析出来是空
raw:       {"payloads":[{"text":"...\"selected_skills\":[\"pdf\"]..."}]}
                                                 ↑ 模型其实答对了
```

OpenClaw 2026.7.x 的输出是 `{"payloads":[{"text":...}]}`，而 `_extract_text()` 只认早期的
顶层 `text/reply/message`。**不报错、不崩溃，只让 `selected_skills` 静默变空** ——
比直接炸难发现得多。现在两代格式都认。

修完实测：「扫描版合同抽表格」→ `['pdf']`，「翻译成英文」→ `[]`。

**3. 验证了 key 的传递路径**

结论：**`.env` 是唯一手工真相源，OpenClaw 的 auth store 是派生副本。**

实测把 `DASHSCOPE_API_KEY` 从环境里拿掉后 OpenClaw 照样跑 → key 确实已进它自己的
sqlite auth store，这是 OpenClaw 的设计，绕不开。代价是 key 存两份，所以：

> **换 key 时改完 `.env` 必须重跑一次 onboard**，否则 OpenClaw 还在用旧 key。

变量名还有个错位：运行时认 `QWEN_API_KEY`/`MODELSTUDIO_API_KEY`/`DASHSCOPE_API_KEY` 三个，
但 **onboard 只认 `QWEN_API_KEY`** —— 所以上面命令要临时映射一下。
用环境变量而非 `--modelstudio-standard-api-key sk-xxx`，是为了别让 key 进 shell history。

### 验证四连（全绿）

```text
openclaw --version                          → OpenClaw 2026.7.1-2 (0790d9f)
openclaw --profile skilleval config validate → Config valid
openclaw ... agent --local --json           → 'OK'
workflows/run_routing.py --suite routing_openclaw --healthcheck → healthy=✓
```

③ 通而 ④ 不通 = 问题在 adapter 或 PATH 继承，不在 OpenClaw。

### 坑

- **`configure` 菜单里选 Workspace 只配工作目录，一个凭据都不配。** 配完看着成功，
  healthcheck 照样 `missing-provider-auth`。要找的是 model/provider 那一项。
- **`config set model.primary` 不足以让模型可用** —— 会报 `Unknown model`。模型还得注册进
  `agents.defaults.models` 表，那是 `onboard` 生成的。别手写 config 绕过 onboard。
- **session id 不能带中文/空格**，会报 `Invalid session ID`。adapter 生成的
  `skilleval-{case_id}-{repeat}` 天然安全（case_id 按 AUTHORING §1.2 就是 ASCII）。
- **Gateway 起不来不是问题**：`agent --local` 是进程内跑，`ECONNREFUSED 18789` 可无视。
- 顺带发现：qwen provider 里**连 GLM 都有**（`qwen/glm-5`、`qwen/glm-4.7`），
  以后做 OpenClaw runtime 的模型对照不用再装第二个 provider。

### 遗留

OpenClaw 侧用哪个模型（`openclaw models set`）**不在 suite 里，因此不进 `config_hash`**。
做 OpenClaw 的模型对照时要在 `models[].id` 里写清楚（它进目录名），否则事后分不清
那批结果用的什么模型。

---

## 2026-07-28 · N2 严格 Suite Contract

### 做了什么

- 新增 `contracts/suite.py`：`RoutingSuite` / model / skill / scoring 全部使用
  Pydantic strict + `extra=forbid`。
- runner 不再直接消费松散 YAML 字典；先校验，再用补齐默认值的 canonical config
  执行、计算 hash 和写快照。
- 运行前拒绝：未知字段、类型漂移、空/重复 model、重复 tool/metric/exclude、
  非法 gate、routing-only 配 tool、LiteLLM 漏 `model`、疑似明文 secret。
- 修正 `skills.mode: none`：现在实际暴露空 catalog；mock 在空 catalog 下稳定拒答。

### 验证

```text
仓库内 8 份 suite 严格解析                         → 全通过
pytest                                             → 53 passed
contracts/ + workflows/matrix.py coverage                    → 100%
routing_pdf_v1 --mock + score                      → 30/30，报告正常
```

---

## 2026-07-28 · N3 确定性 Matrix Builder

### 做了什么

- 新增 `workflows/matrix.py`，用 `itertools.product` 显式展开 `model × case × repeat`。
- 每个任务有稳定且唯一的 `request_id`；每次执行用新的 `execution_id` 生成
  `session_id`，同一配置重跑不会复用 OpenClaw 旧会话。
- `workflows/run_routing.py` 改为消费矩阵任务，不再自己嵌套 case/repeat 循环；快照新增
  `matrix_tasks` 与 `execution_id`。
- 非法矩阵（空 case/model、重复 ID、`repeats < 1`）在调用模型前直接拒绝，
  不静默丢任务。

### 验证

```text
pytest tests/                                      → 34 passed
contracts/ + workflows/matrix.py coverage                    → 100%
10,000 tasks 展开                                  → < 2s 验收通过
routing_pdf_v1 --mock + score_routing              → 30/30，评分与 HTML 正常
OpenClaw healthcheck                               → 找不到 openclaw（当前环境阻塞）
```

### 仍未完成

N3 目前覆盖 `model × case × repeat`。skill 配置/版本/runtime 仍由“一份 suite 一个配置”
表达，尚未在单 suite 内做完整多轴笛卡尔积；多轮也只把 session 隔离铺好，尚未循环 turn。

---

## 2026-07-28 (夜) · dataset hash + pdf 专项干净对照

### 1. 先修了两个契约测试失败

- `RunResult.model_dump_json()` 为兼容旧评分脚本会落 `ok`，但反序列化时 `extra=forbid`
  又拒绝 `ok`。现在用 validator 丢掉这个派生字段，旧 JSONL 可读回。
- `SkillMeta.source_path` 会把本地临时路径序列化出来，测试路径里刚好含“正文”导致误判。
  现在 `source_path` 仍保留在对象里，但默认不进 `model_dump_json()`，routing-only 更干净。

### 2. 补上一个真正的可复现性漏洞：dataset 内容 hash

原来 `config_hash` 只包含 suite 里的 dataset 路径，不包含 JSONL 内容。
这意味着改了题但文件名不变，hash 不变，旧结果看起来还能比 —— 这是错的。

现在 `workflows/run_routing.py` 会计算 `dataset_hash`：

```text
config_hash = suite(剔除 models) + 本 run model + mock + runtime_fp + skill hashes + dataset_hash
```

`dataset_hash` 同时写入 `config.snapshot.yaml`。新增测试：
`tests/test_run_routing.py::test_dataset_hash_进入_config_hash`。

### 3. 新增 pdf 10 题专项小集

文件：

- `evals/datasets/routing_pdf_v1.0.jsonl`
- `evals/suites/routing_pdf_v1.yaml`
- `evals/suites/routing_pdf_v2.yaml`

题量 10，开发期用，不替代 50 题全量基准。重点压这类边界：

```text
这份 PDF 合同里的违约条款有什么风险，帮我看看
读完这份 PDF 审计报告后，帮我判断公司现金流有没有问题
这篇 PDF 论文的核心观点是什么，帮我总结成三条
```

按当前项目定义，这些是内容分析/风险判断/观点总结，不是 PDF 格式操作，应拒答。

### 4. 干净 V1/V2 结果

`routing_pdf_v1.0` × `qwen3.7-max-2026-05-17` × 3 repeats：

| cfg | exact | no-skill rejection | false activation | gate |
| --- | ---: | ---: | ---: | --- |
| pdf-v1 | 70.0% | 0.0% | 100.0% | FAIL |
| pdf-v2 | 100.0% | 100.0% | 0.0% | PASS |

`workflows/compare_runs.py` 没报污染项，只显示 `[对比维度] suite_id/skillcfg`。
V1 的 9 次拒答题全部误激活到 `pdf`；V2 同题 9/9 正确拒答。

### 验证

```text
.venv/bin/python -m pytest tests/ -v  → 17 passed
mock pdf v1/v2                         → ok_runs=30/30
real pdf v1/v2                         → ok_runs=30/30, 30/30
```

---

## 2026-07-28 (下午) · 模型切换修正 + pdf V2 + 指纹粒度

### 1. 模型切换：我把端点配错了

上一轮给 glm-5.1 配了智谱独立端点（`GLM_BASE_URL`/`GLM_API_KEY`），结果因缺 key 跑不了。
实测发现 **DashScope 端点直接就能调 `glm-5.1`**（`glm-4-plus` 不行，5.1 在）：

```yaml
- id: glm-5.1
  model: openai/glm-5.1            # 同一个 DashScope 端点、同一把 key
  api_base_env: DASHSCOPE_BASE_URL
  api_key_env: DASHSCOPE_API_KEY
```

换模型只改 suite 里的 `model:` 一行，不碰 `.env`、不换 provider。

### 2. `config_hash` 粒度从 suite 级改成 per-model 级

改 glm 那一行会让**整个 suite** 的 hash 变，从而作废已跑好的 qwen 结果 —— 可 qwen 的结果
跟 glm 用什么端点毫无关系。粒度错了。

现在 hash = suite(**剔除 models 列表**) + 本 run 用的那一个 model 条目 + mock + runtime_fp + skills。
给 suite 新增模型、或改另一个模型的端点，都不再作废已跑模型的结果。

### 3. pdf V2：修掉「合同→pdf」误激活

V1 的病根：description 通篇在讲"PDF 文件"，模型据此按**文件类型**联想 ——
看到"合同"就想"合同通常是 PDF"。V2 把激活条件改写成**要对文件做什么操作**，
并在 exclusions 里点掉"分析内容/风险判断/咨询建议"。

```
none-rej-07「这份合同里的违约条款有什么风险」
  V1: [pdf] × 3      ← 3/3 一致误激活
  V2: []   × 3      ← 3/3 正确拒答
false_activation 10.0% → 0.0%
```

⚠️ **但这次对比是脏的**：`workflows/compare_runs.py` 自己报了 `[⚠️ 污染] runtime: v1=None`，
因为 v1 那批数据是模块化重构**之前**跑的，system prompt 不同。
所以表里 `type_multi +58.3pp` 是 prompt 变化的功劳，不是 pdf 描述的。
`none-rej-07` 那条逐题证据是可信的，聚合指标的 delta 不可全信 —— 要干净结论得用同一 prompt 重跑 v1。

### 4. 模型切换实测：V2 描述把模型差异抹平了

10 题 × 3 次，qwen3-max vs glm-5.1：

| skillcfg | qwen exact | glm exact | 说明 |
| --- | --- | --- | --- |
| none | 70.0% | 60.0% | 目标 skill 缺席，两个模型都乱套邻居（glm 更严重，正向题 0%） |
| v1 | 83.3% | 90.0% | 描述含糊时两模型表现**不一致**（glm 拒答反而更好） |
| v2 | 100.0% | 100.0% | **描述写清楚后，模型差异消失** |

这条比单个数字有意思：**skill 描述写得好不好，比换哪个模型影响更大**。
v1 下两个模型差 6.7pp 且方向不同，v2 下完全一致。

### 坑

- **`workflows/compare_runs.py` 比较"同条件换模型"时表是空的**：列名用了目录最后一段（skillcfg），
  两个 run 同 cfg 就撞名，pandas 静默覆盖。改成 `model/skillcfg`。
  （这已经是同一个 pandas 覆盖坑第二次咬人了 —— 第一次是 delta 列。）

---

## 2026-07-28 · 测试条件矩阵打通（none / v1 / v2 × 多模型）

### 目标

不是评某个 skill 好不好，而是**验证各种测试条件都能跑**：有 skill / 无 skill / V1 / 改一点的 V2，以及多模型切换。被测对象换成一个真实的第三方 skill。

### 做了什么

**1. 装了真实 skill 作为被测对象**

```bash
bash install.sh --cli-only --no-self-upgrade      # 只装 CLI
skillhub install contextweave-interactive-architecture --dir skills
```

- 用 `--cli-only`：默认模式会往 `~/.openclaw/workspace/skills/` 塞 skill 并改 OpenClaw 配置，
  会污染刚配好的 OpenClaw 环境。
- 用 `--no-self-upgrade`：CLI 自动升级会让"跑过的东西"不可复现。
- 坑：目录名 `contextweave-interactive-architecture`，但 frontmatter 里 `name:` 是
  `interactive-architecture-diagram`。我们的 loader 以 `name` 为 skill_id，**两者不一致**。
  真实 skill 常有这种情况，别假设目录名等于 skill_id。

**2. Skill 加载支持 overlay / exclude**（`contracts/skill.py`）

V1/V2 对照的关键是**不能改原始 skill**（§7.1）。方案：

| 条件 | 实现 | suite 写法 |
| --- | --- | --- |
| No-Skill 基线 | 从目录里剔除目标 skill | `exclude: [interactive-architecture-diagram]` |
| V1 | 原样 | （什么都不写） |
| V2 | 同名 skill 用另一目录的版本覆盖 | `overlay: skills_v2` |

`skills_v2/` 只放改过的那一个 skill，正文一字未动，**只改 frontmatter 的路由面**。

**3. No-Skill 基线要配套换 gold**

目标 skill 不在目录里时，那 4 道正向题的**正确行为从「激活」变成「拒答」**。
所以有两个数据集文件：`ctxweave_10_v1.0.jsonl` 和 `..._none.jsonl`，同题不同 gold。
这是 §18.3 的直接后果，不是偷懒复制。

**4. 多 provider 切换靠 suite，不改 .env**

`models:` 是列表，每个条目自带 `api_base_env` / `api_key_env`。加模型 = 加一个条目：

```yaml
models:
  - {id: qwen3-max, model: openai/qwen3-max, api_base_env: DASHSCOPE_BASE_URL, api_key_env: DASHSCOPE_API_KEY}
  - {id: glm-5.1,   model: openai/glm-5.1,   api_base_env: GLM_BASE_URL,       api_key_env: GLM_API_KEY}
```

配套加了**密钥体检**：跑之前检查每个模型的 key 在不在，缺的直接跳过并大声警告，
把 `skipped_models` 记进 config 快照。跳过是安全的（每个模型出独立目录），
但不记录就会变成"suite 写了 2 个模型、结果只有 1 个"的哑谜。

**5. 补上跨 run delta 报告**（`workflows/compare_runs.py`，此前一直是手写一行 python）

把配置差异分三类，只有第三类才是问题：

- `[对比维度]` suite_id / skillcfg —— 本来就该不同，那正是要比的
- `[随之联动]` dataset / n_skills —— No-Skill 基线下必然跟着变，不算污染
- `[⚠️ 污染]` model / runtime —— 一旦不同，delta 就混了两个原因，归因失效

### 结果：三级阶梯

10 题 × 3 次 × qwen3-max：

```
                      none      v1    Δ(v1)      v2    Δ(v2)
exact_set_match      70.0%   86.7%  +16.7pp  100.0%  +30.0pp
no_skill_rejection   57.1%   88.9%  +31.7pp  100.0%  +42.9pp
false_activation     42.9%   11.1%  -31.7pp    0.0%  -42.9pp
type_pos             25.0%   75.0%  +50.0pp  100.0%  +75.0pp
GATE                  FAIL    PASS             PASS
```

三个可行动的结论：

1. **No-Skill 基线证明这 skill 有价值。** 目标 skill 缺席时，模型不会老实拒答，
   而是硬套最近的邻居：「画架构图」→ `artifacts-builder` ×3，
   「流程图导出到 PPT」→ `pptx` ×3。正向题只有 25%。
2. **V1 的具体毛病抓到了。** 「流程图，要能导出到 PPT 里」被 3/3 判成
   `[目标skill, pptx]` 双选 —— V1 没有 exclusions，和 pptx 的边界是糊的。
   另有 1/3 把「微服务和单体架构各有什么优缺点」误激活（描述里的营销词勾出了"架构"联想）。
3. **V2 只改 frontmatter 就全对了。** 改动：description 从营销文案改成
   「什么时候该用它」，补 7 个 triggers、4 个 exclusions（明确排除交互式网页/看板/幻灯片/文档排版）。
   正文没动，所以这 +13.3pp 完全归因于路由面的措辞。

### 坑

- **workflows/compare_runs.py 的 delta 列一开始全错。** 每列共用同一个列名
  `f"Δ vs {base}"`，后写的覆盖先写的，导致 v1 的 delta 显示成 v2 的。
  改成每列独立的 `Δ(列名)`。教训：pandas 里同名列会静默覆盖，不报错。
- **GLM 没跑成** —— `.env` 里没有 `GLM_API_KEY`。suite 和体检逻辑都就位了，
  补上 key 就能跑，不用改任何代码。

---

## 2026-07-28 · 模块化重构 + OpenClaw 接入 + Runtime 工厂

### 做了什么

**1. 契约拆包**：`contracts.py` → `contracts/`（skill / evalcase / runtime 三个模块），
新增 `InvocationRequest` / `RunResult` / `RuntimeHealth` / `RuntimeCapabilities`。
`RoutingRun` 保留为 `RunResult` 的别名，旧 runs.jsonl 不会读不了。

**2. Runtime 抽象 + 注册表工厂**（`adapters/runtimes/`）

```
suite: runtime: openclaw → create_runtime("openclaw") → RuntimeAdapter → RunResult
```

编排层和评分层都只认 `RunResult`，不知道背后是谁。新增 runtime 只需
`@register("名字")`，工厂本身不用改。三个实现：`litellm` / `openclaw` / `mock`。

配套三个机制：

- `capabilities()` —— suite 要 runtime 不支持的能力（如让 litellm 跑 full），**跑前**就拒绝
- `healthcheck()` —— 不健康时 detail 要能直接告诉人怎么修
- `fingerprint()` —— **补上的一个漏洞**：system prompt 写在代码里，改它会改结果但
  `config_hash` 不变。现在由 adapter 自报内部常量（prompt hash、CLI 版本），一并进 hash。

**3. OpenClaw 非侵入接入**（`adapters/runtimes/openclaw.py`）

只调 `openclaw agent --local --json --session-id`，不 fork 不改核心（§3.1）。
`--session-id` 为多轮留好了口子。

### 坑

- **OpenClaw 会读 `~/.aws/credentials` 去探 Bedrock**，探不到就往 stderr 刷 AccessDenied。
  解法：adapter 把 `AWS_SHARED_CREDENTIALS_FILE` / `AWS_CONFIG_FILE` 指向 `/dev/null`。
- **升级 2026.2.15 → 2026.7.1-2 后 OpenClaw 直接不可用**：新版要求 node ≥22.22.3，
  机器上是 v22.12.0。装了 node 24 并 `nvm alias default 24`，卸掉 v22 下那个跑不了的。
- **openclaw 的 bin 是 `#!/usr/bin/env node` 脚本 —— 给绝对路径也没用**，
  它照样按 PATH 找 node。adapter 因此加了 `node_bin` 选项（把对应目录 prepend 到子进程 PATH）。
- **`_probe_version()` 会误判**：node 版本不合规时 openclaw 把错误提示打到 **stdout** 并退出，
  原来的实现直接把它当版本号存下来，healthcheck 就会把坏掉的实例报成健康。现在校验 returncode。
- **测 nvm default 生效与否，`zsh -l -c` 和 `zsh -i -c` 都不可靠**：前者非交互不读 `.zshrc`，
  后者会继承父 shell 钉死的 PATH。必须 `env -i` 清空环境才等价于真开一个新终端。
- **升级后接口零变化**：`--local` `--json` `--session-id` `--message` `--timeout` 和
  全局 `--profile` 全在，adapter 代码一行没改。auth store 从 JSON 换成了 SQLite，
  诊断逻辑改成两代都认。

### 未完成

OpenClaw 用自己的 auth store，不读环境变量的 key，且配置需要 TTY —— 必须在真终端里
跑一次 `openclaw --profile skilleval configure`。healthcheck 已能精确诊断到这一步。

> **⚠️ 此结论后被推翻（见本文最上方 2026-07-28 晚 条目）。**
> 「配置需要 TTY」只对 `configure` 成立，`onboard --non-interactive` 完全可用，
> 当天已全程非交互配通。保留原文是为了记录当时的认知过程。

---

## 2026-07-27 · 骨架 → 可复现实验

### 做了什么

- **配置从 `.env`/CLI 参数上收到 suite YAML**：会改变结果的东西必须能被 `config_hash` 捕获。
  脚本里不再留可调参数。密钥仍在 `.env`，suite 只写变量名。
- **测试集/结果全部版本化命名**：`evals/datasets/{kind}_{scope}_v{x.y}.jsonl`、
  `outputs/{dataset}__{model}__{skillcfg}/`。骨架期第二次跑会覆盖第一次，做不了 V1/V2 对照。
- **每个 run 目录自带 `config.snapshot.yaml`**：完整 suite + config_hash + skill content_hash。
- **补边界题（amb）制造区分度**：v1.0 的 30 题只有 pos/rej，6 个 skill 语义完全不重叠，
  指标恒为 100%，测不出任何东西。v1.1 加了 12 amb + 4 multi + 4 rej。
- 加了 Exact Skill-Set Match / 分题型准确率 / 发布门槛 PASS-FAIL。

### 结果

补题后 qwen3-max 跑出 **GATE FAIL**（false_activation 10% > 5%）：
`「这份合同里的违约条款有什么风险」被 3/3 一致误路由到 pdf`，理由是"合同通常是 PDF 格式"
—— 模型在按**文件类型联想**激活，而不是按**任务类型**。

### 坑

- **`workflows/score_routing.py` 默认取"最新 run"取错了目录**：用的是目录 mtime，
  但覆盖写文件不更新目录 mtime。改成按 `runs.jsonl` 的 mtime。
- **mock 跑和真实跑会算出相同的 `config_hash`**（同一个 suite），但两者结果绝不可比。
  把 `mock` 纳入 hash 输入。
