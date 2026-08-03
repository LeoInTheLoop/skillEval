# skillEval

[English version / 英文版](README.md)

用一套可复现的实验，回答两个问题：

1. 用户提出任务时，Agent 有没有选对 skill？
2. skill 真正加载后，有没有把任务做完、调用正确的 tool、生成预期产物？

skillEval 目前同时支持：

| 模式 | 测什么 | Runtime | 主要结果 |
| --- | --- | --- | --- |
| `routing_only` | 只给模型看 skill metadata，判断该激活哪个 skill | LiteLLM / OpenClaw | Exact match、Top-1、误激活、No-Skill 拒答、混淆矩阵 |
| `full` | 真正加载 `SKILL.md` 正文，运行 agent loop、tool 和产物流程 | OpenClaw | 任务完成度、tool 命中、artifact 命中、多轮连续性 |
| `mock` | 不联网、不用 key，只验证安装、编排、评分和报告链路 | 内置 mock runtime | synthetic 结果，不代表 skill 质量 |

每次实验都会冻结 suite、dataset、skill、runtime 指纹和原始输出。评分阶段只写新文件，
不修改 `runs.jsonl`；历史运行不会被下一次覆盖。

---

## 5 分钟跑通

前置：Python 3.11–3.13。Python 3.14 也能运行，但部分依赖可能需要更久的安装时间。

```bash
git clone https://github.com/LeoInTheLoop/skillEval.git
cd skillEval
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

先用仓库自带的 3 个示例 skill、14 道题和 mock runtime 验证全链路：

```bash
.venv/bin/python -m pipeline plan \
  --suite evals/suites/example_routing.yaml \
  --mock

.venv/bin/python -m pipeline run \
  --suite evals/suites/example_routing.yaml \
  --mock \
  --confirm
```

`plan` 是只读预检：不写 `outputs/`，也不发模型请求。`run` 会再次打印相同计划，
然后执行并生成：

```text
outputs/routing_example_v1.0__mock__v1/<execution-id>/
├── config.snapshot.yaml
├── inputs/
├── runs.jsonl
├── trajectory.jsonl
├── scores.json
└── report.html
```

mock 会显示 `QUALITY VERDICT NOT EVALUATED`。它只证明管道能跑，不能判断 skill 好坏。

配置好 `.env` 和 Docker 后，用仓库自带的 full suite 跑真实 agent loop。它只有 2 题，
会在逐请求容器中加载 `deliverable-pack`、调用 tool、检查产物，并自动进入 `score_full`。
示例把 Docker 作为安全边界，所以默认开放完整 OpenClaw toolset：

```bash
docker build -f environments/openclaw.Dockerfile -t skilleval-openclaw .
export SKILLEVAL_OPENCLAW_IMAGE="$(
  docker image inspect skilleval-openclaw --format '{{.Id}}'
)"

.venv/bin/python -m pipeline plan \
  --suite evals/suites/example_full.yaml \
  --healthcheck

.venv/bin/python -m pipeline run \
  --suite evals/suites/example_full.yaml \
  --confirm \
  --confirm-egress
