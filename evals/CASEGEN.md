# 出题与改进建议：薄脚本 + 人工审核门

> P2 已有薄入口 [`workflows/gen_cases.py`](../workflows/gen_cases.py)：只负责 LiteLLM 调用、Pydantic 校验、
> 生成 `dataset.jsonl` + `suite.yaml` 草稿，然后停在人审门。P3 已启动
> [`workflows/suggest.py`](../workflows/suggest.py)：读取失败原文、聚类并写建议报告，但不改 skill；
> 不提前搭无人值守迭代框架。
>
> 相关：写题规范 [AUTHORING.md](AUTHORING.md)｜怎么跑 [RUNBOOK.md](RUNBOOK.md)｜
> 阶段计划 [../AGENTS.md](../AGENTS.md) §★★★

---

## 0. 为什么只写薄脚本

P2 已经出现了三个值得固化的重复动作：跨题坏题校验、生成 provenance、严格 suite 草稿。
所以现在写 `workflows/gen_cases.py`，但不自建 CaseGenerator 框架；模型调用、Schema 和 YAML
分别直接复用 LiteLLM、Pydantic 和现有 Suite Contract。**人在回路**仍是硬要求：

* 模型生成的 gold **不是真值**（AGENTS.md ★★★ ① 的硬约束）；
* 自动改写 skill 会让系统为了过测试而**过拟合题库**；
* 人工审核门就是 `git diff`，不需要为 review 建 UI（P2「明确不做」）。

生成产物先进入 `evals/drafts/`，头部明确标 `DRAFT`；人工确认后才能移动到
`evals/datasets/` / `evals/suites/`。脚本不会自动运行 eval。

最薄 smoke 可以用 `--count 3`，覆盖 `pos / amb / rej` 三类；但如果显式加了
`--include-neighbors`，最小合法值会变成 `4`，因为这时还必须覆盖 `multi`。
生成完成后，推荐的人审顺序是：先看 `REVIEW.md`，再看 `case-diff.json`（如果传了
`--reference`），最后才决定是否把 DRAFT 提升为 `APPROVED`。

### 0.2 rej gold 的盲判交叉复审

生成之后会**再调一次模型**，把刚生成的每道 `rej` 题面送回去盲判：只给 catalog metadata
和题面，**不给 gold、不给生成器自己写的 `why_not`**，问的是「这些请求里哪些 skill 该激活」。
换提问方向是关键 —— 同一个问题问第二遍只会得到同一个答案，那不叫复审。

复审点名了 skill，就说明这道题的 `gold=∅` 很可能是错的。这类错不是少一道题：模型答对反而
被记成误激活，`false_activation` 结构性虚高，整批结论是反过来的。所以异议会顶到
`REVIEW.md` 最前面，排在生成器自述的 `rejection_notes` **之前**。

它只标注、不阻断 —— 谁对谁错是语义问题，代码判不了，而草稿本来就停在人审门；复审的作用
是让人知道该先看哪几道题。复审自己失败（模型挂了、JSON 不合法、点名了 catalog 里不存在的
skill_id）也照样写出草稿，但会在 dataset 头部和 `REVIEW.md` 里标成 `FAILED`，**不会
伪装成「无争议」**。`--skip-rej-review` 可以省掉这次调用，那时两处都会标 `SKIPPED`。

结论进 dataset 头部的 `# rejection_review:` 一行，跟着题集走 —— 只写在 `REVIEW.md`
里的话，题集一被复制到 `evals/datasets/` 就丢了。

### 0.1 命令

```bash
.venv/bin/python -m workflows.gen_cases \
  --skill-dir subjects/interactive-architecture-diagram/v1 \
  --acceptance "明确要求把结构或流程转成图时激活；只做内容分析时拒绝"
```

真实调用会把所选 skill 的 metadata 与验收标准发送到 suite 配置的模型端点。
涉及私有仓库时，必须先确认允许向该端点发送这些内容。

---

## 1. 出题：给 Claude Code 的输入与产出约定

### 1.1 输入必须齐三样（缺一不要开始）

