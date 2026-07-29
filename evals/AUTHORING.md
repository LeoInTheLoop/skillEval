# 测试集编写指南

写题、起名、定版本，只看这一份。不用翻 README，不用猜。

> 配置（模型 / tool / skill 版本 / 评分门槛）存在哪、怎么跑、怎么比 → 见 [RUNBOOK.md](RUNBOOK.md)。

---

## 1. 命名规范

四层名字，从测试集一路贯到产物文件。**段内用 `-`，段间用 `_`，维度间用 `__`。**

### 1.1 测试集文件

```
evals/datasets/{kind}_{scope}_v{major}.{minor}.jsonl
```

| 段 | 取值 | 说明 |
| --- | --- | --- |
| `kind` | `routing` / `multiturn` / `effect` / `safety` | 测什么能力 |
| `scope` | skill_id / `a+b` / `all` | 覆盖哪些 skill；全量写 `all` |
| `v{major}.{minor}` | `v1.0` | **测试集自己的版本**，与 skill 版本无关 |

```text
evals/datasets/routing_all_v1.0.jsonl          # 全 skill 路由集
evals/datasets/routing_pdf_v1.0.jsonl          # 只针对 pdf 的路由集
evals/datasets/multiturn_pdf+xlsx_v1.0.jsonl   # pdf→xlsx 多轮
```

> ⚠️ 这里的 `v1.0` 是**题目**的版本。skill 描述的 V1/V2 不进文件名，它是实验配置，进 output 目录名（§1.4）。两个 v 不要混。

### 1.2 Case ID

```
{scope}-{type}-{seq}
```

| 段 | 取值 | 说明 |
| --- | --- | --- |
| `scope` | 期望激活的 skill_id；多个用 `+`；一个都不该激活写 `none` |
| `type` | `pos` 正向 / `amb` 模糊边界 / `multi` 多 skill / `rej` 应拒答 |
| `seq` | `01` 起两位数，同 scope+type 内递增 |

```text
pdf-pos-01          期望激活 pdf，正向题
pptx-amb-03         期望 pptx，但和别的 skill 边界模糊
pdf+xlsx-multi-01   期望同时激活 pdf 和 xlsx
none-rej-02         期望空列表（No-Skill）
```

**ID 在测试集内唯一且永不复用**：题改了就 bump 测试集版本（§4），不要把 `pdf-pos-01` 换成另一道题。

### 1.3 Turn 与 Run ID

```
run_id = {case_id}.t{turn}.r{repeat}       turn 从 1 起，repeat 从 0 起
```

```text
pdf-pos-01.t1.r0     单轮题第 0 次重复（单轮也写 .t1，不省略）
pdf+xlsx-multi-01.t2.r1   多轮题第 2 轮，第 1 次重复
```

只用 `. - + _` 和字母数字 —— 同一个字符串既能当 JSON 字段，也能直接当目录名，不需要两套转义。

`runs.jsonl` 会同时落 `request_id`、`case_id`、`turn_index`、`repeat_index` 和
`session_id`。同一 case/repeat 的各 turn 共用 session；不同 repeat 的 session 与
workspace 完全隔离。

### 1.4 Output 目录

```
outputs/{dataset}__{model}__{skillcfg}/
    ├── runs.jsonl
    ├── report.html
    └── artifacts/{run_id}/<产物原名>
```

| 维度 | 取值 | 说明 |
| --- | --- | --- |
| `dataset` | 测试集文件名去掉 `.jsonl` | `routing_all_v1.0` |
| `model` | suite 里该模型的 `id` 短名 | `qwen3-max` |
| `skillcfg` | `none` / `v1` / `v2` / … | **skill 描述的版本**，No-Skill 基线写 `none` |

```text
outputs/routing_all_v1.0__qwen3-max__v1/     # V1 描述
outputs/routing_all_v1.0__qwen3-max__v2/     # V2 描述 → 和上面比 delta
outputs/routing_all_v1.0__qwen3-max__none/   # No-Skill 基线
outputs/routing_all_v1.0__gpt-4o__v1/        # 换模型
```