```

这不是另一条隐藏 workflow：预检、执行和 full 评分都由同一个 `pipeline plan/run` 入口选择。
完整 suite 与 dataset 分别在
[`evals/suites/example_full.yaml`](evals/suites/example_full.yaml) 和
[`evals/datasets/full_example_v1.0.jsonl`](evals/datasets/full_example_v1.0.jsonl)。
对自己的新 skill 做 full eval 时，不要直接从完整题集开始；先按
[`evals/RUNBOOK.md`](evals/RUNBOOK.md) 的“full eval 首次运行：先做 one-case baseline”
跑一条人工审核过的 case，确认注入、tool、artifact 和评分链路，再扩展题集。

运行项目测试：

```bash
.venv/bin/python -m pytest tests/ -q
```

---

## 统一入口

日常使用只需要记住两个命令：

```bash
.venv/bin/python -m pipeline plan --suite <suite.yaml>
.venv/bin/python -m pipeline run  --suite <suite.yaml> --confirm --confirm-egress
```

`pipeline plan` 会在花钱和外发数据之前检查：

- suite、dataset 和 skill frontmatter 是否符合严格契约；
- runtime 是否支持所选模式；
- skill catalog、版本和 gold 是否可达；
- API key 是缺失、占位符还是真实配置；
- suite、runtime 和 environment 的静态兼容性；
- 计划调用次数、目标 endpoint 和将外发的数据类别；
- 最终结果目录和 `config_hash`。

增加 `--healthcheck` 时，plan 还会探测本地 runtime / environment；不加时保持纯静态预检。
真实 `pipeline run` 会自动重做健康检查。

真实运行必须同时给：

- `--confirm`：确认执行本地写入和 runtime；
- `--confirm-egress`：确认把计划中列出的内容发送给模型服务。

所有会影响结果的参数都写在 suite 中，不提供临时 CLI 覆盖。这样运行快照才是实际实验配置。

---

## 换成第三方 skill 跑 full eval

下面是最小路径。示例使用公开的
[blader/humanizer](https://github.com/blader/humanizer)；它是纯 Markdown skill，
方便把重点放在 skillEval 的执行链路上。

这只是 3 题 smoke test，用来确认系统可用，不是对 humanizer 的正式质量结论。

### 1. 准备 Docker OpenClaw

full eval 需要 OpenClaw，因为 LiteLLM 只做一次模型调用，不执行 agent tool loop。
默认把它跑在逐请求容器里；完整镜像、provider 和凭据说明见 [OPENCLAW.md](OPENCLAW.md)。

```bash
docker build -f environments/openclaw.Dockerfile -t skilleval-openclaw .
export SKILLEVAL_OPENCLAW_IMAGE="$(
  docker image inspect skilleval-openclaw --format '{{.Id}}'
)"
```

宿主机不需要安装 OpenClaw；容器在运行时从 `.env` 注入 provider key。

### 2. 下载并审查 skill

第三方 skill 会进入 agent 的执行上下文。先阅读文件，再导入：

```bash
git clone --depth 1 \
  https://github.com/blader/humanizer.git \
  installed_skills/humanizer

find installed_skills/humanizer -maxdepth 3 -type f -print
sed -n '1,220p' installed_skills/humanizer/SKILL.md
```

不要把下载目录直接当评测输入。冻结一个版本化快照：

```bash
.venv/bin/python -m workflows.import_skill \
  --source installed_skills/humanizer \
  --version v1
```

结果是 `subjects/humanizer/v1/`。导入器保留 skill 文件和附件，但排除 `.git/`、`.env`、
`__pycache__/` 和 `.DS_Store`；`_meta.json` 也不会保存用户的绝对源路径。

三个路径的角色不同：

| 路径 | 角色 |
| --- | --- |
| `installed_skills/<name>/` | 上游下载或安装源，不直接参与实验 |
| `subjects/<skill-id>/vN/` | 不可变的被测快照，V1/V2 对照以它为准 |
| `outputs/<run>/inputs/skills/` | 某次运行实际使用的副本，用于事后复盘 |

### 3. 写 3 道 full-eval 题

将下面内容保存为 `evals/datasets/full_humanizer_smoke_v0.1.jsonl`：

```jsonl
# review_status: APPROVED — manually reviewed smoke set
{"id":"humanizer-pos-01","prompt":"请用 humanizer 把下面这段话改得自然、克制，保留所有事实，并把最终版本保存到 out/rewrite-1.md：\n\nOur groundbreaking platform stands as a testament to innovation, seamlessly empowering teams to unlock unparalleled value in today's rapidly evolving digital landscape.","expected_skills":["humanizer"],"expect_artifacts":["out/rewrite-1.md"],"expect_tools":["write"],"tags":["full","positive","artifact"],"severity":"high"}
{"id":"humanizer-pos-02","prompt":"请去掉这段中文里的 AI 腔，不要增加事实，把改写结果保存到 out/rewrite-2.md：\n\n本项目不仅彰显了团队对卓越的不懈追求，更标志着行业迈向智能化未来的关键转折点，为生态伙伴持续赋能。","expected_skills":["humanizer"],"expect_artifacts":["out/rewrite-2.md"],"expect_tools":["write"],"tags":["full","positive","artifact"],"severity":"high"}
{"id":"humanizer-rej-01","prompt":"2 加 3 等于多少？只回答数字，不要创建文件。","expected_skills":[],"expect_artifacts":[],"expect_tools":[],"forbid_artifacts":true,"tags":["full","rejection"],"severity":"medium"}
```

这三题刻意只验证最小闭环：

- 两道正向题必须加载 humanizer、调用 `write`、生成非空 Markdown；
- 一道拒答边界题必须直接回答，且不能留下文件。

正式评测需要更强的人工 gold、边界题和多次 repeat。开发系统时通常 10–20 道高质量题就够，
不要一开始生成上百道题。

### 4. 写 full suite

将下面内容保存为 `evals/suites/full_humanizer_smoke_v1.yaml`：

```yaml
suite_id: full_humanizer_smoke
suite_version: "0.1"
description: Public humanizer three-case full-eval smoke test

