# skillEval

给 Agent Skill 做**可复现**的评测。它回答一个改 skill 时每天都要问、但凭感觉答不了的问题：

> 我把 skill 的 description / triggers 改了 —— 到底变好了还是变坏了？

现在能跑的是**路由评测**：给模型 N 个 skill 的元数据，看它在真实生产上下文里
选没选对。这不只是一句 prompt 分类题：case 可以同时带 role、长上下文、历史对话、
builtin tool 与 MCP tool 目录；最后一句可以只是“继续”“照刚才说的做”。skill 写得再好，
模型不能从前文恢复意图并激活它，就等于不存在。

---

## 跑起来

**前置只有 Python ≥ 3.11。** 不需要 Docker，不需要 node，不需要 OpenClaw，
第一步连 API key 都不用 —— 那些都是可选运行环境，用到再装（见[下面](#可选装什么才需要装)）。

```bash
git clone <this repo> && cd skillEval
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> Python 3.14 也能装，但 litellm 目前没有 cp314 wheel，pip 会现场编译，
> 第一次装要几分钟而不是几秒。想快就用 3.11–3.13。

**第一步：不花钱，先确认链路是通的。** 仓库自带一套完整示例
（3 个示例 skill + 14 道题 + 1 个 suite），`--mock` 用假数据跑，不联网、不要 key：

```bash
.venv/bin/python -m pipeline plan --suite evals/suites/example_routing.yaml --mock
.venv/bin/python -m pipeline run  --suite evals/suites/example_routing.yaml --mock --confirm
```

跑完你会看到完整报表和
`QUALITY VERDICT NOT EVALUATED（synthetic mock 只验证管道）`。mock runtime 是故意做成
~85% 命中 / ~80% 拒答的假数据（见 [adapters/runtimes/mock.py](adapters/runtimes/mock.py)）；
这些数字只用于确认评分管道没断，`gate_pass` 固定为 `null`，不能判断 skill 好坏。

**第二步：换成真模型。** 填 key，把 `--mock`去掉：

```bash
cp .env.example .env        # 然后把里面的 REPLACE_ME 换成你的真 key
.venv/bin/python -m pipeline plan --suite evals/suites/example_routing.yaml
.venv/bin/python -m pipeline run  --suite evals/suites/example_routing.yaml \
  --confirm --confirm-egress
```

示例 suite 默认用 DashScope 的 qwen。换别家就改 `evals/suites/example_routing.yaml`
的 `models:`，模型串照 LiteLLM 写，`api_key_env` 指向你在 `.env` 里加的变量名。
忘了替换占位符也不会白花钱：`plan` 会认出来并拒绝执行。

`pipeline plan` 不会发模型请求、不会写 `outputs/`。它会列出目标 endpoint、请求数和会发送的
数据类别；真实 run 必须单独给 `--confirm-egress`。`pipeline run` 还会自动重做本地
runtime/environment + endpoint DNS 预检，但这**不等于**已验证 provider 鉴权、模型可用性或额度。
模型、测试集、skill 版本、repeat、环境和 gate 仍然全部只从 suite 读取，避免 CLI 覆盖破坏可复现性。

**第三步：换成你自己的 skill。** 示例跑通之后才轮到真东西 —— 见[下一节](#换成你自己的-skill)。

跑测试（可选，确认环境没问题）：

```bash
.venv/bin/python -m pytest tests/ -q      # 需要 Docker 的那个用例会自动 skip
```

### 可选：装什么才需要装

| 你想干什么 | 还要装什么 |
| --- | --- |
| 路由评测（上面那套，也是主要用法） | 不用，Python 就够 |
| full eval：真加载 skill 正文、真调 tool、真落产物 | OpenClaw CLI → [OPENCLAW.md](OPENCLAW.md) §0/§1 有下载地址和从零到通的脚本 |
| 把 agent 关进容器跑（隔离 / 断网 / 限资源） | Docker：<https://docs.docker.com/get-started/get-docker/>，再 build [environments/openclaw.Dockerfile](environments/openclaw.Dockerfile) |

`requirements.txt` 里的 `docker` 只是 Python SDK，装上不代表你需要 Docker daemon；
不用容器就永远不会去连它。

---

## 换成你自己的 skill

仓库里跟踪的三个 skill（`csv-profiler` / `deliverable-pack` / `release-notes`）
**只是示例**，用来让空白 clone 能跑通。真评测要换成你自己的。

最短路径是统一初始化入口。第一次不加确认参数，它只展示将创建什么、调用哪个生成模型、
会发送哪些数据，不写文件也不发请求：

```bash
.venv/bin/python -m pipeline init \
  --source installed_skills/humanizer \
  --acceptance "明确要求去除 AI 写作痕迹时激活；普通润色、翻译和排版任务拒绝"
```

确认计划后，用同一条命令补上：

```bash
.venv/bin/python -m pipeline init \
  --source installed_skills/humanizer \
  --acceptance "明确要求去除 AI 写作痕迹时激活；普通润色、翻译和排版任务拒绝" \
  --confirm --confirm-egress
```

它会依次完成：

1. 把安装源冻结成 `subjects/<skill-id>/v1/`；已有内容完全一致的快照会安全复用，内容不同则拒绝覆盖。
2. 调生成模型产出 10 道 routing dataset + suite 草稿。
3. 停在 `evals/drafts/<skill-id>/REVIEW.md` 人工审核门，**不会自动运行评测**。

默认只把目标 skill 放进 catalog，先覆盖 pos / amb / rej。需要把邻近 skill 纳入 amb/multi
边界测试时，再使用底层 `workflows.gen_cases --include-neighbors`；初始化入口不替用户猜哪些
邻居属于真实生产 catalog。

先分清**三种路径角色**：

- `installed_skills/<slug>/`：上游**安装源目录**。它属于 SkillHub 或 agent 自己，不是评测输入。
- `subjects/<skill-id>/vN/`：skillEval 读取的**被测快照目录**。V1/V2 对照、hash、归因都以它为准。
- `outputs/<run>/<execution_id>/inputs/skills/<skill-id>/`：某次 run 归档里的**运行时副本**，只用于复盘当时到底喂了什么。

评测默认读 `subjects/`，不是直接读 `installed_skills/`。这是故意的：V1/V2 对照需要冻结输入版本，
也不能让本地 agent catalog 的实时变化污染历史结果。`inputs/skills/` 也不是新的被测源，
它只是那次 run 的归档快照。

如果只想做桥接、不想自动出题，也可以单独运行底层命令：

```bash
.venv/bin/python -m workflows.import_skill --source installed_skills/humanizer --version v1
```

它会把安装产物复制成 `subjects/<skill-id>/v1/`，后续 suite 就能直接引用。

一旦某个 `skill@version` 已出现在历史 run 中，这个版本就视为不可变。再次 plan 时若发现
同版本 `content_hash` 已改变，平台会在花钱前阻断并要求新建 `vN+1`；不要让两个内容不同的
报告都自称 “v2”。

不使用 `pipeline init` 时，才需要手工复制示例 suite：

```bash
cp evals/suites/example_routing.yaml evals/suites/my_skill_routing.yaml
# 改三处：dataset 指向你的题集、skills.include 换成你的 skill、models 换成你要测的模型
```

> **你自己的 skill、题集、suite、素材都不进 git。** `.gitignore` 只放行那一套示例，
> `subjects/`、`evals/datasets/`、`evals/suites/`、`evals/fixtures/` 下你新加的东西
> 一律被忽略 —— 被测 skill 是你的私有输入，评测平台不该替你上传它。

如果你已经跑出一轮失败结果，最短的改进建议命令现在是：

```bash
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/<your-run>/<execution-id> \
  --confirm-egress
```

它会优先复用 source run 的模型配置，并尽量从 `config.snapshot.yaml` /
`inputs/skills/` 这份**运行归档副本**自动推断目标 `SKILL.md`。只有同一次 run 里
保留了多个 skill 副本、无法唯一推断时，才需要再补 `--skill-id <skill>` 或
`--skill-file <path>`。

---

## 示例跑出来长什么样

下面是上面第一步那条 `--mock` 命令的**真实输出**（14 题 × 2 次 = 28 次判定，catalog 3 个 skill）。
换成真模型跑，格式一模一样，只有数字会变：

```text
路由结果 — 14 cases × 2 runs = 28 次可评判定 | skills=3
  Exact skill-set match    85.7%   ← 最严格（全部题）
  Top-1 accuracy (单skill)  95.0%
  Multi-skill exact         0.0%
  No-Skill rejection       83.3%
  False activation         16.7%

分题型准确率（Exact match）：
  pos      16 runs    93.8%
  amb       4 runs   100.0%
  multi     2 runs     0.0%
  rej       6 runs    83.3%

每-skill 指标（单标签题）：
                  precision  recall  f1-score  support
csv-profiler           0.89    1.00      0.94      8.0
deliverable-pack       1.00    1.00      1.00      8.0
release-notes          1.00    0.75      0.86      4.0
∅(No-Skill)            0.83    0.83      0.83      6.0

混淆矩阵（行=期望 top-1，列=预测 top-1；26/28 次单标签判定）：
                    预:csv-profiler  预:deliverable-pack  预:release-notes  预:∅
真:csv-profiler                   8                   0                0    0
真:deliverable-pack               0                   8                0    0
真:release-notes                  0                   0                3    1
真:∅                              1                   0                0    5

稳定性：exact_set_match 跨 2 次 repeat 85.7% ± 0.0%

⚠️ 同题跨 repeat 判定不一致（delta 会被这些题的随机性污染）：
  example-pos-08           1/2 判对
  example-rej-03           1/2 判对

发布门槛：
  OBSERVED exact_set_match      >= 0.85  实际 85.7%
  OBSERVED top1                 >= 0.90  实际 95.0%
  OBSERVED no_skill_rejection   >= 0.90  实际 83.3%
  OBSERVED false_activation     <= 0.05  实际 16.7%
  OBSERVED type_amb             >= 0.75  实际 100.0%
  → QUALITY VERDICT NOT EVALUATED（synthetic mock 只验证管道）
```

再说一次：mock 没有 skill 质量结论。表里的门槛比较只标成 `OBSERVED`，
不会产出 PASS/FAIL；这一步只看报表出得来、`outputs/` 有归档。

---

## 设计：五个抽象步骤

整套东西就是这条链路。每一步都有固定的落点，不靠人记。

```text
① 定题 ──→ ② 定配置 ──→ ③ 跑 ──→ ④ 打分 ──→ ⑤ 判门槛 / 比 delta
   题目           suite        原始判定      维度分数       PASS/FAIL
```

| 步骤 | 你做什么 | 东西落在哪 | 谁执行 |
| --- | --- | --- | --- |
| **① 定题** | 写用户会怎么问，标注期望激活哪个 skill（可以是"一个都不该激活"） | `evals/datasets/{kind}_{scope}_v{x.y}.jsonl` | 你 + [AUTHORING.md](evals/AUTHORING.md) |
| **② 定配置** | 声明：用哪个模型、哪版 skill、跑几遍、门槛多少 | `evals/suites/*.yaml` | 你 + [RUNBOOK.md](evals/RUNBOOK.md) |
| **③ 跑** | 每题 × 每次重复 = 一次判定，原始输出全落盘 | `outputs/{run}/runs.jsonl` | `workflows/run_routing.py` |
| **④ 打分** | 算 Top-1 / 拒答率 / 误激活 / 每-skill PRF / 混淆矩阵，外加跨 repeat 方差与效率维度 | `outputs/{run}/scores.json` | `workflows/score_routing.py` + `workflows/metrics.py` |
| **④b 判语义**（可选） | 代码判不了的东西交给**独立 judge 模型**：标准维度打 0–1 分、自定义断言判 pass/fail，都要给证据 | `outputs/{run}/grading.{judge_id}.json` | `workflows/grade.py` + `workflows/dimensions.py` |
| **⑤ 判门槛** | 对照 suite 里的 gate 出 PASS/FAIL；或和另一次 run 比 delta | `outputs/{run}/report.html` | 同上 |

> ④b 的 judge **和被测模型完全分开**：模型、端点、API key 各配各的
> （默认读独立的 `JUDGE_BASE_URL` / `JUDGE_API_KEY`）。理由很直接 ——
> 拿被测模型给自己判分等于让考生改自己的卷子。换 judge 就是换尺子：
> 同一批 run 换个 judge 重判会得到不同的 `assertion_pass_rate`，两者不可直接比较，
> `workflows/compare_runs.py` 会在那一行标出 `[⚠️ 尺子不同]`。

### 撑住这条链路的三条原则

**1. 配置即实验 —— 会改变结果的东西，一个都不许散落。**

模型、模型参数、tool 列表、skill 版本、重复次数、评分门槛，全部集中在一个 suite 文件里，跑的时候**冻结一份快照**（含 `config_hash`）进结果目录。所以脚本里没有可调参数：想改什么就改 suite。

> 反例：把模型写在 `.env`、重复次数硬编码在脚本里 —— 三个月后你会拿到两组对不上的数字，且查不出为什么。

**2. 确定性优先 —— 能用代码判定的，绝不叫 LLM 当裁判。**

路由对错是集合比较，`sklearn` 一行就出混淆矩阵。LLM Judge 留给"文案写得好不好"这类真的没法用代码判的。

**3. 分层加载 —— 路由只读 metadata，不读正文。**

routing-only 模式只把 frontmatter（name / description / triggers / exclusions）喂给模型。既省 token，也让"description 写得好不好"成为一个能被单独测量的变量。

路由有两档数据集：

- 快速 metadata 基线：单句 prompt，定位 description / trigger 的直接边界。
- 生产上下文路由：`RoutingCase.context` 带 role、长文本、历史消息和只读
  tool/MCP 目录，模型必须从整段会话恢复未完成意图。把示例 suite 的
  `routing_input.strategy` 从 `direct` 换成 `production_context` 即可（字段见下）。

两档由输入策略工厂切换，不复制 LiteLLM runtime：

```yaml
# 第一阶段：先把 metadata 的直接选择调好
routing_input:
  strategy: direct
  options: {}

# 第二阶段：再切真实上下文匹配
routing_input:
  strategy: production_context
  options:
    include_role: true
    include_long_context: true
    include_messages: true
    include_tools: true
```

策略实例由 `create_routing_input(strategy, **options)` 创建。切换参数会进入 suite 快照和
`config_hash`；策略的指令、版本和实际 options 还会进入 runtime fingerprint。因此 direct
与 production_context 是两组明确不可混算的实验。

两档都只调用 LiteLLM 一次、都不执行 tool；`context.tools` 是模型在生产里能看到的
工具说明，suite 顶层 `tools` 才是执行授权，不能混用。

### 执行环境是可替换的：Runtime 工厂

「用什么跑」和「怎么评」是两件事。所有执行环境实现同一个 `RuntimeAdapter` 接口，由工厂按 suite 里的 `runtime:` 字段创建——**编排层和评分层都不知道背后是谁**：

```text
suite: runtime: openclaw
          ↓
   create_runtime("openclaw")        ← adapters/runtimes/__init__.py（注册表工厂）
          ↓
   RuntimeAdapter.run(InvocationRequest) → RunResult   ← 统一契约
          ↓
   workflows/score_routing.py                  ← 只认 RunResult，换 runtime 不用改一行
```

| runtime | 干什么 | 支持模式 | tool | 多轮 |
| --- | --- | --- | --- | --- |
| `litellm` | 一次 completion 完成路由判定（默认） | none, routing_only | ✗ | ✗ |
| `openclaw` | 走真实 agent loop，可加载并执行 skill | none, routing_only, full | ✓ | ✓ |
| `mock` | 假数据，只用来验证链路没断 | 全部 | ✗ | ✗ |

新增一个 runtime 不用改工厂：写个类加 `@register("名字")` 即可。suite 里声明了 runtime 不支持的能力（比如让 `litellm` 跑 `full`），**跑之前就会被拦下**，不会跑一半才炸。

每个 adapter 还要用 `fingerprint()` 交代自己内部会影响结果的东西（system prompt、CLI 版本），这些一并进 `config_hash`——否则改一句 prompt 就能悄悄改变结果而指纹不变。

Runtime 与 Environment 是两条独立替换轴。编排层先让 Environment 物化隔离环境，
再把 prepared request 交给原来的 runtime 接口：

```python
with environment.prepared(request) as prepared_request:
    result = runtime.run(prepared_request)  # 仍返回同一个 RunResult
```

`local` 提供逐 request 的独立 workspace；`docker` 用固定 digest 镜像创建逐 request
容器，当前支持只读 skill mount、可写 workspace、`disabled/full` 网络和 CPU/内存限制。
Runtime 不判断“是不是 Docker”，只消费 resolved path 与通用 command prefix；
Evaluator 仍只消费 `RunResult`。细节见 `environments/`。

> 下载来的 skill **不是**安装到 `~/.codex/skills` 后再测。把它作为项目内的版本化、
> **本地忽略**输入快照（例如 `subjects/topnews/v1/`）；Docker backend 会为每个 request 复制该快照、
> 只读挂载到容器 `/skills`，并在退出后清理。这样宿主 agent catalog 不被污染，且同一
> source hash 才有可复现的 V1/V2 对照。仓库只跟踪 `csv-profiler` / `deliverable-pack` /
> `release-notes` 这套示例 catalog，不发布任何真实被测 skill 或其评测结果。

### 结果目录：三层记录，各管各的

```text
outputs/routing_example_v1.0__qwen3.7-max-2026-05-17__v1/20260729T120000+0200/
├── config.snapshot.yaml   ← frozen suite + config_hash + runtime/environment
├── inputs/                ← 本次 dataset 与实际 resolved SKILL.md 副本
├── runs.jsonl             ← 原始输出 + duration_ms + usage token
├── scores.json            ← 质量分 + time/token/tool/error 统计
└── report.html            ← 同样内容的人读版
```

目录名 `{数据集}__{模型}__{skill版本}/{execution_id}` 四个维度全在；每次运行都会新建
归档，并保留当时实际的题集和 skill 内容。所以之后换模型、题集或 V2/V3，旧结果仍可复现和对比。

> **比 delta 前先对 `config_hash`。** hash 不同说明配置动过，两个数字不可直接比。

---

## 常见任务

**我改了 skill 描述，想知道有没有变好**

```bash
# 1. 复制已评过的版本；原始 V1 保持不动
cp -R subjects/<skill>/v1 subjects/<skill>/v2
# 2. 复制 suite，只改两处
cp evals/suites/example_routing.yaml evals/suites/routing_v2.yaml
```

```yaml
skills:
  dir: subjects                    # ← 不动！dir 决定 catalog 里有哪些 skill
  target: [<skill>]                # ← 不动！明确这套 V1/V2 对照归哪个 subject
  versions: {<skill>: v2}          # ← 只将目标 skill 钉到 V2
  cfg: v2                          # ← 换个标签，进 output 目录名
```

```bash
.venv/bin/python -m pipeline run --suite evals/suites/routing_v2.yaml \
  --confirm --confirm-egress
.venv/bin/python -m workflows.compare_runs outputs/<dataset>__<model>__{v1,v2}/*
```

> ⚠️ **别改 `skills.dir`。** dir 指向只放了一个 V2 skill 的目录时，catalog 会从 N 个
> 缩成 1 个 —— 那是「单选题 vs N 选一」，不是版本对照，delta 完全不可归因。未在
> `versions` 钉选的 skill 固定取最小版本（通常是 `v1`），日后新增 `v3` 不会污染旧基线。

**我本地装了一堆 skill，这次只想拿其中几个当候选**

```yaml
skills:
  dir: subjects
  target: [pdf]                    # 这套实验归 PDF；与候选 catalog 分开
  include: [artifacts-builder, docx, mcp-builder, pdf, pptx, xlsx]   # catalog 就是这 6 个
  # exclude: [topnews]        # 反过来写也行：从全部里剔掉。但优先用 include
```

`include` 是显式清单，之后本地再装 10 个 skill 也不会漂进这次实验；`exclude` 是减法，
装了新的就会悄悄进来。catalog 组成进 `config_hash`，所以**故意没有命令行覆盖**——
能用 CLI 改一下就换掉实验的话，归档记的配置就不是真跑的配置了。

`target` 则只声明“这套测试归谁”，供 subject 归档和后续 Viewer 使用。No-Skill 基线也保留
目标，例如 `target: [pdf]`，只是把 PDF 从 `include` 拿掉。它进入配置快照但不改变运行
`config_hash`，因为它不改变模型实际看到的输入。

> 漏选目标 skill 时，`plan` 会在花钱前报「N/M 道题的 gold 指向 catalog 里没有的
> skill」并举例是哪几条。不拦运行 —— No-Skill 基线正是故意造成这个状态。

**我想知道这个 skill 真跑起来有没有用（不只是路由选对）**

这是 full eval：真加载正文、真调 tool、真落产物，按确定性断言打分。**先装 OpenClaw**
（[OPENCLAW.md](OPENCLAW.md)），因为 `litellm` 不执行 tool，只会在跑之前被拦下。

要写两份 suite：`mode: none` 的 No-Skill 基线，和 `mode: full` 的 skill 在场版。
仓库不发这两份（它们指向你自己的 skill），照示例 suite 改：

```bash
.venv/bin/python -m workflows.run_routing --suite evals/suites/<你的>_none.yaml   # No-Skill 基线
.venv/bin/python -m workflows.run_routing --suite evals/suites/<你的>_v1.yaml     # skill 在场
.venv/bin/python -m workflows.score_full  --dir outputs/<那次 run 的目录>
.venv/bin/python -m workflows.compare_runs outputs/<dataset>__<model>__{none,v1}
```

两份 suite 必须**只差 `skills.cfg` 与 `skills.exclude`**，其余逐字相同 —— 这是 delta 能归因的前提。
`workflows/compare_runs.py` 报出 `[⚠️ 污染]` 就说明混了多个原因，别信那个数。

Full eval 支持多轮和受控并发。顶层 `prompt/files/expect_*` 是第 1 轮，`turns`
从第 2 轮开始；同一 `case × repeat` 全程复用 session、workspace 与容器，不同
conversation 才能并发：

```json
{
  "id": "deliverable-pack-pos-02",
  "prompt": "先根据输入整理一份初稿，保存到 out/draft.md",
  "expected_skills": ["deliverable-pack"],
  "expect_artifacts": ["out/draft.md"],
  "turns": [{
    "prompt": "沿用刚才的数据，把初稿补成最终版",
    "requires_context": true,
    "expect_artifacts": ["out/final.md"],
    "expect_workspace_files": ["out/draft.md", "out/final.md"]
  }]
}
```

```yaml
repeats: 3
parallelism: 4   # 并发 conversation 数；同一 conversation 内的 turns 永远串行
```

上一轮执行失败，后续轮会记为 `skipped`，不重复扣分。`score_full` 同时输出
`task_completion`（按 conversation）、`turn_completion`、`session_continuity`、
`context_retention` 和 `file_state_continuity`。本机 OpenClaw 的 profile 配置是共享状态，
因此 agent 调用会安全地串行；需要真正并发的 full eval 使用 Docker environment，
每个 conversation 一个独立容器。

**我想换个 / 多加一个模型** —— 编辑 suite 的 `models:`，写成多条，一次命令跑完，各出一个目录。密钥加到 `.env`，suite 里只写变量名。

**我想评估语义质量（代码判不了的那些）**

```bash
.venv/bin/python -m workflows.grade --list-dimensions              # 有哪些维度、每档锚点是什么
.venv/bin/python -m workflows.grade --dir outputs/xxx --dry-run    # 先看会外发什么，不调模型
.venv/bin/python -m workflows.grade --dir outputs/xxx              # 用 suite 配的 judge + 维度
.venv/bin/python -m workflows.score_routing --dir outputs/xxx      # 把分并进报表
```

通用语义维度评价的是“最终回答是否完成用户任务”，因此只适用于 full eval。routing-only
只输出 skill 选择与理由，正确性应由 `score_routing.py` 的集合匹配、拒答率和混淆矩阵判定；
CLI 会拒绝把通用 LLM Judge 误用在路由 JSON 上。

内置维度（定义与 rubric 在 [workflows/dimensions.py](workflows/dimensions.py)，每条都注明抄自哪）：

| 维度 | 问什么 | 来源 |
| --- | --- | --- |
| `faithfulness` | 输出的事实能不能在输入里找到依据（有没有编造） | RAGAS / DeepEval |
| `completeness` | 用户要求的每一项都覆盖了吗 | AGENTS.md §21.6 |
| `relevancy` | 切不切题 | RAGAS answer_relevancy |
| `instruction_following` | 显式约束守住了吗 | G-Eval 常见 criteria |
| `correctness` | 与参考答案有没有事实冲突（需要题里写 `reference`） | autoevals Factuality |
| `conciseness` | 有没有冗余灌水 | G-Eval 变体 |

**维度是跨题通用的尺子** —— suite 里选几个就对所有题生效，题里一个字都不用改
（相比之下 `expect_assertions` 要一题一题写）。分数是 0–1 连续分，每条都要带原文证据。

judge 看得到**文本产物的内容**（`Artifact.text_excerpt`，截断到 4000 字符），所以
"报告里的数字是不是编的""人名是不是文字稿里没有的"这类断言判得准；docx/png 这类二进制
产物它只看得到元数据，需要读内容才能确认的断言会按"证据不足"判 failed。

跑的时候把 grade 接进管道，一条命令走完 run → 判语义 → 打分：

```bash
.venv/bin/python -m pipeline run --suite <suite> --stages run,grade,score --confirm --confirm-egress
```

`grade` 默认不在 `--stages` 里 —— 它是一次额外的付费外发调用，必须显式要。
写的顺序不作数，它一定排在 `score` 之前（`score_full` 要把判定结果并进 `assertion_pass_rate`）。

**我想换一把尺子来判分（judge 模型和被测模型分开）**

```bash
# 换模型必须换 id，否则拒绝执行、防止覆盖上一把尺子的结果
.venv/bin/python -m workflows.grade --dir outputs/xxx \
    --judge-id qwen --judge-model openai/qwen3.7-max-2026-05-17   # → grading.qwen.json
.venv/bin/python -m workflows.score_full --dir outputs/xxx --judge-id qwen  # 并入哪把尺子的分
```

judge 的凭据走独立的 `JUDGE_BASE_URL` / `JUDGE_API_KEY`，跟被测模型的
`DASHSCOPE_API_KEY` 互不干扰 —— 可以让 GLM 判 qwen 的卷子，或反过来。
那两个字段存的是**环境变量名**，填什么都行；默认分开只是为了别让人不知不觉
拿被测模型判自己。`scoring.judge` **不进 `config_hash`**（它跑在 run 之后，
不改变 `runs.jsonl`），追溯看 `scores.json` 的 `judge` 字段。

> **judge 打的分默认不进 gate。** 未经校准的语义分不该决定发版
> （AGENTS.md §22.6：judge 与人工标注一致率 ≥ 80% 才够格）。
> 它们照常出数、照常比 delta，只是不参与 PASS/FAIL。

**我想加题** —— 用 `workflows/gen_cases.py --skill-dir ... --acceptance ...` 先生成 DRAFT。
草稿落在 `evals/drafts/<scope>/routing_<scope>_v0.1-draft.jsonl`（文件名带 scope，
因为它会原样变成 output 目录名的第一维）。加了 `--include-neighbors` 时，生成器**必须**
为每道 `rej` 题写清「catalog 里为什么没有一个 skill 该激活」，这些理由顶在 `REVIEW.md`
最前面 —— 那是最容易错也最贵的一类 gold（错了会让 false_activation 结构性虚高）。
默认只把目标 skill 放进 catalog；要测边界/多 skill 时才显式加 `--include-neighbors`。可用
`--count 3..30`（默认 10）控制草稿规模；但带 `--include-neighbors` 时，最小合法值不是 3，
而是 4，因为生成器还必须覆盖 `multi`。DRAFT 会被 `plan` 和 `run` 硬拦下；按
[AUTHORING.md](evals/AUTHORING.md) §5 人审后，将 dataset 头部标为 `APPROVED` 再运行。

生成完还会**再调一次模型盲判 rej gold**：只给 catalog metadata 和题面，不给 gold、不给
生成器自己写的理由，反过来问「这些请求里哪些 skill 该激活」。复审点名了 skill，就说明这道题
`gold=∅` 很可能是错的，异议会顶到 `REVIEW.md` 最前面（排在生成器自证之前），结论同时写进
dataset 头部的 `# rejection_review:` 一行。它只标注不阻断 —— 谁对谁错是语义问题，代码判不了；
复审自己挂了会标成 `FAILED`，不会伪装成「无争议」。`--skip-rej-review` 省掉这次调用。

**我想根据失败结果整理下一版 skill 要改什么** —— 直接对某次 run 生成聚类建议：

```bash
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/<dataset>__<model>__<skillcfg>/<execution-id> \
  --confirm-egress
```

默认行为：

- 优先继承该 run 的 `resolved_model`，避免手动切回“上次成功的模型”
- 能唯一推断目标 skill 时，不要求再填 `--skill-file`
- 调用前打印端点、skill metadata 和失败 trajectory manifest；不加 `--confirm-egress`
  就停在预览，不调模型、不写文件
- 只写 `improvements/round-01/suggestions.json`，不改任何 skill 文件
- 失败证据按该 run 的 `skills.mode` 取：routing-only 取"选错 skill"；**full 取"任务没完成"**
  （缺产物 / 没调 tool / 拒答题却留了文件）**和"judge 判 failed 的语义断言"**，
  并把产物原文一起作为证据 —— 跑过 `grade` 的 run 才有后者

补充参数只在推断失败时再用：

```bash
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/<...>/<execution-id> \
  --skill-id humanizer \
  --confirm-egress
```

**我想让它把建议真的落成下一版 skill** —— 加 `--apply`：

```bash
.venv/bin/python -m workflows.suggest --run-dir outputs/<...>/<execution-id> \
  --apply --confirm-egress
```

它会：

- 把建议交给同一个模型改写 SKILL.md，**只新增** `subjects/<skill-id>/v<N+1>/`，源版本一个字不动
- 整目录复制源版本的 `references/`、`scripts/` 等附件，避免新版本运行时缺文件
- 校验改写结果：frontmatter 合法、`name` 没被改掉（改了就是另一个 skill）、不是原样抄回
- 写 `PROVENANCE.md` 记下依据哪个 run、哪份建议
- 生成同题复验 suite `improvements/round-NN/reeval.suite.yaml` —— 题集、模型、runtime、
  judge 全部照抄上一轮，**只换被测版本**，保证 delta 归因得到那次改动

复验就是拿它再跑一次，然后比：

```bash
.venv/bin/python -m pipeline run --suite outputs/<...>/improvements/round-01/reeval.suite.yaml \
    --stages run,grade,score --confirm --confirm-egress
.venv/bin/python -m workflows.compare_runs outputs/<v1 run> outputs/<v2 run>   # 基线放第一个
```

> `--apply` 会改仓库里的 `subjects/`（只新增版本目录），且要外发一次模型调用。
> 不带 `--apply` 时行为和以前完全一样：只出建议，不碰任何 skill 文件。

**我想调发布门槛** —— 改 suite 的 `scoring.gate`。它会进 `config_hash`，所以"门槛什么时候放宽的"有据可查。

---

## 目录

```text
skillEval/
├── README.md              本文 —— 怎么用
├── AGENTS.md              完整架构规范（开发文档，北极星设计）
│
├── contracts/             N0 数据契约（换 runtime 不用改评分，全靠它）
│   ├── skill.py           SkillMeta + 加载器 + catalog 构建
│   ├── evalcase.py        RoutingCase + 加载器
│   ├── runtime.py         InvocationRequest / RunResult / Health / Capabilities
│   └── suite.py           严格 Suite Contract + secret/交叉字段校验
│
├── adapters/runtimes/     N5 执行环境（工厂 + 可插拔实现）
│   ├── __init__.py        注册表工厂：create_runtime(name)
│   ├── base.py            RuntimeAdapter Protocol + 异常兜底基类
│   ├── litellm.py         纯 metadata 推理（默认）
│   ├── openclaw.py        OpenClaw CLI，非侵入接入
│   └── mock.py            假数据，验链路
├── adapters/routing_inputs/
│   ├── direct.py          第一阶段：metadata + 当前问题
│   └── production_context.py 第二阶段：role/context/history/tool/MCP
├── environments/          Runtime 之外的 Environment Backend
│   ├── base.py            EnvironmentBackend Protocol
│   ├── local.py           每 request 独立 workspace / skill staging
│   └── docker.py          固定 digest 容器、mount、网络、资源、可靠清理
│
├── pipeline/              跑前只读预检 + 显式确认后执行（`python -m pipeline`）
├── workflows/             运行与评分的可执行工作流（全部走 `python -m workflows.<name>`）
│   ├── run_routing.py     ③ 编排层；routing 与 full 共用
│   ├── matrix.py          确定性任务矩阵（model × case × repeat）+ session 隔离
│   ├── score_{routing,full}.py ④⑤ 确定性评分 + gate
│   ├── grade.py           独立 judge 的语义判定
│   ├── compare_runs.py    跨 run delta + 污染检测
│   ├── gen_cases.py       P2：生成 DRAFT dataset/suite
│   └── suggest.py         P3：失败证据 → 聚类建议 →（--apply）新版本 + 复验 suite
├── installed_skills/      上游安装源目录（你自己建；不作为评测输入，git 忽略）
├── subjects/              本地被测快照目录
│   ├── csv-profiler/v1/       ← 示例 skill（已跟踪）
│   ├── deliverable-pack/v1/   ← 示例 skill（已跟踪）
│   ├── release-notes/v1/      ← 示例 skill（已跟踪）
│   └── <你自己的>/vN/          ← git 忽略
├── evals/
│   ├── AUTHORING.md       ← 怎么写题、怎么起名、怎么定版本
│   ├── CASEGEN.md         ← 自动出题与改进建议的人审规范
│   ├── RUNBOOK.md         ← 配置存哪、怎么跑、怎么比
│   ├── datasets/          ① 问题集（答案内联）
│   │   └── routing_example_v1.0.jsonl   ← 唯一已跟踪的题集，其余 git 忽略
│   ├── suites/            ② 配置
│   │   └── example_routing.yaml         ← 唯一已跟踪的 suite，其余 git 忽略
│   ├── fixtures/          输入素材（full eval 用；git 忽略）
│   └── expected/          大件参考答案（full eval 用；git 忽略）
├── tests/                 契约验收测试（`python -m pytest tests/ -q`）
├── outputs/{run}/         ③④⑤ 结果（git 忽略）
├── archives/              按 subject 聚合的可恢复测试包（git 忽略）
└── .env                   仅密钥，不进 git
```

> 仓库只发**一套**能在空白 clone 上跑通的示例：3 个 skill + 1 份题集 + 1 个 suite。
> 你自己的 skill、题集、suite、素材全部落在同样的目录里，但被 `.gitignore` 挡住 ——
> 被测 skill 和评测结果是你的私有输入，评测平台不该替你上传。

---

## 当前边界

| 能力 | 状态 |
| --- | --- |
| 路由评测（routing-only） | ✅ |
| 生产上下文路由（role / 长上下文 / 历史 / tool / MCP / 模糊末句） | ✅ 把 `routing_input.strategy` 换成 `production_context` |
| Runtime 抽象 + 注册表工厂 | ✅ litellm / openclaw / mock |
| Suite 严格校验 | ✅ 未知字段、类型漂移、重复 ID、gate、明文 secret 均在运行前拒绝 |
| 多模型一次跑完 | ✅ |
| 确定性任务矩阵 | ✅ model × case × repeat × turn；唯一 request、conversation 内复用 session；并发后按矩阵顺序落盘 |
| 配置快照 + config_hash（含 runtime fingerprint） | ✅ |
| 维度评分 + 发布门槛 PASS/FAIL | ✅ |
| Exact skill-set match / 多 skill 题 / 分题型指标 | ✅ |
| 问题集 / 结果的版本化命名 | ✅ |
| Subject 测试包归档 / 解归档 | ✅ `pipeline archive/unarchive`；支持单个或批量 subject、共享依赖保留、checksum 后清理、冲突拒绝覆盖 |
| Case 输入文件只读挂载（`files:` → workspace） | ✅ 素材放 `evals/fixtures/`；缺文件在 `plan` 阶段就拦下 |
| `severity: critical` → Critical Skill Miss Rate | ✅ routing 与 full 双侧接入，可进 gate（默认门槛 = 0） |
| 单变量对照的护栏 | ✅ `skills.versions` 钉选单个目标版本；未钉 skill 固定取 v1，skill 被作者 `disable` 会在 `plan` 里告警 |
| catalog 组成自己定 | ✅ `skills.include` 点名这次传哪些（推荐，抗漂移）/ `exclude` 剔掉哪些；组成进 `config_hash`，故意不给 CLI 覆盖 |
| 选错 skill 的护栏 | ✅ 题集 gold 指向 catalog 里没有的 skill 时，`plan` 报「几道题无解、缺哪个、举例哪几条」，No-Skill 基线除外 |
| OpenClaw 执行 | ✅ 已打通（healthy=✓，真实路由已验证）→ [OPENCLAW.md](OPENCLAW.md) |
| Full eval 管道 | ✅ 注入 skill → 加载 → 调 tool → 产物归一化（`tool_calls`/`artifacts`/`loaded_skills`） |
| Environment Backend | 🚧 local 已接编排；docker 生命周期、只读 mount、断网、资源限制真实 smoke 已通过 |
| 多轮对话 / 并行执行 | ✅ full eval 逐轮断言、session/context/file 延续、失败后 skipped；`parallelism` 按独立 conversation 并发，repeat 零泄漏 |
| Full eval 的**对照数字** | ✅ P1：none→v1 任务完成度 16.7%→83.3%，无配置污染 |
| No-Skill 基线、跨 run delta + 污染检测 | ✅ |
| 自动出题（skill + 验收标准 → 题目/suite） | ✅ P2：自动集复现 none < v1 < v2 与 FAIL/FAIL/PASS |
| 改进闭环（建议 → 新版本 → 同题复验） | ✅ P3：`suggest` 收 routing/full 两种失败证据；`--apply` 写 `subjects/<id>/v<N+1>/` 并生成只换版本的复验 suite；gate / 累计 token+墙钟预算 / 最大轮数命中即停。外发前必须审 manifest 并显式给 `--confirm-egress`。**每一步都要人显式发起，没有自动循环** |
| 语义 Judge（独立模型判断言 + 维度分） | ✅ 产物文本内容进 judge 输入（`Artifact.text_excerpt`）；二进制产物仍只有元数据 |
| Evaluator 注册表（可插拔评分器） | ❌ P4 |
| Docker 环境隔离 / 网络策略 | 🚧 提前开发：`full`（默认）与 `disabled` 已实现；mock/allowlist 尚未完成 |
| 全过程 Viewer（问题→运行→结果→评价 下钻） | ❌ P7，当前只有单 run 的 `report.html` |

**OpenClaw 已接通**（用独立 profile，不碰你的 main 配置）。换机器重新接、或升级后出问题，照 [OPENCLAW.md](OPENCLAW.md) 走 —— 那里有从零到通的脚本、env 怎么接、以及全部踩过的坑。验证：

```bash
.venv/bin/python -m workflows.run_routing --healthcheck \
    --runtime openclaw --runtime-option profile=skilleval
# runtime=openclaw version=OpenClaw 2026.7.1-2 healthy=✓
```

表里的 P1–P7 是阶段编号。**最终验收标准（六条）和每阶段的交付/验收/明确不做，见
[AGENTS.md](AGENTS.md) §★★★** —— 判断「下一步做什么」以那一节为准。完整架构设计同样在 AGENTS.md。
