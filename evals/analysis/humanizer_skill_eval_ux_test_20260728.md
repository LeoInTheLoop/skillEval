# Humanizer ordinary-user evaluation trial — 2026-07-28

Scope: download `humanizer` by following `https://skillhub.cn/install/skillhub.md`,
then exercise the evaluation flow without modifying evaluator code.

## Successful trajectory

1. `skillhub --version` found an existing CLI; no first-install “priority
   source” choice was needed.
2. `skillhub search humanizer` located `humanizer` 1.0.0 and
   `skillhub install humanizer --dir skills` installed the actual artifact at
   `skills/humanizer/`.
3. `workflows.gen_cases` generated an 8-case draft and stopped for review as
   intended.  Human review promoted it to `routing_humanizer_v1.0.jsonl`.
4. `pipeline plan --healthcheck` showed catalog, model, task count, hashes,
   outputs and gate before cost was incurred.
5. A two-model smoke test exercised Qwen and GLM.  The evaluator preserved
   failed API attempts with `error_kind=network`, rather than scoring them as
   task failures.
6. `workflows.suggest` clustered raw failure evidence without changing the
   skill.  A manual V2 routing-metadata overlay was then created.
7. Same-dataset GLM V1/V2 comparison gave V1 68.8% / FAIL and V2 100% / PASS;
   `workflows.compare_runs` found no configuration pollution.

## Human-facing friction observed

- With sandboxed network, SkillHub `search` printed “No skills found” while
  exiting successfully, and `install` only exposed the DNS problem in text.
  A normal user needs a clearer actionable network status.
- The first model run had all 32 requests fail due to blocked DNS. The
  evaluator recorded `network` correctly, but the final score command only
  said “no evaluable runs”; it did not summarize `error_kind` or remediation.
- LiteLLM printed repeated “Give Feedback / Get Help” blocks for each failed
  request, hiding the useful progress and error summary.
- The catalog warning says a disabled skill is still evaluated. That is useful,
  but it leaves an ordinary user uncertain whether the measured catalog matches
  what their agent will actually load.
- The test initially marked cases `high` but gated on `critical_miss`. The
  report showed N/A as `nan%` and failed the gate. The authored guidance
  explains the distinction, but plan-time validation or a direct warning would
  prevent this confusing configuration.
- Qwen model `qwen3.7-max-2026-05-17` later failed because its provider free
  quota was exhausted. The raw provider message is preserved, but it is grouped
  under the broad `network` error kind.

## Boundary change evidence

The V1 description’s broad “make it sound more natural and human-written” led
to activating humanizer for ordinary Word/PPT polishing and a translationese
request. `suggestions.json` preserves the case IDs, raw model reasoning and
proposed wording. V2 makes explicit AI/LLM artifacts necessary and ordinary
proofreading, translation polishing, transcription correction and format
editing excluded.

## Scope limitation

This is a routing-only result. The V2 overlay is intentionally metadata-only
for this test, so it does not validate the installed skill’s full rewriting
behaviour or any artifact/tool execution. Semantic judge was inspected but not
run: it requires an independently configured judge credential, which was not
available.