dataset: evals/datasets/full_humanizer_smoke_v0.1.jsonl

runtime: openclaw
runtime_options:
  bin: openclaw
  profile: skilleval

environment:
  backend: docker
  image_env: SKILLEVAL_OPENCLAW_IMAGE
  network: full
  cpus: 2
  memory: 2g
  env_passthrough: [QWEN_API_KEY=DASHSCOPE_API_KEY]

skills:
  dir: subjects
  target: [humanizer]
  include: [humanizer]
  mode: full
  cfg: v1-smoke

models:
  - id: openclaw-default

tools: ["*"]
repeats: 1
parallelism: 1
timeout_seconds: 600

scoring:
  metrics: [task_completion, artifact_hit, tool_hit]
  gate:
    task_completion: ">= 0.66"
    artifact_hit: ">= 1.0"
    tool_hit: ">= 1.0"
```

OpenClaw 自己管理执行模型和凭据，所以这里只需要可追溯的 `id`。
`RunResult.resolved_model` 会记录最终实际使用的 provider/model。

### 5. 先预检，再运行

```bash
.venv/bin/python -m pipeline plan \
  --suite evals/suites/full_humanizer_smoke_v1.yaml \
  --healthcheck
```

确认计划中的 skill、题数、tool、runtime、请求数和外发清单后：

```bash
.venv/bin/python -m pipeline run \
  --suite evals/suites/full_humanizer_smoke_v1.yaml \
  --confirm \
  --confirm-egress
```

full eval 可能数十秒才打印下一题；只要没有超过 suite 的 `timeout_seconds`，不代表卡死。
完成后，统一入口会自动调用 `score_full` 并生成报告。

完成后应看到 3 cases、确定性指标和 gate。它只检查“下载 → 冻结 → 注入 → OpenClaw
加载 → tool → artifact → score”链路；即使全部通过，也不代表两段改写的语义质量是
100 分。

检查原始轨迹和产物：

```bash
find outputs/full_humanizer_smoke_v0.1__openclaw-default__v1-smoke \
  -name scores.json -o -name report.html -o -name runs.jsonl

jq -c \
  '{case_id,status,loaded_skills,tool_calls,artifacts,resolved_model,error_kind}' \
  outputs/<run>/<execution-id>/runs.jsonl
```

---

## 路由评测：description / triggers 改好了吗

`routing_only` 只读取 `SKILL.md` frontmatter 的 `name`、`description`、`triggers` 和
`exclusions`，不把正文发给路由模型。它适合快速比较 V1/V2 的激活边界。

### 用真实模型跑仓库示例

复制密钥模板：

```bash
cp .env.example .env
```

把 `.env` 中的占位符换成真实 key，然后运行：

```bash
.venv/bin/python -m pipeline plan \
  --suite evals/suites/example_routing.yaml

