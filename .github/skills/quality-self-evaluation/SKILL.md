---
name: quality-self-evaluation
description: Score each final answer and changed deliverable against the live OCF rules with explicit evidence. Use before final reporting.
argument-hint: "The answer and each deliverable to evaluate"
user-invocable: true
disable-model-invocation: false
---

# OCF Quality Self-Evaluation

This is an OCF-specific evidence report, not a general writing rubric and not an independent model
benchmark. Evaluate the final user-facing answer and each new or changed deliverable separately.
Do not score intermediate status updates. Do not write scores into the artifact being evaluated.

## Evaluate

1. Load the current policy from the workspace. Start with the live rule list from
   `python .github/skills/quality-self-evaluation/scripts/score.py --workspace <root> --template`;
   add one `--deliverable <id>` for each new or changed output. Fill every placeholder before scoring.
   Create one artifact with `kind: answer` and one `kind: deliverable` artifact for each output.
2. For each artifact, cover every rule ID in the current policy exactly once. Use the artifact's
   `occasions` to name the soft-rule moments that actually applied while it was produced.
3. For a soft rule, use `not_applicable` when it is disabled or its occasion does not apply. When it
   applies, score the answer or artifact against its wording and cite a concrete passage or result.
   If the required evidence is unavailable, use `unverified`, not `not_applicable`. Each deliverable
   must name at least one real production occasion; do not leave the template blank.
4. For a hard rule, use `not_applicable` only when the operation did not meet the rule's conditions.
   Score compliance only from a direct hook, tool, journal, or test result. If no such evidence is
   available, use `unverified`.
5. Run `scripts/score.py` with the workspace root and the completed JSON input on stdin. Fix schema errors;
   do not omit rules to get a score.
6. In the final report, show the answer score, each deliverable score, overall score, rule-level
   evidence summary, and hard-compliance status. Keep hard compliance separate from the overall
   quality score.

## Score anchors

- `5`: the rule is fully satisfied and specific evidence shows how.
- `4`: satisfied, with only a small omission that does not affect the goal.
- `3`: partly satisfied or awaiting review.
- `2`: a substantial requirement is missing.
- `1`: the artifact contradicts the rule.

For hard rules, `5` requires direct evidence of compliance, `4` requires sufficient indirect runtime
evidence, `3` means the result is unresolved, `2` means a likely violation, and `1` means a confirmed
violation. Never assign a numeric hard score from prose alone.

The evaluator requires one record per live rule ID. Disabled rules and rules whose conditions do not
apply use `not_applicable` with a reason. Applicable rules with missing evidence use `unverified`.
Each numeric score requires non-empty evidence.

`quality_status` describes score coverage only: `COMPLETE` means no applicable rule is unverified,
`PARTIAL` means at least one applicable rule is unverified, and `UNRATED` means no numeric score is
available. `hard_compliance` is independent: a hard score of 1 or 2 yields `FAIL`; a hard score of 3
or applicable `unverified` yields `UNVERIFIED`; applicable hard rules all scored 4 or 5 yield
`PASS`; all hard rules not applicable yields `NOT_APPLICABLE`. A high overall score never cancels a
hard-rule failure.
