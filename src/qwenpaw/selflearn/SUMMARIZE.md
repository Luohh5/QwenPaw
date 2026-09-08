# Stage: Analyze / Summarize

## Task

Synthesize the user-feedback annotations produced by Analyze / Episode Analysis
into evidence-backed improvement proposals for the review bot's harness.
Identify which observed problems warrant a change, where that change belongs,
and how it should be tested. Produce actionable work items for a later Optimizer.

This stage is analysis only. Do not edit files, change configuration, deploy
anything, or claim to have run tests or evaluations.

## Inputs

- **Case index:** episode IDs, PR grouping IDs, first-stage summaries and
  findings, missing evidence, review-window metadata, reviewed-code metadata,
  and data-quality flags.
- **Case details, available through `read_case`:** the first-stage annotations
  with their reasons and evidence, together with the original episode's review,
  user feedback, code diff, subsequent changes, and recorded outcome.
- **Harness target list:** target IDs, file paths, target types, scopes, change
  modes, allowed changes, and file-existence metadata.
- **Frozen harness contents, available through `read_harness`:** the file
  contents captured for the allowed targets in this run.
- **Coverage:** the source dataset and the numbers of analyzed episodes and
  distinct pull requests represented in the batch.

## Evidence Access

- Use `read_case` to inspect annotations and original evidence. An annotation's
  evidence pointer is relative to the original episode; prefix it with
  `/episode` when calling this tool, for example
  `/episode/post_review_events/0/text`.
- Use `read_harness` to inspect allowed targets in the frozen snapshot. Start
  with a focused `search`, then expand relevant passages with
  `start_line` / `end_line`. A search returns `context_lines` surrounding each
  match. Without a search or end line, the default is 60 lines; other reads
  return at most 200 lines. Follow `next_start_line` to continue.
- A failed search or an incomplete excerpt does not establish that a rule is
  absent. Try other terms or inspect a wider range before claiming a gap.
- Identical passages from the same file are deduplicated, even across target
  IDs. To retrieve a passage again, repeat the request with `reread=true`.
  Quote the original text, not tool-added line numbers or truncation notices.

## Workflow

1. **Read the case index and identify questions worth investigating.**
   Note the specific review conclusion or omission at issue and what the
   feedback appears to challenge, support, or clarify. Keep candidate problems
   separate from conclusions about their causes.

2. **Recheck the annotations against the original evidence.**
   Read the relevant review, feedback, and available code. Determine whether the
   feedback actually concerns the bot, whether it is credible, and whether it
   concerns the version the bot reviewed. First-stage labels are not ground
   truth. If they conflict with the evidence, describe the conflict and use
   `needs_evidence`; do not silently rewrite the first-stage findings.

3. **Group related observations by mechanism and look for counterexamples.**
   Group cases only when a shared failure mechanism is plausible, not because
   they use similar words. Look for cases that support existing behavior,
   contradict the proposed explanation, or narrow its scope. Multiple reviews,
   replies, or reactions within one PR are not independent cases. A single
   serious case may justify an experiment, but it is not a recurring defect.

4. **Inspect the relevant harness and check what it already covers.**
   Read the targets related to each candidate problem. Identify the existing
   instruction, capability, or concrete gap that matters. If a rule already
   addresses the issue, investigate why it may not have been effective instead
   of proposing the same instruction again. The current snapshot cannot
   establish which configuration was active during a past review.

5. **Form a primary causal hypothesis and retain alternatives.**
   Explain how the candidate harness issue could account for the observed
   behavior, what supports that explanation, and what remains unknown. Without
   execution traces, do not claim that a skill failed to activate, a search was
   skipped, or model capability was the cause. Separate the observed review
   error from the suspected cause and the proposed remedy.

6. **Choose the most appropriate allowed target.**
   Select one `primary_target` and respect its `scope`, `change_mode`, and
   `allowed_changes`. Project-specific facts may belong in knowledge; reusable
   procedures in a skill; communication constraints in a prompt. A missing
   capability may require a tool or workflow change, not more instructions.
   Use `null` if no target is justified. An `engineering` change mode denotes
   work for a later engineering step, not permission to modify source code here.

7. **Specify the smallest useful change and its verification plan.**
   State what should change, why it should help, and which behavior must remain
   intact. For a new file, explain how the bot would load or invoke it. Describe
   how to test the causal hypothesis, confirm the change takes effect, compare
   old and new behavior, and check counterexamples and unseen cases within a
   reasonable budget. This batch is development evidence, not an independent
   blind test set. Preserve permissions, safety controls, independent evaluation,
   and human approval requirements.

8. **Return the structured proposals.**
   Tie each proposal to specific findings and exact harness excerpts. Choose
   `propose`, `needs_evidence`, or `no_change` according to what the evidence
   supports. Do not manufacture a proposal for every case. If neither a change
   nor further investigation is warranted, return an empty `proposals` list
   and explain why.

## Output Contract

Use the supplied structured-output schema. Write explanatory fields in Chinese;
preserve quotations in their original language and formatting.

Top-level fields:

- `summary`: the main conclusions from this batch.
- `proposals`: the improvement work items described below; may be empty.
- `limitations`: coverage limits, missing evidence, and uncertainties that
  constrain the conclusions.

Each proposal contains:

- `problem`: the reusable problem or specific question that warrants attention.
- `supporting_findings`: the findings supporting it, referenced by `episode_id`
  and zero-based `finding_index`, not by a whole PR indiscriminately.
- `counter_evidence`: findings that challenge the hypothesis, support existing
  behavior, or limit generalization; may be empty.
- `root_cause_hypothesis`: the leading explanation, its evidence, and uncertainty.
- `primary_target`: the selected allowed target ID, or `null`.
- `harness_evidence`: exact excerpts from the snapshot, each with `target_id`
  and `quote`. A missing proposed file is not proof of a missing capability;
  inspect the existing harness as well.
- `change_intent`: the minimal proposed change and its rationale, or `null`.
- `alternatives`: other explanations or intervention points and why they are
  not the first choice.
- `verification_plan`: checks and experiments to perform later, not claims
  about completed validation.
- `status`: `propose` for a candidate experiment, `needs_evidence` when more
  evidence is needed, or `no_change` when a persistent change is unwarranted.
  `propose` does not mean the cause is proven or the change is approved to ship.

The program calculates the number of independent PRs from the referenced cases.
That count is not a vote and does not establish a common cause.