.venv/bin/python -m pipeline run \
  --suite evals/suites/example_routing.yaml \
  --confirm \
  --confirm-egress
```

示例默认使用 DashScope 兼容端点。换 provider 时，编辑 suite 的 `models:`：

```yaml
models:
  - id: my-model
    model: openai/<provider-model>
    api_base_env: MY_MODEL_BASE_URL
    api_key_env: MY_MODEL_API_KEY
    params: {temperature: 0}
```

suite 里只写环境变量名；真实值只放 `.env`。

### 导入自己的 skill 并自动生成路由草稿

`pipeline init` 会：

1. 将上游 skill 冻结到 `subjects/<skill-id>/vN/`；
2. 根据 metadata 和业务验收标准生成 routing dataset + suite 草稿；
3. 停在人工审核门，不自动开跑。

先只看计划：

```bash
.venv/bin/python -m pipeline init \
  --source installed_skills/humanizer \
  --acceptance "明确要求去除 AI 写作痕迹时激活；普通翻译、排版和事实问答不激活"
```

确认本地写入和外发内容后：

```bash
.venv/bin/python -m pipeline init \
  --source installed_skills/humanizer \
  --acceptance "明确要求去除 AI 写作痕迹时激活；普通翻译、排版和事实问答不激活" \
  --count 10 \
  --confirm \
  --confirm-egress
```

草稿位于 `evals/drafts/<skill-id>/`。生成的 gold 不是真值；必须按
[evals/AUTHORING.md](evals/AUTHORING.md) 和草稿里的 `REVIEW.md` 人工审核。
标记为 `APPROVED` 前，`pipeline plan/run` 会拒绝使用它。

`pipeline init` 当前生成的是路由题。full eval 的 tool、artifact 和业务语义断言仍需人工设计。

### metadata 基线与生产上下文

快速基线只看当前问题：

```yaml
routing_input:
  strategy: direct
  options: {}
```

生产上下文路由可以加入 role、长文本、历史消息和只读 tool/MCP 目录：

```yaml
routing_input:
  strategy: production_context
  options:
    include_role: true
    include_long_context: true
    include_messages: true
    include_tools: true
```

两种 strategy 的 fingerprint 不同，结果不可直接混算。

---

## Dataset 与 suite 的最小契约

### Routing case

```json
{
  "id": "pdf-amb-01",
  "prompt": "分析这份合同的违约风险，不要改文件格式",
  "expected_skills": [],
  "tags": ["ambiguous"],
  "severity": "critical"
}
```

### Full-eval case

```json
{
  "id": "deliverable-pos-01",
  "prompt": "读取素材，生成 out/report.md",
  "files": ["evals/fixtures/input.txt"],
  "expected_skills": ["deliverable-pack"],
  "expect_tools": ["read", "write"],
  "expect_artifacts": ["out/report.md"],
  "severity": "high"
}
```

可用字段包括：

- `expected_skills`：正确激活集合，空列表表示 No-Skill；
- `expect_tools`：必须观察到的 tool；
- `expect_artifacts`：本轮必须新增或修改的文件 glob；
- `forbid_artifacts`：明确要求不能留下文件；
- `files`：复制到独立 workspace 的只读输入；
- `expect_assertions`：需要独立 LLM judge 判断的语义断言；
- `turns`：后续轮，每轮可声明自己的 `expect_*`；
- `severity: critical`：纳入 Critical Skill Miss Rate。

### Suite 中最重要的字段

```yaml
skills:
  dir: subjects
  target: [pdf]                 # 这套实验归谁
  include: [pdf, docx, xlsx]    # 模型/agent 实际能看到谁
  versions: {pdf: v2}           # 可选：钉住单个 skill 版本
  mode: routing_only            # none | routing_only | full
  cfg: v2                       # 结果目录中的实验标签