三个维度都进目录名，所以**换模型、换 skill 版本、换测试集都不会互相覆盖** —— 这正是 V1/V2 对照要的。目录里还有一份 `config.snapshot.yaml`（完整配置 + `config_hash`），比 delta 前先对 hash，见 [RUNBOOK.md](RUNBOOK.md) §3。

---

## 2. Case 字段

一行一个 JSON（JSONL）。字段定义见 [contracts/evalcase.py](../contracts/evalcase.py) 的 `RoutingCase`，**多写字段会被拒绝**（`extra="forbid"`）。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 按 §1.2 |
| `prompt` | ✅ | 用户原话。**不许出现 skill_id、skill 名、文件扩展名以外的技术词** |
| `expected_skills` | ✅ | 期望激活的 skill_id 列表；**空列表 = 期望拒答** |
| `tags` | | **只认这四个枚举**：`["positive"]` / `["ambiguous"]` / `["multi-skill"]` / `["no-skill"]`。自由文本标签没法聚合，`gen_cases` 会把生成器随手写的标签规范化掉 |
| `severity` | | `low` / `medium` / `high` / `critical`，默认 `medium`。`critical` **真的会进指标**（见下） |
| `files` | | 输入素材，相对仓库根的路径（放 `evals/fixtures/`）。跑的时候按 basename **只读**复制进 workspace；声明了但文件不存在 → `pipeline plan` 直接拦下 |
| `expect_artifacts` | | **full eval 专用**：必须产出的文件，相对 workspace 的路径 glob |
| `expect_tools` | | **full eval 专用**：必须调到的 tool 名，如 `["write"]` |
| `forbid_artifacts` | | **full eval 专用**：这题**不该留下任何文件**（拒答题用），默认 `false` |
| `expect_assertions` | | **语义断言**（逐题写），交给 judge 模型判 —— 见 §2.2。代码判得了的一律别写这里 |
| `reference` | | 参考答案。只有 `correctness` 维度用它；不写的题该维度记 N/A —— 见 §2.3 |
| `expect_workspace_files` | | **full 多轮专用**：本轮结束时 workspace 中必须仍存在的路径 glob；可检查上一轮未修改的文件 |
| `requires_context` | | **full 多轮专用**：本轮必须依赖此前对话才能完成；用于 `context_retention` 指标 |
| `turns` | | **full 多轮专用**：从第 2 轮开始的数组，每轮可写自己的 prompt/files/expect_* |

`severity` 的用法：`critical` 表示"这题错了就不能发版"。它会算成
**`critical_miss`**（critical 题里判错的比例），routing 与 full 两侧都出这个数，
可以直接写进 `scoring.gate`（AGENTS.md §22.4 的默认门槛是 `critical_miss: "<= 0"`）。
一道 critical 题都没有时该指标记 N/A，不按 0。日常正向题给 `medium`，
每个 skill 挑 1 道最典型的给 `high`。

```jsonl
{"id": "meeting-and-brief-pos-01", "prompt": "workspace 里的「项目周会-文字稿.txt」帮我按正式格式整理出来存档。", "files": ["evals/fixtures/项目周会-文字稿.txt"], "expected_skills": ["meeting-and-brief"], "expect_tools": ["read", "write"], "tags": ["positive"], "severity": "critical"}
```

> `files` 出现之前，「给助手一份文字稿」这类题只能把内容**整段内联进 prompt**，
> 跟真实用法（丢一个文件过去）差得很远，测出来的东西要打折。

```jsonl
{"id": "pdf-pos-01", "prompt": "这份扫描版合同里有几张表格，帮我把表格数据抽出来存成 csv", "expected_skills": ["pdf"], "tags": ["positive"], "severity": "high"}
{"id": "none-rej-01", "prompt": "帮我把这段话翻译成英文", "expected_skills": [], "tags": ["no-skill"], "severity": "medium"}
```

### 2.1 full eval 的两个断言字段

只有 `skills.mode: full` 的题才需要填。评分逻辑见 [../workflows/score_full.py](../workflows/score_full.py)。

