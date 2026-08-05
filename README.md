# skillEval

Reproducible experiments for evaluating agent skills.

[中文文档 / Chinese version](README.zh-CN.md)

skillEval answers two questions:

1. Did the agent select the right skill for a user request?
2. After the skill was loaded, did the agent complete the task, use the expected tools, and produce the expected artifacts?

| Mode | What it evaluates | Runtime | Main outputs |
| --- | --- | --- | --- |
| `routing_only` | Skill activation from metadata | LiteLLM / OpenClaw | Exact match, Top-1, false activation, No-Skill rejection, confusion matrix |
| `full` | Skill execution with an agent loop, tools, and artifacts | OpenClaw | Task completion, tool hit rate, artifact hit rate, multi-turn continuity |
| `mock` | Installation, orchestration, scoring, and reporting paths | Built-in mock runtime | Synthetic results; not a skill-quality verdict |

Every run freezes the suite, dataset, skill snapshot, runtime fingerprint, and raw output. Scoring writes new files and never modifies `runs.jsonl`; historical runs are never overwritten.

## Quick start

Requirements: Python 3.11–3.13. Python 3.14 may work, but dependency installation can take longer.

```bash
git clone https://github.com/LeoInTheLoop/skillEval.git
cd skillEval
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Use the bundled routing suite and mock runtime to verify the complete pipeline without an API key:

```bash
.venv/bin/python -m pipeline plan \
  --suite evals/suites/example_routing.yaml \
  --mock

.venv/bin/python -m pipeline run \
  --suite evals/suites/example_routing.yaml \
  --mock \
  --confirm
```

`plan` is read-only: it does not write to `outputs/` or call a model. `run` prints the same plan and then produces an immutable run directory:

```text
outputs/routing_example_v1.0__mock__v1/<execution-id>/
├── config.snapshot.yaml
├── inputs/
├── runs.jsonl
├── trajectory.jsonl
├── scores.json
└── report.html
```

The mock runtime only proves that the pipeline works. It does not evaluate skill quality.

## Running a real full eval

Full eval requires OpenClaw because LiteLLM only performs a single completion and does not execute an agent tool loop. The repository includes a Docker-first example:

```bash
docker build -f environments/openclaw.Dockerfile -t skilleval-openclaw .
export SKILLEVAL_OPENCLAW_IMAGE="$(docker image inspect skilleval-openclaw --format '{{.Id}}')"

.venv/bin/python -m pipeline plan \
  --suite evals/suites/example_full.yaml \
  --healthcheck

.venv/bin/python -m pipeline run \
  --suite evals/suites/example_full.yaml \
  --confirm \
  --confirm-egress
```

The bundled example has two cases and is already a verified public smoke test. For a new skill, first create a separate one-case baseline dataset and suite. Run it with `repeats: 1` and `parallelism: 1`, verify skill loading, tool calls, artifacts, and `scores.json`, then expand the full dataset. See the [full-eval baseline procedure](evals/RUNBOOK.md#full-eval-首次运行先做-one-case-baseline).

The complete example files are [`evals/suites/example_full.yaml`](evals/suites/example_full.yaml) and [`evals/datasets/full_example_v1.0.jsonl`](evals/datasets/full_example_v1.0.jsonl).

Run the test suite with:

```bash
.venv/bin/python -m pytest tests/ -q
```

## Unified CLI

```bash
.venv/bin/python -m pipeline plan --suite <suite.yaml>
.venv/bin/python -m pipeline run  --suite <suite.yaml> --confirm --confirm-egress
```

Before spending API budget or sending data, `pipeline plan` checks suite, dataset, and skill contracts; runtime capabilities; skill catalog and versions; API key state; request count and external-data categories; output location; and `config_hash`.

Add `--healthcheck` to probe the local runtime and environment. A real `pipeline run` repeats the health check automatically. All result-changing parameters belong in the suite, so the run snapshot remains the actual experiment configuration.

## Using a third-party skill

The shortest complete path uses the public [`blader/humanizer`](https://github.com/blader/humanizer) skill.

Download and inspect it first:

```bash
git clone --depth 1 \
  https://github.com/blader/humanizer.git \
  installed_skills/humanizer

find installed_skills/humanizer -maxdepth 3 -type f -print
sed -n '1,220p' installed_skills/humanizer/SKILL.md
```

Freeze a versioned evaluation snapshot instead of evaluating the download directory directly:

```bash
.venv/bin/python -m workflows.import_skill \
  --source installed_skills/humanizer \
  --version v1
```

| Path | Role |
| --- | --- |
| `installed_skills/<name>/` | Upstream download or installation source; not evaluated directly |
| `subjects/<skill-id>/vN/` | Immutable subject snapshot used for V1/V2 comparisons |
| `outputs/<run>/inputs/skills/` | Exact copy used by one run for later inspection |

The bundled humanizer smoke dataset and suite are documented in [evals/RUNBOOK.md](evals/RUNBOOK.md). A smoke test confirms the execution chain; it is not a formal quality conclusion.

## Dataset and suite contract

A full-eval case can declare deterministic expectations:

```json
{
  "id": "deliverable-pos-01",
  "prompt": "Read the input and create out/report.md",
  "files": ["evals/fixtures/input.txt"],
  "expected_skills": ["deliverable-pack"],
  "expect_tools": ["read", "write"],
  "expect_artifacts": ["out/report.md"],
  "severity": "high"
}
```

Important fields:

- `expected_skills`: the correct activation set; an empty list means No-Skill;
- `expect_tools`: tools that must be observed;
- `expect_artifacts`: files that must be created or modified;
- `forbid_artifacts`: asserts that the case must not leave files;
- `files`: read-only inputs copied into an isolated workspace;
- `expect_assertions`: semantic assertions evaluated by an independent judge;
- `turns`: subsequent turns in a multi-turn full case;
- `severity: critical`: includes the case in Critical Skill Miss Rate.

The suite separates ownership from the visible catalog:

```yaml
skills:
  dir: subjects
  target: [pdf]
  include: [pdf, docx, xlsx]
  versions: {pdf: v2}
  mode: full
  cfg: v2