```

优先使用 `include` 明确 catalog，避免以后新增本地 skill 时实验集合漂移。
`target` 是归属，不等于候选集合。No-Skill 基线仍保留 `target`，但从 `include` 中移除目标。

suite 顶层 `tools` 是 full eval 的运行时强 allowlist。仓库 Docker 示例使用
`tools: ["*"]`，因为容器才是它的文件系统、网络和资源边界；需要缩小 agent 能力时可写
`tools: [read, write]`。空列表会临时写入 `tools.deny: ["*"]`，即禁止所有 tool。
写入、回读校验或恢复失败都会让该次运行失败关闭，不会在权限未落实时继续。同一台机器
上共享 local profile 的线程和 pipeline 进程也会使用同一把文件锁，避免临时策略互相覆盖。

它与 case 的 `expect_tools` 分工不同：

- suite `tools`：允许 agent 使用什么，是执行权限；`["*"]` 表示完整 toolset；
- case `expect_tools`：这道题应该实际调用什么，是评分 gold。

OpenClaw profile 中原有的更严格 deny 仍然生效，suite 不会放宽它。allowlist 限制的是
tool 名称，不是 tool 的副作用：如果允许 `exec`，agent 仍可能通过 shell 读写文件或访问网络。
不可信 skill 还应使用 Docker environment 的网络、挂载和资源策略。

---

## 结果、对照与改进

每个模型和 execution 都有独立目录：

```text
outputs/<dataset>__<model>__<skillcfg>/<execution-id>/
├── config.snapshot.yaml     # suite、hash、runtime/environment、resolved versions
├── inputs/                  # 实际 dataset 与 SKILL.md 副本
├── runs.jsonl               # 不可变原始结果
├── trajectory.jsonl         # 可观察执行事件投影
├── grading.<judge-id>.json  # 可选语义评分
├── trajectory_grading.<judge-id>.json # 可选通用轨迹 judge
├── scores.json
└── report.html
```

### 比较 V1 / V2

复制旧版本，新建 `v2`，不要原地修改已经跑过的 `v1`：

```bash
cp -R subjects/<skill>/v1 subjects/<skill>/v2
```

复制 suite，只改变：

```yaml
skills:
  versions: {<skill>: v2}
  cfg: v2
```

运行后比较：

```bash
.venv/bin/python -m workflows.compare_runs \
  outputs/<v1-run>/<execution-id> \
  outputs/<v2-run>/<execution-id>
```

工具会报告 delta、`config_hash` 差异、配置污染和 judge 尺子差异。

### 可选语义 judge

确定性检查适合 tool、文件、MIME、hash 和 schema；“事实有没有编造”“要求是否完整覆盖”
需要独立 judge。suite 配置 `scoring.judge` 后：

```bash
.venv/bin/python -m pipeline run \
  --suite <suite.yaml> \
  --stages run,grade,score \
  --confirm \
  --confirm-egress
```

`grade` 是额外付费调用，因此默认不执行。judge 应与被测模型使用独立模型、端点和 key。
未经人工标注校准的 judge 分默认不应决定发布 gate。

### 从失败轨迹生成改进建议

先预览外发 manifest；没有 `--confirm-egress` 不会调用模型：

```bash
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/<run>/<execution-id>
```

确认后生成聚类建议：

```bash
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/<run>/<execution-id> \
  --confirm-egress
```

只有显式增加 `--apply` 才会新建 `subjects/<skill-id>/v<N+1>/` 和同题复验 suite；
源版本不会被覆盖。停止条件支持 gate PASS、累计 token/墙钟预算和最大迭代数。

### 归档私有实验

```bash
.venv/bin/python -m pipeline archive --subjects <skill-id>
.venv/bin/python -m pipeline archive --subjects <skill-id> --confirm
```

归档会打包并校验 subject 的版本、suite、dataset、结果和 lineage；详见
[evals/RUNBOOK.md](evals/RUNBOOK.md)。

---

## 隐私、安全与可复现性

仓库只跟踪 routing 和 full 两套合成示例。以下内容默认被 `.gitignore` 排除：

- `.env` 和本地模型额度记录；
- `installed_skills/` 与非示例 `subjects/`；
- 非示例 datasets、suites、fixtures 和 drafts；
- `outputs/`、`archives/` 与 `.local/`。

提交前建议检查：

```bash
git status --ignored --short
git diff --cached --check
git grep --cached -n -I -E \
  '(sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{16}|github_pat_[A-Za-z0-9_]{20,})' \
  || true
