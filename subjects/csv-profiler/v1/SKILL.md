---
name: csv-profiler
description: Inspect an existing CSV or table and report per-column data quality — types, null rate, cardinality, outliers, suspicious values. Use when the user asks what's wrong with / what's in a dataset they already have, not when they want a new deliverable produced from it.
triggers: [数据质量, 看看这份数据, 有没有脏数据, 缺失值, 列都是什么, profile]
exclusions: [生成交付包, 写报告给别人看, 做图表]
---

# csv-profiler

仓库自带的**示例 skill**，用来演示路由评测怎么跑。正文很短是故意的：
routing-only 模式只读上面的 frontmatter，正文一个字都不会进模型上下文。

## 做什么

对用户已有的表格逐列体检，输出一段人读的结论：

- 每列的推断类型、空值率、唯一值数量
- 明显异常的值（超出量级、编码错乱、日期格式不一致）
- 一句话总结「这份数据能不能直接用」

## 不做什么

不产出交付文件。用户要的是「把这份数据整理成一份包交出去」时，
那是 `deliverable-pack` 的活，不是这里。
