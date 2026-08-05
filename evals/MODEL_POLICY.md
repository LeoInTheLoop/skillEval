# Evaluation model rotation policy

Source: user-supplied availability snapshot, 2026-07-28.

## Rule

For a **new** evaluation suite, choose the available model with the earliest
quota-expiry date.  If dates tie, use the lower remaining quota first.  A
running suite never switches model automatically: its `models:` entry is part
of the experiment and must stay fixed for reproducibility.

| Priority | Model | Quota expiry | Remaining at snapshot |
| ---: | --- | --- | ---: |
| 1 | `qwen3.7-max-2026-05-20` | 2026-08-19 | 129,268 |
| 2 | `qwen3.7-max` | 2026-08-19 | 1,000,000 |
| 3 | `qwen3.7-max-preview` | 2026-08-23 | 1,000,000 |
| 4 | `glm-5.1` | 2026-08-25 | 867,741 |
| 5 | `qwen3.7-plus` | 2026-08-31 | 397,067 |
| 6 | `qwen3.7-plus-2026-05-26` | 2026-08-31 | 1,000,000 |
| 7 | `glm-5.2` | 2026-09-23 | 1,000,000 |
| 8 | `kimi-k2.7-code` | 2026-09-23 | 1,000,000 |
| 9 | `qwen3.7-flash-2026-07-15` | 2026-10-22 | 1,000,000 |
| 10 | `qwen3.7-flash` | 2026-10-22 | 1,000,000 |

## Current exception

`qwen3.7-max-2026-05-17`, previously used by several example suites, returned
provider error “free quota has been exhausted” during the humanizer test. Do
not choose it for a new suite unless its provider quota is restored. The
humanizer V1/V2 comparison therefore fixed `glm-5.1` on both sides.

The repository's **new case-generation default** is now
`openai/qwen3.7-flash-2026-07-15`, matching the newer local availability ledger dated
2026-07-29. The old `qwen3.7-max-2026-05-17` default is not in the available rotation and is
known exhausted. This default applies only when creating a new draft; an existing suite never changes
models automatically. If a provider account has a different entitlement, pass both `--model-id` and
`--model` explicitly and keep that choice fixed for comparable generation runs.

## Judge policy for full-eval absolute assertions

Calibration set:
`evals/calibration/meeting_and_brief_assertions_v1.0.json` (10 human-audited
assertions). Reproducible result:
`evals/calibration/meeting_and_brief_assertions_v1.0.results.json`.

| Judge | Human agreement | Invalid output | Decision |
| --- | ---: | ---: | --- |
| `openai/qwen3.7-max` (`qwen37max`) | **100%** | 0% | Default for this assertion style |
| `openai/qwen-plus` (`qwenplus-inputs`) | 90% | 0% | Qualified, but not preferred |
| `openai/qwen-plus` (`qwenplus`) | 90% | 0% | Qualified, but not preferred |

All three clear the current ≥80% / ≤2% absolute-assertion thresholds.
`qwen-plus` missed the critical “张付总→张副总” silent rewrite. It also gave
factually wrong evidence about “赵磊” on another assertion even though the
final boolean happened to be correct for a different reason (unsupported
dates). For improvement suggestions, where evidence is consumed downstream,
use `qwen37max` unless a newer calibration supersedes this result.

A/B swap consistency is `N/A` here because these are absolute assertions, not
pairwise A/B judgments. This calibration does not authorize semantic
dimensions to enter a release gate. The qualified absolute-assertion gauges are
registered in `evals/calibration/registry.json`; the registry binds the evidence
report hash and exact model / endpoint-env / params / prompt / rubric fingerprint.
Changing any of those fields invalidates qualification.

Suites that intentionally gate on `assertion_pass_rate` must set:

```yaml
scoring:
  calibration_registry: evals/calibration/registry.json
```

Standard semantic dimensions still have no human calibration entries. Putting
one in a gate therefore produces `judge-uncalibrated` and an indeterminate gate,
not PASS/FAIL. Do **not** reuse assertion gold or model-generated labels for a
dimension. After a human has scored at least 10 applicable runs for each target
dimension, generate a dimension report and a separate registry with:

```bash
.venv/bin/python -m workflows.calibrate_dimensions \
  --gold <human-dimension-gold.json> --grading <grading.json> \
  --output <calibration.results.json> --registry-output <registry.json>
```

The default agreement rule is explicit: absolute score error ≤ 0.25 on the
0–1 scale, with ≥80% agreement, ≤2% invalid judge calls, and at least 10 human
annotations per dimension. The report also records MAE. A/B swap is `N/A`
because these are absolute scores, not pairwise rankings. The human gold pins
`dimension_versions`; a grading file produced with another rubric version is
rejected rather than compared.
