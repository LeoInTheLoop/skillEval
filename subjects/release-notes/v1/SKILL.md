---
name: release-notes
description: Turn a raw commit log, merged PR list, or changelog fragment into human-readable release notes grouped by change type, written for users rather than developers. Use when the user asks to announce or summarize what shipped in a version.
triggers: [发版说明, release notes, 更新日志, 这版改了什么, changelog]
exclusions: [写代码, 分析数据, 生成表格]
---

# release-notes

仓库自带的**示例 skill**，用来演示路由评测怎么跑。正文很短是故意的：
routing-only 模式只读上面的 frontmatter，正文一个字都不会进模型上下文。

## 做什么

把 commit / PR 列表改写成给用户看的发版说明：

- 按「新功能 / 修复 / 破坏性变更」分组
- 每条用用户语言重写，去掉内部模块名和 PR 编号
- 破坏性变更单独置顶，写清要改什么

## 不做什么

不碰数据分析，也不产出 CSV。
