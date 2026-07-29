# ctxweave 自动生成集 v1.0 一致性审计

人工基准（`ctxweave_10_v1.0`，qwen，3 repeats）：

| cfg | exact | gate |
| --- | ---: | --- |
| none | 70.0% | FAIL |
| v1 | 83.3% | FAIL |
| v2 | 100.0% | PASS |

P2 自动生成并人工审核的首版（`routing_interactive-architecture-diagram_v1.0`）：

| cfg | exact | no-skill rejection | false activation | gate |
| --- | ---: | ---: | ---: | --- |
| generated-none | 83.3% | 73.3% | 26.7% | FAIL |
| generated-v1 | 80.0% | 100.0% | 0.0% | PASS |
| generated-v2 | 90.0% | 100.0% | 0.0% | PASS |

结论：**不一致，不能把 v1.0 自动集用于 P2 gate 验收。**

差异证据：

1. 人工集配比为 `4 pos / 3 amb / 3 rej / 0 multi`；自动集为
   `3 pos / 3 amb / 2 rej / 2 multi`。目标 skill 缺席时，两道 multi 仍可由 pptx/xlsx
   完成一部分，削弱了 No-Skill 基线。
2. 自动集 `none-rej-01/02` 在 prompt 里直接写了“不需要画图”“不创建文档”，
   等于提示模型不要误激活；人工集的拒答题只表达业务任务，不泄漏路由答案。
3. 自动集的 V1 错误集中在 `docx-amb-01`、`pptx-amb-01`，共 6 次；
   但 exact 恰好 80%，踩线 PASS。人工集的 V1 还会在贴边 rej 上误激活，因此 gate FAIL。
4. V2 相对 V1 仍提升 `+10pp exact / +33.3pp amb`，方向正确，但 gate 结论不足以复现人工基准。

修正进入 generator `p2-v0.2`：10 题配比改为 `4/3/2/1`，并禁止 rej 用显式否定提示
泄漏 gold。旧数据与 run 保留，不覆盖；修正后生成集 bump 到 v1.1。

## v1.1 复验：通过

`p2-v0.2` 重新生成、人工审核后，qwen × 3 repeats：

| cfg | exact | no-skill rejection | false activation | gate |
| --- | ---: | ---: | ---: | --- |
| generated-none | 56.7% | 45.8% | 54.2% | FAIL |
| generated-v1 | 80.0% | 33.3% | 66.7% | FAIL |
| generated-v2 | 100.0% | 100.0% | 0.0% | PASS |

结论：`none < v1 < v2`，并且 gate 为 `FAIL / FAIL / PASS`，与人工基准一致。
V1 的 9 次 rej 中 6 次误激活到目标 skill，V2 9/9 正确拒答；改进方向也与人工集一致。