```jsonl
{"id": "deliverable-pack-pos-01", "prompt": "Q3 三个大区的销售额出来了：…帮我出一份交付包，短名用 q3-sales。", "expected_skills": ["deliverable-pack"], "expect_artifacts": ["out/q3-sales.csv", "out/q3-sales.md"], "expect_tools": ["write"], "tags": ["full", "artifact"], "severity": "high"}
```

四条规矩，破一条这批题就白写：

1. **`expect_*` 留空 = 该维度 `N/A`，不是「必须为零」。** 不适用的维度记 N/A、不按 0 分处理，
   否则 gate 会被结构性拉低（AGENTS.md ★★★ ③）。
2. **要表达「不该落文件」，用 `forbid_artifacts: true`，不要靠留空。**
   踩过：拒答题三个维度全留空 → 全是 N/A → 模型写一屋子文件也算它过，等于只做了冒烟。
3. **prompt 里只写业务目标，不写文件格式。**「两个文件 / `out/` / 固定表头」是
   **skill 正文**的职责。写进题面 = 替模型把答案抄好了，No-Skill 基线也会做对，
   delta 归零 —— 这批题就测不出任何东西。
4. **产物断言 = 存在 + 非空 + MIME。** MIME 由**后缀**推出，所以它抓「名字对但类型不对」，
   抓不到「叫 `.csv` 其实内容是 HTML」。内容级校验等 P4 的 Judge。

> ⚠️ **不要用 `expected_skills` 判 full 题的激活。** OpenClaw 的 `loaded_skills` 是
> 「**注入进 system prompt** 的目录」，不是「模型激活了它」——
> 实测拒答题里模型什么都没做，目标 skill 照样出现在 `loaded_skills` 里。
> 它在 `workflows/score_full.py` 里只作为 `skill_injected` **体检项**（验证注入生效），
> **不进 gate、不进任务完成度**。full 题的激活情况只能靠产物/tool 断言间接判断。

### 2.2 `expect_assertions`：确定性断言判不了的才写这里

一条一句**可判真假**的话，由 [../workflows/grade.py](../workflows/grade.py) 交给 **judge 模型**逐条判 pass/fail
并给出原文证据。评分维度是 `assertion_pass_rate`，可以进 gate。

```jsonl
{"id": "deliverable-pack-pos-01", "prompt": "…", "expected_skills": ["deliverable-pack"], "expect_artifacts": ["out/q3-sales.csv"], "expect_tools": ["write"], "expect_assertions": ["回答里说明了三个大区各自的销售额，数字与用户给的原始数据一致", "没有把用户没提过的地区写进结果"], "tags": ["full"], "severity": "high"}
```

四条规矩：

1. **能用 `expect_artifacts` / `expect_tools` 表达的，绝不写成 assertion。**
   那些是代码能确定的事；交给模型判只会引入抖动、花钱，还判得更差（§21.2 确定性断言优先）。
   assertion 只用来问「内容对不对、有没有编、说清楚了没有」这类代码判不了的。
2. **一条只问一件事。** 「格式对且数字准且没编造」判 failed 时你分不清是哪一截坏了。
3. **写成可证伪的陈述句，不写评价词。** ❌「报告质量高」 ✅「报告给出了每个大区的同比变化率」。
   judge 拿不到可引用的证据时按 failed 处理，模糊的断言只会稳定失败。
4. **文本产物内容会截取前 4000 字符给 judge；二进制仍只有元数据。**
   需要读取 docx/png 内容才能确认的断言会按「证据不足」判 failed，不许猜。

**judge 是可换的，而且默认跟被测模型用不同的 env。** 配置见 suite 的 `scoring.judge`
（[suites/example_routing.yaml](suites/example_routing.yaml) 有 routing 侧的完整示例；
[suites/example_full.yaml](suites/example_full.yaml) 是可直接执行的 full 基础示例；换成
自己的 skill 后再按业务需要增加 judge）。
**换 judge 就是换尺子**，同一批 run 用两个 judge 判会得到两个不同的 `assertion_pass_rate`；
两者不可直接比较，`workflows/compare_runs.py` 会报 `[⚠️ 尺子不同]`。