| 输入 | 说明 |
| --- | --- |
| skill 源目录 | `subjects/<skill-id>/<vN>/SKILL.md`，连正文一起给。这里要的是**被测快照目录**，不是 `installed_skills/<slug>/` 这种上游安装目录 |
| **业务目标与验收标准** | 「做完什么算对」。这一条最容易被省掉，省掉了生成的就只有正向题 |
| 输入样例 / 外部依赖 | 有就给；没有就明说「任务自包含，数据内联在 prompt 里」 |

### 1.2 产出必须满足的硬约束

生成的 JSONL **逐条**要能过 `contracts/evalcase.py` 的校验，并且：

1. **题目只写业务目标，不写文件格式。**
   把「两个文件 / `out/` / 固定表头」写进 prompt，等于替模型把答案抄好了 ——
   No-Skill 基线也会做对，delta 归零，这批题就白写了。格式约束是 **skill 正文**的职责。
2. **配比是精确契约**：单 skill 默认按 `pos 40% / amb 30% / rej 30%`，多 skill catalog
   按 `pos 40% / amb 30% / rej 20% / multi 10%` 做 largest-remainder 分配，且每个必需类型
   至少一题。生成 prompt 会写出整数目标，返回数量必须逐类相等；不能用“每类至少一道”把
   30 题交成 `15 pos / 1 amb / 14 rej` 后仍声称 balanced。`multi` 只在 catalog 至少有两个
   skill 时要求。
3. **ID 唯一且永不复用**，命名照 [AUTHORING.md](AUTHORING.md) §1.2。
4. **gold 只能指向真实存在的 skill_id**（拿 `subjects/*/*/SKILL.md` 的 `name:` 核对）。
5. **同一个 prompt 不能出现两次**，更不能同 prompt 不同 gold。
6. full 题的 `expect_artifacts` / `expect_tools` 留空 = 该维度 **N/A**，不是「必须为零」。
7. 开发期**一次不超过 10 题**（AGENTS.md §★★）。质量 > 数量。

### 1.3 人工审核清单（`git diff` 时逐条过）

- [ ] 每道 `amb` 题，我能说出它「模糊在哪」——说不出就是假边界题，删掉
- [ ] 每道 `rej` 题，我确认它**真的**不该激活目标 skill（而不是我懒得写 gold）
- [ ] `REVIEW.md` 顶部若有交叉复审异议，我逐条给了结论：改 gold（题型跟着改成
      `amb`/`multi`）、换题，或者写下为什么复审是错的 —— 不许留着不处理就批准
- [ ] 期望产物的路径/后缀和 skill 正文里写的**逐字一致**
- [ ] 没有一道题在 prompt 里泄漏了 skill 正文才该有的格式约定
- [ ] 数据集头部注释写清了：谁生成的、基于哪版 SKILL.md、生成日期

> 生成过程本身要可复现：把**用的模型 + 生成日期 + 基于哪个 skill content_hash**
> 写进数据集文件头部的 `#` 注释行（`load_cases` 会跳过 `#` 开头的行）。

### 1.4 生成失败不是丢弃已付费响应

生成结果若 schema、gold、题数或精确配比不合法，生成器会把原始约束、候选输出与机器错误
发回同一模型，做**最多一次**结构化 repair；因此 `pipeline init` 的 egress plan 按最大值
申报三次请求：生成、条件式 repair、rej 盲判复审。

- repair 成功：`<draft>/generation/` 保留每次 `*.raw.txt`、可解析的
  `*.candidate.json`、SHA-256 和校验错误；正式 dataset 仍是 DRAFT。
- repair 仍失败：只写
  `<draft>/generation_failures/<timestamp>/` 与 `RECOVER.md`，**不写可运行的 JSONL/suite**。
  用户可以从 candidate 手改一份新数据集，也可原命令重跑；failure bundle 不阻断重跑，且
  历史响应不覆盖。
- 首次模型调用本身未返回内容（认证、额度、网络错误）：没有候选可修，留给 provider 错误
  路径处理，不虚构空 DRAFT。

原始响应可能回显业务验收文字或用户题面，按用户输入处理；draft/failure 目录不得公开提交，
分享前必须人工检查和脱敏。

---

## 2. 改进建议：读失败证据，不读分数