```

重要边界：

- `pipeline plan` 不发模型请求；
- 真实 run 会明确列出 endpoint、请求数和 payload 类别；
- routing-only 只发送 skill metadata；
- full eval 会发送题目、所选 `SKILL.md` 正文和 tool catalog；
- API key 值不进入 suite、快照、报告或模型 payload；
- 第三方 skill 可能包含脚本和提示注入，导入前必须人工审查；
- suite 的 `tools` 会强制映射为 OpenClaw tool policy，并在请求后恢复；
- 允许 `exec` 等广义 tool 仍会带来间接文件/网络能力，tool policy 不等于系统沙箱；
- local environment 是独立 workspace，但不是强安全沙箱；运行不可信 skill 时使用 Docker backend。

`config_hash` 包含会影响结果的 suite 字段、dataset hash、skill hash、模型参数、
runtime fingerprint 和 environment 配置。hash 不一致的两次运行不能直接当作单变量 A/B。

---

## 架构与文档导航

```text
Experiment suite
      ↓
InvocationRequest
      ↓
Environment Backend
      ↓
Runtime Adapter
      ↓
RunResult
      ↓
Evaluator factory
      ↓
Outcome / Trajectory / Reliability / Efficiency
      ↓
scores.json + report.html + regression delta
```

| 文档 | 用途 |
| --- | --- |
| [evals/AUTHORING.md](evals/AUTHORING.md) | 写题、命名、版本、gold 和人工审核 |
| [evals/RUNBOOK.md](evals/RUNBOOK.md) | suite、运行、对照、产物与归档 |
| [evals/CASEGEN.md](evals/CASEGEN.md) | 自动出题和改进建议的人审规范 |
| [evals/TRAJECTORY.md](evals/TRAJECTORY.md) | 通用 trajectory 数据、四层指标和 judge |
| [OPENCLAW.md](OPENCLAW.md) | OpenClaw 从零配置、健康检查、Docker 与排障 |
| [AGENTS.md](AGENTS.md) | 完整架构、阶段验收标准和未完成项 |

主要代码：

```text
contracts/                  严格数据契约
adapters/runtimes/          litellm / openclaw / mock
adapters/routing_inputs/    direct / production_context
environments/               local / docker
pipeline/                   init / plan / run / archive / unarchive
workflows/                  执行、评分、judge、compare、suggest
evaluators/                 outcome / trajectory / reliability / efficiency 注册表
tests/                      回归与契约测试
```

---

## 当前状态与限制

| 能力 | 状态 |
| --- | --- |
| Routing eval、No-Skill、multi-skill、生产上下文 | ✅ |
| OpenClaw full eval、tool/artifact、错误分类、suite tool 强 allowlist | ✅ |
| 多轮、session/context/file 延续、conversation 并发 | ✅ |
| V1/V2 delta、污染检测、版本不可变检查 | ✅ |
| 自动生成 routing 草稿 + 人工审核门 | ✅ |
| 失败证据 → 建议 → 新版本 → 同题复验 | ✅ |
| 独立语义 judge 与证据引用 | ✅，校准注册仍在完善 |
| Docker backend | 🚧 已支持固定镜像、逐 request 容器、只读 skill mount、disabled/full 网络和资源限制；mock/allowlist 未完成 |
| Evaluator 注册表 | ❌ |
| 全过程 Viewer | ❌，当前使用 `report.html` 和结构化 JSON |

这个项目仍处于 eval 系统开发期。一次用 10–20 道高质量题验证指标和归因机制，
比堆 100 道普通正向题更有价值。

License: [GPL-3.0](LICENSE)