### 2.3 标准评估维度：跨题通用，不用逐题写

`expect_assertions` 要一题一题写；**维度不用** —— 在 suite 里选几个，对所有题一起生效。
定义在 [../workflows/dimensions.py](../workflows/dimensions.py)，查看全部：`python -m workflows.grade --list-dimensions`。

| 维度 | 问什么 | 需要 | 定义来源 |
| --- | --- | --- | --- |
| `faithfulness` | 输出的事实是否都能在输入里找到依据（有没有编造） | — | RAGAS faithfulness / DeepEval FaithfulnessMetric |
| `completeness` | 用户要求的每一项是否都覆盖了 | — | AGENTS.md §21.6「完整性」 |
| `relevancy` | 是否切题，有没有大段无关内容 | — | RAGAS answer_relevancy |
| `instruction_following` | 显式约束（格式/范围/禁止项）守住了吗 | — | G-Eval 常见 criteria |
| `correctness` | 与参考答案是否有事实冲突 | **`reference`** | autoevals Factuality |
| `conciseness` | 有没有冗余灌水 | — | G-Eval coherence/fluency 的实用变体 |

```yaml
scoring:
  judge:
    id: glm5
    model: openai/glm-5.1
    dimensions: [faithfulness, completeness, relevancy]   # 默认这三个
```

四条要点：

1. **0–1 连续分，不是 pass/fail。** 语义质量有程度之分，二元判定会把
   「基本做到了但漏一项」和「完全没做」压成同一个数字。
2. **判不了的记 N/A，不进分母。** `correctness` 需要 `reference`，没写的题直接跳过 ——
   「10 题里只有 3 题有参考答案」不会把 correctness 腰斩。跳过项会打印出来。
3. **维度名拼错在运行前就被拒**，不会静默少判一个维度（那会让分母悄悄变小）。
4. **改了 rubric 要 bump 该维度的 `version`。** 版本进判定产物，
   `workflows/compare_runs.py` 靠它识别「模型没换但判定标准变了」。

**维度分默认不进 gate。** 它们是 judge 打的分，未经校准不该决定发版
（AGENTS.md §22.6：judge vs 人工一致率 ≥ 80% 才够格）。硬要放进 `scoring.gate`
也不会被拦，只会在评分时提示 `judge-uncalibrated`。

### 2.4 Full eval 多轮题

顶层字段永远是第 1 轮；`turns` 只写后续轮次。后续轮不要重复粘贴历史，模型应通过
同一个 session 自己记住；否则测到的是 prompt 重复，不是上下文保持。

```jsonl
{"id":"deliverable-pack-pos-02","prompt":"先读取素材并生成 out/draft.md","files":["evals/fixtures/input.txt"],"expected_skills":["deliverable-pack"],"expect_artifacts":["out/draft.md"],"turns":[{"prompt":"沿用刚才的数据，把初稿改成最终版","requires_context":true,"expect_artifacts":["out/final.md"],"expect_workspace_files":["out/draft.md","out/final.md"],"expect_assertions":["最终版保留了第一轮确认的关键数字"]}],"severity":"high"}
```

- `expect_artifacts` 只检查**本轮新增或修改**的文件。
- `expect_workspace_files` 检查本轮结束时仍存在的全部文件，包括上一轮留下但本轮没改的。
- 每轮可新增 `files`，它只会在该轮开始前只读注入；不会提前泄漏给前一轮。
- 上一轮 runtime/task 执行失败后，后续轮记 `skipped`，不进入 turn/artifact/tool 分母。
- `task_completion` 按完整 conversation 计一次；`turn_completion` 才按实际执行的轮次计。
- `parallelism` 写在 suite，控制同时执行多少段独立 conversation；同一 conversation
  内永远串行，不允许 turn 乱序。

---

## 3. 一个路由测试集要有什么题

### 3.1 配额

按 skill 数 N 算：

