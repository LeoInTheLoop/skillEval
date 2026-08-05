# PDF V1 improvement-suggestion capability benchmark

Status: **PASS**
Prepared: 2026-07-29
Executed: 2026-08-05

## Fixed input

- Source run:
  `outputs/routing_pdf_v1.0__qwen3.7-max-2026-05-17__pdf-v1`
- Skill: `subjects/pdf/v1/SKILL.md`
- Suggestion model: `openai/qwen3.7-flash-2026-07-15`
- Prompt hash: `sha256:5cfd72f3e13e4649`
- Payload: 340-character full SKILL.md plus 9 failed trajectories from
  `none-rej-01/02/03`; rendered suggestion prompt is 4057 characters.
- Source `runs.jsonl` SHA-256:
  `2603c70920e60fbfac1a5e60430de52f202b317fd924f6196859be90ccbfe2a9`
- Source `SKILL.md` SHA-256:
  `8ea3108429363e59c1222ab1e22254875b342e8444d1d8c1a3f4f2cd07add34d`

Dry-run command:

```bash
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/routing_pdf_v1.0__qwen3.7-max-2026-05-17__pdf-v1 \
  --skill-file subjects/pdf/v1/SKILL.md \
  --skill-id pdf \
  --model openai/qwen3.7-flash-2026-07-15 \
  --api-base-env DASHSCOPE_BASE_URL \
  --api-key-env DASHSCOPE_API_KEY
```

The command printed the target endpoint, complete frontmatter metadata, and a
preview of all 9 trajectories. It then exited without a model call or file
write because `--confirm-egress` was absent.

## PASS criteria

The real suggestion must:

1. cluster all three rejection cases under the common file-format-association
   failure;
2. quote evidence from each case's archived raw output;
3. narrow activation to PDF operations such as extraction, conversion,
   merge/split, and forms;
4. exclude content-only analysis, risk assessment, and viewpoint summaries.

The same contract is covered offline by
`test_pdf_v1能力基准_mock建议按任务而非文件格式激活`.

## Result

The manifest was reviewed, then the same command was run once with
`--confirm-egress`. It wrote the immutable report to:

`outputs/routing_pdf_v1.0__qwen3.7-max-2026-05-17__pdf-v1/improvements/round-01/suggestions.json`

Result SHA-256:
`1d619806300b27819923eb59b1ae64530f09cf7f1a509709e7634048cba88546`.
The single real-model suggestion object was:

```json
{
  "pattern": "模型因 description 描述过于宽泛，将 PDF 文件的常规阅读、分析与总结任务误判为需要调用 pdf skill，导致对纯内容分析类请求产生误触发（False Positive）。",
  "case_ids": ["none-rej-01", "none-rej-02", "none-rej-03"],
  "metric": "exact_set_match",
  "evidence": [
    {
      "case_id": "none-rej-01",
      "quote": "用户需要提取并分析PDF合同中的内容，符合pdf skill的处理范围。"
    },
    {
      "case_id": "none-rej-02",
      "quote": "用户需要读取和分析PDF格式的审计报告以提取财务数据判断现金流，符合pdf skill的处理范围。"
    },
    {
      "case_id": "none-rej-03",
      "quote": "用户要求总结PDF论文的核心观点，需要提取和阅读PDF文件内容，符合pdf skill的功能。"
    }
  ],
  "change": "修改 frontmatter 中的 description 字段，将末尾的 'Use when the user works with .pdf files or wants to extract/create PDF content.' 替换为 'Use ONLY for structural manipulation (merge, split, compress), form filling, or precise table extraction. Explicitly DO NOT use for general reading, summarization, or data analysis of PDF content.' 以明确排除常规阅读与分析场景，防止模型仅凭‘PDF’关键词或‘分析/总结’意图就误选该skill。"
}
```

## Human acceptance verdict

**PASS.** The model independently identified the task-type versus file-carrier
boundary: all three rejection cases are one cluster; every evidence quote is
verbatim from the archived run; activation is narrowed to structural/form/table
operations; content-only reading, analysis, and summaries are excluded. This is
the actionable conclusion required by the P3 capability benchmark.

`apply_status=not_requested`; no PDF V3 was created and neither V1 nor V2 was
modified. The existing deterministic V2 run remains separate evidence that the
same boundary change raises rejection accuracy from 0% to 100% on this dataset.
