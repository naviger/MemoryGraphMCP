# Architecture retrieval and delivery pilot

The [case manifest](benchmark.json) contains four cases across MemoryGraphMCP and
Sextant. Each project has one question about a real architecture decision and one
small test-writing task. Each case runs twice, for eight trials total.

## Run the paired trials

1. Generate the trial prompts with the plan command below. Give the agent only the
   selected case from that output. Keep the manifest's expected claims and citations
   with the grader. Include the shared `delivery_guidance` in both conditions.
   It states the pinned behavior and test runtime assumptions. Record its use
   in the trial setup because it changes comparability with earlier delivery runs.
2. Use fresh, disposable worktrees at the case's exact base commit and fresh agent
   sessions. Keep the model version, settings, tools apart from project memory,
   instructions, time budget, and check commands the same within each pair. Use a
   stable setup label for those controls. Alternate which condition runs first.
3. In the **repository** condition, use the checkout's guidance, ADR files, and
   code search. In the **memorygraph** condition, allow the same repository access
   plus published project guidance, graph lookup, and cited retrieval. Use the
   case's as-of date. Verify code citations against the pinned checkout when
   the graph's current index has a different or unknown revision. For an
   assisted answer, pass the `prepare_context_excerpts` results to the model and
   ask it to select supporting IDs. Call `finalize_context_excerpts` with the
   same query and returned context ID. Save the raw selections, verified findings, and flagged gaps.
   Measure finalization separately from model request time.
4. Save each answer or patch and its check output. For delivery, inspect the
   diff against the requested behavior and look for vacuous tests. Record
   every listed check, including failures or checks that could not run.
   Use evidence references the grader can inspect.

```sh
python -m app.pilot_benchmark plan docs/pilot/benchmark.json
python -m app.pilot_benchmark score docs/pilot/benchmark.json --results /path/to/trials.json
```

The runner also ships in the API container. From the repository checkout, mount
the manifest and results read-only into a one-off container to grade them, or run
the module in the host environment.

## Record the evidence

`trials.json` contains a `trials` array with one entry per case and condition.
Use `condition` of `repository` or `memorygraph`. The `commit` is the clean
base checkout before any task edits. The grader inspects the result against the
manifest and includes a claim ID only when the answer establishes that claim
and its cited evidence actually supports it. Normalize a citation to
`repo:<relative-path>` after checking it at the case commit, or
`records:<record-key>` after checking the record and its validity. Equivalent
citations are listed together in the manifest. Omit unsupported claims.

Example for one retrieval entry:

```json
{
  "case_id": "memory-review-schedule",
  "condition": "repository",
  "model": "model-and-version",
  "setup": "pilot-config-1",
  "commit": "476f35a429db0769d1db5f2244443a41cbdb7717",
  "evidence": "artifacts/memory-review-schedule-repository.md",
  "findings": {"independent-dates": ["repo:README.md"]},
  "checks": {},
  "tokens": 1200,
  "seconds": 42,
  "retries": 0
}
```

For a delivery entry, each manifest check ID maps to an object with the exact
`command`, a `status` of `pass`, `fail`, or `not_run`, and an `evidence`
reference to the output or reason. Review the actual patch as well as the command
output. Use the same token accounting and timing window within each pair.
Count retries after the first attempt. Leave unavailable measurements unset.

The scorecard reports supported claims and required claims for retrieval, cases
with all checks passing for delivery, and paired token, time, and retry
differences when both values are recorded. The scorecard checks the grader's
reported citations and commands. It relies on the grader to verify the answers,
patches, and logs. Treat the eight trials as a pilot sample.