| 题型 | 数量 | 目的 |
| --- | --- | --- |
| `pos` 正向 | 每 skill ≥ 4 | 基本召回 |
| `amb` 模糊边界 | **≥ 总题数 20%** | 区分度的唯一来源 |
| `rej` 应拒答 | ≥ 总题数 20% | 误激活率 |
| `multi` 多 skill | ≥ 3 | Skill-Set 精确匹配 |

README 的基准规模是 20 skill × 100 题 × 3 次重复。

### 3.2 四种题怎么写

**`pos` 正向** —— 描述真实任务，让人一看就知道该用哪个 skill，但**别提 skill 名**。

```text
✅ "把这三个报告文件合并成一个，都是那种带页码的电子版"     → pdf
❌ "用 pdf skill 合并文件"                                  泄漏 skill_id
❌ "合并文件"                                               没有可判定的信号
```

**`amb` 模糊边界** —— **这是最重要的一类，也是最容易偷懒不写的一类。** 专挑两个 skill 的交界处出题，逼出 description 写得好不好。

```text
"把这份材料整理成能给客户看的东西"          docx? pptx?
"这些数据帮我做个图表放报告里"              xlsx? docx? pptx?
"做个页面把季度数据展示出来"                artifacts-builder? xlsx?
```

出 `amb` 题时 `expected_skills` 必须**能说出理由** —— 说不清就是题坏了，不是模型坏了。

**`rej` 应拒答** —— 正常请求，但现有 skill 一个都不该激活。

```text
"帮我把这段话翻译成英文"
"解释一下什么是复利"
"推荐几本讲谈判的书"
```

不要写成"明显超纲/胡言乱语"，那种题谁都能拒答，测不出东西。要写**贴着 skill 边缘但确实不该激活**的。

**`multi` 多 skill** —— 一句话里有两个明确子任务。

```text
"把 pdf 里的表格抽出来，做成带公式的汇总表"   → ["pdf", "xlsx"]
```

### 3.3 坏题自查

写完每道题过一遍：

- [ ] prompt 里没有 skill_id、skill name、触发词原文
- [ ] 换个人读，能独立推出同样的 `expected_skills`
- [ ] `amb` 题的答案说得出理由
- [ ] `rej` 题不是"明显超纲"，是"贴边但不该激活"
- [ ] 没有和已有题语义重复（重复题只会虚高准确率）

**如果新一批题跑出来还是 100%，八成是 `amb` 配额没写够，不是模型太强。**

---

## 4. 什么时候 bump 版本

历史版本**不覆盖、不删除**（README §25.4），并排放。

| 改动 | 动作 |
| --- | --- |
| 加题 | minor +1 → `v1.1` |
| 改已有题的 prompt / expected | minor +1，并在文件头注释写改了哪些 id |
| 删题、改 id 含义、改字段结构 | major +1 → `v2.0` |
| 只改 tags / severity | 不 bump |

跨版本比指标前先确认题目集合可比 —— minor 加题会让绝对值不可直接对比，看每题明细。

---

## 5. 生成新测试集（可直接喂给模型的模板）

优先用仓库的薄生成入口，它会在写文件前检查重复 id / 重复 prompt / 同 prompt 不同 gold /
gold 指向不存在的 skill / 题型缺类，并用现有严格契约生成 suite 草稿：

```bash
.venv/bin/python -m workflows.gen_cases \
  --skill-dir subjects/<target-skill>/v1 \
  --acceptance "这里写业务目标与做对的标准" \
  --count 10
```

输出在 `evals/drafts/<skill>/`：

* `routing_<scope>_v0.1-draft.jsonl`：头部含 generator 版本、模型、参数、prompt hash、
  skill hash，状态为 `DRAFT`。**文件名带 scope**，因为它会原样变成 output 目录名的第一维；
* `suite.yaml`：可通过 `contracts/suite.py` 严格校验，但 `cfg=v1-draft`；
* `REVIEW.md`：**开头是每道 `rej` 题「为什么 catalog 里没有一个 skill 该激活」的逐题理由**
  （catalog 有多个 skill 时这是硬要求，写不出理由的批次直接拒绝生成），后面才是模型
  自述的高风险 amb/rej gold；