```

`target` says which subject owns the experiment. `include` says which skills the agent can see. A No-Skill baseline keeps the target but removes it from `include`. The top-level `tools` field is the runtime allowlist; case-level `expect_tools` is scoring gold and does not grant permission.

## Results, comparisons, and improvement

Each model and execution has an independent directory:

```text
outputs/<dataset>__<model>__<skillcfg>/<execution-id>/
├── config.snapshot.yaml
├── inputs/
├── runs.jsonl
├── trajectory.jsonl
├── grading.<judge-id>.json
├── trajectory_grading.<judge-id>.json
├── scores.json
└── report.html
```

For a V1/V2 comparison, copy the old skill snapshot and create a new version. Do not edit a version that has already been evaluated:

```bash
cp -R subjects/<skill>/v1 subjects/<skill>/v2
```

Change only the target version and config label:

```yaml
skills:
  versions: {<skill>: v2}
  cfg: v2
```

Then compare the immutable run directories:

```bash
.venv/bin/python -m workflows.compare_runs \
  outputs/<v1-run>/<execution-id> \
  outputs/<v2-run>/<execution-id>
```

Semantic judging is opt-in and uses an independent model:

```bash
.venv/bin/python -m pipeline run \
  --suite <suite.yaml> \
  --stages run,grade,score \
  --confirm \
  --confirm-egress
```

Generate clustered improvement suggestions from a failed run:

```bash
.venv/bin/python -m workflows.suggest \
  --run-dir outputs/<run>/<execution-id>
```

Only `--apply` creates `subjects/<skill-id>/v<N+1>/` and a same-dataset re-evaluation suite. The source version is never overwritten.

## Privacy, security, and reproducibility

The repository tracks only synthetic routing and full examples. Local credentials, installed skills, private subjects, private datasets, fixtures, outputs, and archives are ignored by default.

Before committing, inspect ignored files and staged content:

```bash
git status --ignored --short
git diff --cached --check
git grep --cached -n -I -E \
  '(sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{16}|github_pat_[A-Za-z0-9_]{20,})' \
  || true
```

Important boundaries:

- `pipeline plan` does not call a model;
- a real run displays its endpoint, request count, and payload categories;
- routing-only sends skill metadata only;
- full eval sends the case, selected `SKILL.md` content, and tool catalog;
- API key values never enter suites, snapshots, reports, or model payloads;
- third-party skills may contain scripts or prompt injection and must be reviewed before import;
- Docker should be used for untrusted skills; local workspaces are not strong security sandboxes.

`config_hash` includes result-changing suite fields, dataset and skill hashes, model parameters, runtime fingerprint, and environment configuration. Runs with different hashes are not directly comparable as a single-variable A/B test.

## Architecture and documentation

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

| Document | Purpose |
| --- | --- |
| [evals/AUTHORING.md](evals/AUTHORING.md) | Case authoring, naming, versions, gold answers, and review |
| [evals/RUNBOOK.md](evals/RUNBOOK.md) | Suites, execution, comparisons, artifacts, archiving, and baseline runs |
| [evals/ARTICLE_INSIGHTS.md](evals/ARTICLE_INSIGHTS.md) | 腾讯技术文章《AI Agent & Skill 测评方案及落地实践》的关键点、功能归因与取舍 |
| [evals/CASEGEN.md](evals/CASEGEN.md) | Automatic case generation and review |
| [evals/TRAJECTORY.md](evals/TRAJECTORY.md) | Trajectory data, metrics, and judging |
| [OPENCLAW.md](OPENCLAW.md) | OpenClaw setup, health checks, Docker, and troubleshooting |
| [AGENTS.md](AGENTS.md) | Architecture, acceptance criteria, and project status |
| [README.zh-CN.md](README.zh-CN.md) | Chinese version of this guide |

Main code areas:

```text
contracts/                  strict data contracts
adapters/runtimes/          litellm / openclaw / mock
adapters/routing_inputs/    direct / production_context
environments/               local / docker
pipeline/                   init / plan / run / archive / unarchive
workflows/                  execution, scoring, judging, comparison, suggestions
evaluators/                 outcome / trajectory / reliability / efficiency
tests/                      regression and contract tests
```

## Current status

| Capability | Status |
| --- | --- |
| Routing eval, No-Skill, multi-skill, production context | ✅ |
| OpenClaw full eval, tools/artifacts, error classification, tool allowlist | ✅ |
| Multi-turn sessions and conversation concurrency | ✅ |
| V1/V2 deltas, contamination detection, immutable versions | ✅ |
| Automatic routing drafts with human review gate | ✅ |
| Failure evidence → suggestions → new version → same-case re-evaluation | ✅ |
| Independent semantic judge with evidence | ✅; calibration registry still evolving |
| Docker backend | 🚧 Fixed image, per-request containers, read-only skill mount, and resource/network controls are available; some variants remain incomplete |
| Evaluator registry | ❌ |
| End-to-end viewer | ❌; use `report.html` and structured JSON for now |

The project is still developing the evaluation system itself. During development, 10–20 high-quality cases that expose boundaries and attribution are more useful than hundreds of ordinary positive cases.

License: [GPL-3.0](LICENSE)