### 2.1 输入

跑完之后的 `outputs/{run}/runs.jsonl` + `scores.json`，以及被测的 `SKILL.md`。
**给失败 case 的原始输出，不要只给汇总分数** —— 分数说明不了改哪句。

最短命令现在是：

```bash
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/<dataset>__<model>__<skillcfg>/<execution_id> \
  --confirm-egress
```

默认会先复用 source run 的模型配置，并优先从 `config.snapshot.yaml` /
`inputs/skills/` 这份**运行归档副本**自动推断目标 `SKILL.md`。只有推断不唯一时，
才补：

```bash
.venv/bin/python -m workflows.suggest --run-dir outputs/<...>/<execution_id> \
  --skill-id <skill-id> --confirm-egress
```

或：

```bash
.venv/bin/python -m workflows.suggest --run-dir outputs/<...>/<execution_id> \
  --skill-file subjects/<skill-id>/v1/SKILL.md --confirm-egress
```

### 2.2 产出必须满足的硬约束

1. 每条建议带齐四样：`case_id` + **模型原文证据** + 对应评分维度 + 具体改哪句。
2. **按失败模式聚类**，不是每道错题一条。10 道错题不该产出 10 条建议 ——
   产出 10 条就说明没聚类，退回重来。
3. **不自动改写 skill。** 建议输出完停下等确认，确认后再落 `subjects/<skill-id>/v<N+1>/`（新版本目录，旧版一个字不改）。
4. 复验必须跑**同一个 suite**，`config_hash` 只允许 `skillcfg` 一项变。
   跑完用 `workflows/compare_runs.py` 看，**出现 `[⚠️ 污染]` 就说明 delta 混了多个原因，别信那个数。**

### 2.3 能力基准（判断这套做法有没有用）

拿 **pdf V1** 当输入，看能不能自己提出「按**任务类型**而非**文件格式**激活」这一条 ——
这是 HANDOFF §4.2 里人工得出的结论。提不出来，说明证据给得不够或聚类没做。

---

## 3. 停止条件（对话里也要守）

三选一，触到就停，并把停在哪一条记下来：

* gate PASS
* 预算上限（token / 钱 / 墙钟时间）
* 最大迭代数

**没有停止条件的迭代 = 烧钱不收敛。** 对话模式下最容易破的就是这条 ——
「再改一版试试」很便宜，所以要提前写死改几版。

`workflows.suggest` 会在任何建议模型调用之前读取 `scores.json.gate_pass`，
并把整条迭代 lineage 的 token / 墙钟时间累加进 `suggestions.json`：

```bash
# 第一轮：预算在这里写死；未确认外发时只打印 manifest
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/<v1-run> \
  --max-total-tokens 200000 \
  --max-total-seconds 1800 \
  --confirm-egress

# 复验评分后：继承上一轮预算与 max_iterations，不得中途修改
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/<v2-run> \
  --previous-report outputs/<v1-run>/improvements/round-01/suggestions.json
```

`stop_reason` 的主原因优先级是 `gate_pass` → `budget_exceeded` →
`max_iterations`，同时命中的原因都会保存在 `triggered_stop_reasons`。一旦命中，
即便传了 `--apply` 也不会调用模型或创建新版本。每轮仍须人显式发起，不存在无人值守循环。

---

## 4. 什么时候才继续抽象

对话够用，直到出现下面任意一条：

| 信号 | 为什么这时才值得写 |
| --- | --- |
| 同一套出题 prompt **手动重复了三次以上** | 复用成本超过了写脚本的成本 |
| 需要**批量**对多个 skill 出题 | 人肉循环开始出错 |
| 需要把生成过程本身纳入 `config_hash` | 手写的元信息注释会漏，得由代码保证 |
| 建议环节要接进 CI / 无人值守 | 对话进不了流水线 |

在此之前，不建 CaseGenerator 注册表、不建审核 UI、不做无人值守循环。P2 的
`workflows/gen_cases.py` 与 P3 的 `workflows/suggest.py` 都保持薄胶水；等 P3 出现真实第二轮复验后，
再判断哪些共同点值得在 P4 抽成注册表。
