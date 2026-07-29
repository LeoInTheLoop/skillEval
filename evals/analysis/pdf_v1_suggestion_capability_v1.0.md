# PDF V1 improvement-suggestion capability benchmark

Status: **PENDING EXTERNAL-EGRESS APPROVAL**
Date: 2026-07-29

## Fixed input

- Source run:
  `outputs/routing_pdf_v1.0__qwen3.7-max-2026-05-17__pdf-v1`
- Skill: `subjects/pdf/v1/SKILL.md`
- Suggestion model: `openai/qwen3.7-max`
- Prompt hash: `sha256:3fa49ef39f0332b2`
- Payload: 340-character full SKILL.md plus 9 failed trajectories from
  `none-rej-01/02/03`; rendered suggestion prompt is 3868 characters.

Dry-run command:

```bash
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/routing_pdf_v1.0__qwen3.7-max-2026-05-17__pdf-v1 \
  --skill-file subjects/pdf/v1/SKILL.md \
  --model openai/qwen3.7-max
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

- Raw real-model suggestion: **N/A — no egress authorization**
- Human acceptance verdict: **PENDING**
- PDF V3 created: **No**

After explicit authorization, rerun the fixed command with
`--confirm-egress`, preserve the resulting `suggestions.json` verbatim, and
record PASS/FAIL against the criteria above. Do not add `--apply`.