* 传 `--reference <人工集>` 时另出 `case-diff.json`，逐 case 标出 prompt/gold 分歧。

传单个 skill 目录时，生成器默认只评这个目标，不会静默把个人 skills 目录中的相邻 skill
变成 multi gold。确实要测相邻 skill 的边界时，显式加 `--include-neighbors`。`--count`
支持 3–30（默认 10）；超过 10 的草稿应先完成同等严格的人审，再进入真实运行。

若目标 SKILL.md 缺少 YAML frontmatter 的 `description`，生成器会在调用模型前拒绝执行；
请补 `name / description / triggers / exclusions`。否则路由题没有
可审计的能力边界。

### 5.1 生成题的人审完成标准

- [ ] `pos / amb / rej` 都有；catalog 有两个以上 skill 时还必须有 `multi`
- [ ] 每个 `amb` 的 gold 能由审核人独立说明，不能只接受模型的 review note
- [ ] 每个 `rej` 都贴近能力边界，但确实不应激活任何 catalog skill。
      **判据是「整个 catalog 里没有一个该激活」，不是「目标 skill 不该激活」** ——
      踩过：`--include-neighbors` 时生成器出了「Word 字体统一改微软雅黑 + 自动目录」
      gold=∅ 这种题，而 catalog 里明明有 `docx`。照单全收的话，模型答对反而被记成误激活，
      `false_activation` 结构性虚高，整批结论是反的。逐条对着 `REVIEW.md` 开头那节审。
- [ ] prompt 没有照抄 skill_id / triggers，也没有泄漏正文里的实现步骤
- [ ] `expected_skills` 全部存在；多 skill 顺序不表达执行顺序
- [ ] 把文件头改为 `# review_status: APPROVED — 审核人/日期/依据`，并把 dataset/suite
      改成正式版本化名字、检查 `git diff`；`DRAFT` 会被 `pipeline plan/run` 硬拦下

生成脚本**不会自动开跑**。模型生成的 gold 不是真值，未经上述人工审核不得进入 gate。

### 5.2 手工生成模板

不方便运行脚本时，也可以直接把下面模板喂给模型；产物仍须通过同一套人审与契约校验。

```text
我要为一个 skill 路由评测生成测试用例。

【可用 skill 目录】
<粘贴 subjects/*/vN/SKILL.md 的 name + description + triggers + exclusions>

【要求】
- 输出 JSONL，一行一个 case，字段只有：id, prompt, expected_skills, tags, severity
- id 格式 {scope}-{type}-{seq}：scope 是期望的 skill_id（无则 none），
  type ∈ pos|amb|multi|rej，seq 两位数
- prompt 用中文口语，是真实用户会说的话
- prompt 里禁止出现 skill_id、skill 名称、triggers 里的原词
- 本批生成：每个 skill 4 道 pos + 共 N 道 amb + 共 M 道 rej + 3 道 multi
- amb 题必须落在两个 skill 的边界上，并在 reasoning 里说明为什么选这个答案
  （reasoning 不要写进 JSONL，单独列出来给我审）
```

生成后**必须人工过一遍** `amb` 和 `rej` 两类 —— 模型最容易在这两类上出自相矛盾的题。

---

## 6. 校验与运行

```bash
DS=evals/datasets/routing_example_v1.0.jsonl

# 校验格式（走 Pydantic，字段错/多字段会报错）+ 查 id 重复 + 看配额
.venv/bin/python -c "
import collections, sys
from contracts import load_cases
cs = load_cases('$DS')
dup = [k for k,v in collections.Counter(c.id for c in cs).items() if v>1]
print(len(cs), 'cases OK | 重复 id:', dup or '无')
print('配额:', dict(collections.Counter(c.id.split('-')[-2] for c in cs)))"

# 跑（配置在 suite 里，不是 CLI 参数）
.venv/bin/python -m workflows.run_routing
.venv/bin/python -m workflows.score_routing
```

新建测试集后记得在 suite 里把 `dataset:` 指过去（见 [RUNBOOK.md](RUNBOOK.md) §2）。
