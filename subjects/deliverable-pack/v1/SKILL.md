---
name: deliverable-pack
description: Turn raw numbers, notes, or findings into a standard two-file deliverable pack — a machine-readable CSV plus a written Markdown report, both saved under out/. Use when the user asks for a 交付包 / deliverable / 汇总交付 rather than a one-off answer in chat.
triggers: [交付包, deliverable, 汇总交付, 数据交付, 整理成一份包]
exclusions: [只要一句结论, 单纯问答, 不落文件]
---

# deliverable-pack

用户要「交付包」时，**必须**在工作目录下落两个文件，缺一不可。只在对话里回答不算完成。

## 1. 目录与命名

- 两个文件都写进 `out/` 子目录（不存在就先建）。
- 文件名主干用用户给的短名 `<slug>`；用户没给就用 `deliverable`。

## 2. `out/<slug>.csv` —— 机器可读那份

第一行必须是这个表头，逐字不变、不加空格：

```
item,value,unit,note
```

其后每行一条数据。没有单位填 `-`，没有备注留空。

## 3. `out/<slug>.md` —— 给人看那份

必须包含这三个二级标题，顺序不变：

```
## 概览
## 明细
## 结论
```

- **概览**：两三句话说清这批数据是什么、覆盖什么范围。
- **明细**：把 csv 的内容用 Markdown 表格重述一遍。
- **结论**：至少给一条可执行的判断，不要只把数字念一遍。

## 4. 回复前自检

写完后把两个文件读回来确认：

1. 两个文件都存在且非空；
2. csv 第一行就是上面那行表头；
3. md 里三个二级标题都在。

任何一条不满足，先修好再回复。回复正文里写清两个文件的路径。
